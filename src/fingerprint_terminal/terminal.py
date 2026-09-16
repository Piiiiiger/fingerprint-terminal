"""Native terminal adapter construction."""

from __future__ import annotations

import os
import shlex
import shutil
from pathlib import Path
from typing import Any, Mapping, Sequence

from .profiles import ProfileError, resolved_cwd


def resolve_backend(requested: str = "auto") -> tuple[str, str]:
    if requested == "auto":
        override = os.environ.get("FT_TERMINAL", "").strip()
        if override:
            parts = shlex.split(override)
            if not parts:
                raise ProfileError("FT_TERMINAL is empty after parsing")
            executable = shutil.which(parts[0]) or parts[0]
            return "custom", executable
        for backend in ("kitty", "konsole"):
            executable = shutil.which(backend)
            if executable:
                return backend, executable
        raise ProfileError("no supported terminal found; install kitty/konsole or set FT_TERMINAL")

    if requested not in {"kitty", "konsole"}:
        raise ProfileError(f"unsupported terminal backend: {requested}")
    executable = shutil.which(requested)
    if not executable:
        raise ProfileError(f"terminal backend not found in PATH: {requested}")
    return requested, executable


def terminal_command(
    profile: Mapping[str, Any],
    shell_command: Sequence[str],
) -> list[str]:
    terminal = profile.get("terminal", {})
    requested = str(terminal.get("backend", "auto") or "auto")
    backend, executable = resolve_backend(requested)
    cwd = str(resolved_cwd(profile))
    title = f"Fingerprint Terminal — {profile.get('name', profile.get('id', 'profile'))}"

    if backend == "kitty":
        return [
            executable,
            "--class",
            "fingerprint-terminal",
            "--title",
            title,
            "--directory",
            cwd,
            *shell_command,
        ]

    if backend == "konsole":
        return [
            executable,
            "--workdir",
            cwd,
            "-p",
            f"tabtitle={title}",
            "-e",
            *shell_command,
        ]

    # FT_TERMINAL is intentionally a minimal escape hatch. Arguments can be
    # supplied in FT_TERMINAL; the profile shell command is appended.
    override = shlex.split(os.environ.get("FT_TERMINAL", ""))
    if not override:
        override = [executable]
    return [*override, *shell_command]


def printable_command(command: Sequence[str]) -> str:
    return shlex.join([str(part) for part in command])

