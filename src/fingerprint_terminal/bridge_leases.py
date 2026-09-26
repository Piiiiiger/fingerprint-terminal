"""Track and safely remove bridge-only compatibility mountpoints."""

from __future__ import annotations

import json
import os
import re
import sys
import uuid
from pathlib import Path, PurePosixPath
from typing import Iterable

from .profiles import PROFILE_DATA_ROOT


STATE_ROOT = Path.home() / ".local" / "state" / "fingerprint-terminal"
_PROFILE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


def _targets(values: Iterable[str]) -> set[str]:
    result: set[str] = set()
    for value in values:
        relative = PurePosixPath(str(value).strip())
        if relative.parts and not relative.is_absolute() and ".." not in relative.parts:
            result.add(relative.as_posix())
    return result


def _lease_dir(profile_id: str) -> Path:
    if not _PROFILE_ID.fullmatch(profile_id):
        raise ValueError(f"invalid bridge lease profile id: {profile_id!r}")
    return STATE_ROOT / profile_id / "bridge-leases"


def _process_start_time(pid: int) -> str | None:
    try:
        value = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        # Everything after the final ') ' begins with field 3 (state); field 22
        # (starttime) is therefore index 19 in this tail.
        return value.rsplit(") ", 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def _process_matches(pid: int, start_time: str) -> bool:
    return bool(start_time) and _process_start_time(pid) == start_time


def create_bridge_lease(
    profile_id: str,
    targets: Iterable[str],
    protected_targets: Iterable[str],
) -> Path | None:
    """Register compatibility targets before Bubblewrap creates them."""

    normalized = _targets(targets)
    if not normalized:
        return None
    pid = os.getpid()
    start_time = _process_start_time(pid)
    if start_time is None:
        raise RuntimeError("cannot identify bridge process for mountpoint lease")
    directory = _lease_dir(profile_id)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{pid}-{uuid.uuid4().hex}.json"
    payload = {
        "version": 1,
        "profile_id": profile_id,
        "pid": pid,
        "start_time": start_time,
        "targets": sorted(normalized),
        "protected_targets": sorted(_targets(protected_targets)),
    }
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    temporary.chmod(0o600)
    os.replace(temporary, path)
    return path


def _read_manifest(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _blocks_removal(relative: PurePosixPath, blockers: set[str]) -> bool:
    for raw in blockers:
        candidate = PurePosixPath(raw)
        if candidate == relative or relative in candidate.parents:
            return True
    return False


def _remove_empty_target(
    private_home: Path, target: str, blockers: set[str]
) -> int:
    relative = PurePosixPath(target)
    if not relative.parts or relative.is_absolute() or ".." in relative.parts:
        return 0
    removed = 0
    current = relative
    while current.parts:
        if _blocks_removal(current, blockers):
            break
        path = private_home.joinpath(*current.parts)
        try:
            path.rmdir()
        except OSError:
            break
        removed += 1
        current = current.parent
        if current == PurePosixPath("."):
            break
    return removed


def _release_manifests(
    profile_id: str,
    released: list[dict],
    *,
    extra_protected: Iterable[str] = (),
) -> int:
    directory = _lease_dir(profile_id)
    active_targets: set[str] = set()
    protected = _targets(extra_protected)
    stale: list[tuple[Path, dict]] = []
    for path in directory.glob("*.json") if directory.is_dir() else ():
        manifest = _read_manifest(path)
        if manifest is None:
            stale.append((path, {}))
            continue
        protected.update(_targets(manifest.get("protected_targets", [])))
        try:
            pid = int(manifest.get("pid", 0))
        except (TypeError, ValueError):
            pid = 0
        if _process_matches(pid, str(manifest.get("start_time", ""))):
            active_targets.update(_targets(manifest.get("targets", [])))
        else:
            stale.append((path, manifest))

    for path, manifest in stale:
        path.unlink(missing_ok=True)
        released.append(manifest)

    candidates: set[str] = set()
    for manifest in released:
        candidates.update(_targets(manifest.get("targets", [])))
        protected.update(_targets(manifest.get("protected_targets", [])))
    candidates.difference_update(active_targets)
    blockers = active_targets | protected
    private_home = PROFILE_DATA_ROOT / profile_id / "home"
    ordered = sorted(
        candidates,
        key=lambda item: len(PurePosixPath(item).parts),
        reverse=True,
    )
    removed = sum(
        _remove_empty_target(private_home, target, blockers) for target in ordered
    )
    try:
        directory.rmdir()
    except OSError:
        pass
    return removed


def release_bridge_lease(path: Path) -> int:
    """Release one bridge lease and remove now-unused empty mountpoints."""

    path = Path(path).resolve()
    profile_id = path.parent.parent.name
    if path.parent != _lease_dir(profile_id).resolve():
        raise ValueError(f"bridge lease is outside its state directory: {path}")
    manifest = _read_manifest(path)
    if manifest is None or str(manifest.get("profile_id")) != profile_id:
        raise ValueError(f"invalid bridge lease manifest: {path}")
    path.unlink(missing_ok=True)
    return _release_manifests(profile_id, [manifest])


def cleanup_stale_bridge_leases(
    profile_id: str, protected_targets: Iterable[str] = ()
) -> int:
    """Collect leases whose bridge process no longer exists."""

    return _release_manifests(profile_id, [], extra_protected=protected_targets)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2 or args[0] != "release":
        print(
            "usage: python -m fingerprint_terminal.bridge_leases release PATH",
            file=sys.stderr,
        )
        return 2
    try:
        release_bridge_lease(Path(args[1]))
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"bridge lease cleanup failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
