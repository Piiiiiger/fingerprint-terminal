#!/usr/bin/python3
"""A small vault-file command for terminal integrations."""

from __future__ import annotations

import os
import sys
from pathlib import Path, PurePosixPath


def vault_root() -> Path:
    current = Path.cwd().resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".obsidian").is_dir():
            return candidate
    raise ValueError("No Obsidian vault is available from the current directory.")


def run(argv: list[str], root: Path) -> int:
    commands = [arg for arg in argv if "=" not in arg]
    options = dict(arg.split("=", 1) for arg in argv if "=" in arg)
    if len(commands) != 1 or commands[0] != "delete" or set(options) - {"vault", "path"}:
        print("Supported vault command: obsidian [vault=<directory-name>] delete path=<relative-file>", file=sys.stderr)
        return 2
    if options.get("vault", root.resolve().name) != root.resolve().name:
        print("The requested vault does not match the current vault directory.", file=sys.stderr)
        return 2

    raw_path = options.get("path", "")
    relative = PurePosixPath(raw_path)
    if (
        not relative.parts
        or relative.is_absolute()
        or ".." in relative.parts
        or relative.parts[0] == ".trash"
    ):
        print("path must name a file inside the vault.", file=sys.stderr)
        return 2

    root = root.resolve()
    source = root / Path(relative.as_posix())
    if not source.is_file() or source.is_symlink() or not source.resolve().is_relative_to(root):
        print(f"File not found or unsafe: {raw_path}", file=sys.stderr)
        return 1

    trash = root / ".trash"
    if trash.is_symlink():
        print("Vault trash is unsafe.", file=sys.stderr)
        return 1
    destination = trash / Path(relative.as_posix())
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.parent.resolve().is_relative_to(trash.resolve()):
        print("Vault trash destination is unsafe.", file=sys.stderr)
        return 1
    if destination.exists():
        for number in range(1, 10000):
            candidate = destination.with_name(
                f"{destination.stem}~{number}{destination.suffix}"
            )
            if not candidate.exists():
                destination = candidate
                break
        else:
            print("No available trash filename.", file=sys.stderr)
            return 1

    os.rename(source, destination)
    print(f"Moved to vault trash: {destination.relative_to(root)}")
    return 0


def main() -> int:
    try:
        return run(sys.argv[1:], vault_root())
    except ValueError as error:
        print(error, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
