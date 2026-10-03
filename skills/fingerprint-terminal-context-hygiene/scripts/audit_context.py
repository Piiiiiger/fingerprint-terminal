#!/usr/bin/env python3
"""Read-only context inventory; report markers without echoing file contents."""

from __future__ import annotations

import argparse
import collections
import json
import os
from pathlib import Path, PurePosixPath
import re


CJK = re.compile(r"[\u3400-\u9fff]")
MARKERS = {
    "regional_format": re.compile(r"Asia/Shanghai|Asia/Chongqing|zh[_-]CN", re.I),
    "location_mention": re.compile(
        r"中国大陆|常驻中国|人在国内|人在中国|大陆办公|大陆工作|中国人|"
        r"mainland China|based in China|located in China", re.I
    ),
    "singapore": re.compile(r"新加坡|Singapore|Asia/Singapore", re.I),
}
PRUNE = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".ssh", ".aws", ".cloudflared"}
SENSITIVE = re.compile(r"credential|password|secret|token|oauth|auth|id_rsa|id_ed25519", re.I)
CONTEXT_NAMES = {"CLAUDE.md", "CLAUDE.local.md", "AGENTS.md", "MEMORY.md"}
TEXT_SUFFIXES = {".md", ".txt", ".json", ".jsonl", ".yaml", ".yml", ".sh"}


def files_under(root: Path, hidden: set[str], shared: bool):
    if root.is_symlink() or not root.is_dir():
        return
    for base, dirs, names in os.walk(root, followlinks=False):
        base = Path(base)
        dirs[:] = sorted(
            name for name in dirs
            if name not in PRUNE
            and not (shared and name in {".trash", "vendor", ".cache"})
            and not (base / name).is_symlink()
            and (base / name).relative_to(root).as_posix() not in hidden
        )
        for name in sorted(names):
            path = base / name
            if path.is_symlink() or not path.is_file() or SENSITIVE.search(name):
                continue
            relative = path.relative_to(root)
            if shared and name not in CONTEXT_NAMES and not (
                ".claude" in relative.parts and path.suffix.lower() in TEXT_SUFFIXES
            ):
                continue
            yield path


def memory_path(raw: str, home: Path, username: str) -> Path | None:
    if raw == "~":
        return None
    if raw.startswith("~/"):
        candidate = home / raw[2:]
    elif PurePosixPath(raw).is_absolute():
        try:
            candidate = home / PurePosixPath(raw).relative_to(f"/home/{username}")
        except ValueError:
            return None
    else:
        candidate = home / raw
    if candidate.resolve() == home.resolve() or not candidate.resolve().is_relative_to(home.resolve()):
        return None
    return candidate


def audit(config: Path, data_root: Path, profile_id: str, max_bytes: int, limit: int) -> dict:
    document = json.loads(config.read_text(encoding="utf-8"))
    profile = next(p for p in document["profiles"] if p["id"] == profile_id)
    if profile.get("sandbox", {}).get("mode") != "strict" or profile.get("home", {}).get("mode") != "isolated":
        raise ValueError("context auditing requires a strict profile with an isolated HOME")
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", profile_id) or profile_id in {".", ".."}:
        raise ValueError("unsafe profile ID")
    home = data_root / profile_id / "home"
    if home.is_symlink():
        raise ValueError("isolated HOME must not be a symlink")
    claude = home / ".claude"
    if claude.is_symlink():
        raise ValueError("Claude context directory must not be a symlink")
    settings_path = claude / "settings.json"
    settings = json.loads(settings_path.read_text()) if settings_path.is_file() and not settings_path.is_symlink() else {}
    memory = memory_path(str(settings.get("autoMemoryDirectory") or "~/.claude/memory"), home, profile.get("sandbox", {}).get("username", "dev"))
    warnings = []
    if memory is None:
        warnings.append("custom memory directory is outside the private HOME; skipped")
    scopes = []
    if memory is not None:
        scopes.append(("memory", memory, set(), False))
    for name in ("projects", "file-history", "paste-cache", "plans", "shell-snapshots", "backups"):
        scopes.append((name, claude / name, set(), False))
    rules = claude / "rules"
    scopes.append(("rules", rules, set(), False))
    hidden = set(profile.get("sandbox", {}).get("hidden_paths", []) or [])
    host_home = Path.home().resolve()
    for share in profile.get("sandbox", {}).get("shares", []) or []:
        source = Path(os.path.expandvars(str(share["source"]))).expanduser().resolve()
        if source in {host_home, Path("/")}:
            warnings.append("unsafe whole-HOME or root share skipped")
            continue
        scopes.append(("share:" + str(source), source, hidden, True))

    stats = collections.defaultdict(collections.Counter)
    findings = []
    seen = set()
    total_findings = 0

    def inspect(path: Path, scope: str):
        nonlocal total_findings
        if path in seen or path.is_symlink() or not path.is_file():
            return
        seen.add(path)
        counts = stats[scope]
        try:
            size = path.stat().st_size
            counts["files"] += 1
            counts["bytes"] += size
            # File-history snapshots have extensionless @vN names.
            if size > max_bytes or (scope != "file-history" and path.suffix.lower() not in TEXT_SUFFIXES):
                counts["skipped_content"] += 1
                return
            hit_lines = {key: [] for key in MARKERS}
            hit_counts = collections.Counter()
            cjk_lines = 0
            with path.open(encoding="utf-8", errors="replace") as stream:
                for number, line in enumerate(stream, 1):
                    cjk_lines += bool(CJK.search(line))
                    for key, pattern in MARKERS.items():
                        if pattern.search(line):
                            hit_counts[key] += 1
                            if len(hit_lines[key]) < 8:
                                hit_lines[key].append(number)
            counts["scanned_files"] += 1
            counts["cjk_files"] += bool(cjk_lines)
            for key in hit_counts:
                counts[key + "_files"] += 1
            if cjk_lines or hit_counts:
                total_findings += 1
                if len(findings) < limit:
                    findings.append({"path": str(path), "scope": scope, "cjk_lines": cjk_lines,
                                     "marker_line_counts": dict(hit_counts), "sample_line_numbers": hit_lines})
        except OSError:
            counts["read_errors"] += 1

    for scope, root, excluded, shared in scopes:
        for path in files_under(root, excluded, shared):
            inspect(path, scope)
    for path in (claude / "CLAUDE.md", claude / "history.jsonl"):
        inspect(path, "global")
    return {"profile": profile_id, "config": str(config), "private_home": str(home),
            "read_only": True, "scopes": {key: dict(value) for key, value in stats.items()},
            "findings": findings, "omitted_findings": total_findings - len(findings),
            "warnings": warnings, "note": "Markers identify context for review, not nationality or physical location."}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path(os.environ.get("FT_PROFILES_FILE") or "~/.config/fingerprint-terminal/profiles.json").expanduser())
    parser.add_argument("--data-root", type=Path, default=Path.home() / ".local/share/fingerprint-terminal/profiles")
    parser.add_argument("--profile", default="strict-auto-ip")
    parser.add_argument("--max-file-mib", type=int, default=32)
    parser.add_argument("--max-findings", type=int, default=100)
    args = parser.parse_args()
    if args.max_file_mib < 1 or args.max_findings < 0:
        parser.error("limits must be nonnegative, and max-file-mib must be positive")
    try:
        result = audit(args.config.expanduser(), args.data_root.expanduser(), args.profile, args.max_file_mib * 1024 * 1024, args.max_findings)
    except (OSError, ValueError, KeyError, StopIteration) as exc:
        parser.exit(1, f"Audit failed: {type(exc).__name__}. Check the configuration and profile paths.\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
