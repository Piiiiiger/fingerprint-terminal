"""Provision and locate a profile-owned /usr for strict sandboxes."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Mapping

from .profiles import PROFILE_DATA_ROOT


PACKAGES = (
    "filesystem", "glibc", "bash", "coreutils", "findutils", "grep", "sed",
    "gawk", "which", "util-linux", "procps-ng", "iproute2", "openssh",
    "curl", "ca-certificates", "python", "git", "nodejs", "npm",
    "cloudflared", "fontconfig", "noto-fonts", "ttf-liberation", "less",
    "ripgrep", "tar", "gzip", "xz", "unzip", "jq", "wl-clipboard",
)


class SystemViewError(RuntimeError):
    """Raised when a private system view is missing or cannot be prepared."""


def mode(profile: Mapping[str, Any]) -> str:
    return str(profile.get("sandbox", {}).get("system", "host") or "host")


def _system_dir(profile: Mapping[str, Any]) -> Path:
    return PROFILE_DATA_ROOT / str(profile["id"]) / "system"


def system_root(profile: Mapping[str, Any]) -> Path:
    """Return the completed, active root or fail before starting a shell."""

    system_dir = _system_dir(profile)
    releases = system_dir / "releases"
    current = system_dir / "current"
    if not current.is_symlink():
        raise SystemViewError(
            f"private /usr is not prepared for {profile['id']}; "
            f"run fingerprint-terminal prepare-system {profile['id']}"
        )
    try:
        root = current.resolve(strict=True)
    except OSError as exc:
        raise SystemViewError(f"private /usr link is broken: {current}") from exc
    if root.parent != releases.resolve() or not root.is_dir():
        raise SystemViewError(f"private /usr link points outside profile releases: {current}")
    for relative in (
        "ready.json",
        "usr/bin/bash",
        "usr/bin/python3",
        "usr/bin/wl-paste",
        "usr/share/zoneinfo/America/Los_Angeles",
        "usr/share/zoneinfo/Asia/Singapore",
        "usr/share/wayland-sessions/niri.desktop",
    ):
        if not (root / relative).is_file():
            raise SystemViewError(f"private /usr is incomplete: {root / relative}")
    return root


def _copy_pacman_state(stage: Path) -> None:
    keyring = stage.parent / "gnupg"
    keyring.mkdir(mode=0o700)
    host_keys = Path("/etc/pacman.d/gnupg")
    for name in ("pubring.gpg", "trustdb.gpg", "gpg.conf"):
        source = host_keys / name
        if not source.is_file():
            raise SystemViewError(f"pacman keyring file is unavailable: {source}")
        shutil.copy2(source, keyring / name)

    sync = stage / "var/lib/pacman/sync"
    sync.mkdir(parents=True)
    databases = list(Path("/var/lib/pacman/sync").glob("*.db"))
    if not databases:
        raise SystemViewError("pacman sync database is missing; update it before preparing /usr")
    for source in databases:
        shutil.copy2(source, sync / source.name)


def _remove_build_tree(path: Path) -> None:
    """Package hooks can leave root-owned-style directories without write bits."""

    for directory, children, _files in os.walk(path, followlinks=False):
        for child in children:
            item = Path(directory) / child
            if not item.is_symlink():
                item.chmod(item.stat().st_mode | stat.S_IWUSR | stat.S_IXUSR)
    shutil.rmtree(path)


def prepare_system_view(profile: Mapping[str, Any], *, refresh: bool = False) -> Path:
    """Build a signed Arch package view without changing the host /usr.

    A new release is built beside the active one. The symlink switch is atomic,
    so running sandboxes retain their old mount and a failed build changes none.
    """

    if mode(profile) != "private":
        raise SystemViewError(f"profile {profile['id']} does not request a private /usr")
    if not refresh:
        try:
            return system_root(profile)
        except SystemViewError:
            pass
    for binary in ("pacman", "unshare", "chroot"):
        if not shutil.which(binary):
            raise SystemViewError(f"preparing private /usr requires {binary}")

    system_dir = _system_dir(profile)
    releases = system_dir / "releases"
    releases.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=".building-", dir=releases))
    stage = work / "root"
    stage.mkdir()
    try:
        (stage / "var/lib/pacman").mkdir(parents=True)
        (stage / "var/log").mkdir(parents=True)
        (work / "cache").mkdir()
        _copy_pacman_state(stage)
        command = [
            "unshare", "--user", "--map-root-user", "--mount",
            "--propagation", "private", "--", "pacman", "-S",
            "--root", str(stage),
            "--gpgdir", str(work / "gnupg"),
            "--cachedir", str(work / "cache"),
            "--cachedir", "/var/cache/pacman/pkg",
            "--noconfirm", "--needed", "--disable-sandbox", *PACKAGES,
        ]
        try:
            subprocess.run(command, check=True)
            # The package hooks may skip locale generation in a minimal root.
            for locale in ("en_US", "en_SG"):
                subprocess.run(
                    ["unshare", "--user", "--map-root-user", "--mount",
                     "--propagation", "private", "--", "chroot", str(stage),
                     "/usr/bin/localedef", "-i", locale, "-f", "UTF-8", f"{locale}.UTF-8"],
                    check=True,
                )
        except (OSError, subprocess.CalledProcessError) as exc:
            raise SystemViewError(f"could not prepare private /usr: {exc}") from exc

        usr = stage / "usr"
        for relative in ("share/applications", "share/xsessions", "share/wayland-sessions"):
            path = usr / relative
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            elif path.exists() or path.is_symlink():
                path.unlink()
        sessions = usr / "share/wayland-sessions"
        sessions.mkdir(parents=True)
        (sessions / "niri.desktop").write_text(
            "[Desktop Entry]\nName=Niri\nComment=Wayland compositor\n"
            "Exec=niri\nType=Application\n",
            encoding="utf-8",
        )
        for relative in ("bin/bash", "bin/python3", "bin/ssh", "bin/cloudflared",
                         "bin/wl-paste",
                         "share/zoneinfo/America/Los_Angeles",
                         "share/zoneinfo/Asia/Singapore"):
            if not (usr / relative).is_file():
                raise SystemViewError(f"private /usr package is missing: {relative}")
        (stage / "ready.json").write_text(
            json.dumps({"version": 1, "packages": PACKAGES}, indent=2) + "\n",
            encoding="utf-8",
        )

        release_name = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
        release = releases / release_name
        os.replace(stage, release)
        link = system_dir / f".current-{uuid.uuid4().hex}"
        link.symlink_to(Path("releases") / release_name)
        os.replace(link, system_dir / "current")
        return release
    finally:
        if work.exists():
            _remove_build_tree(work)
