"""Command-line entrypoint for Fingerprint Terminal."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Sequence

from . import __version__
from .identity import IdentityError, detect_exit_identity
from .network import NetworkError, TRANSPARENT_DEPENDENCIES, isolation_command, is_transparent, prepare_transparent_environment
from .sandbox import SandboxError, is_strict, strict_command
from .profiles import (
    ProfileError,
    build_environment,
    clone_profile,
    config_path,
    load_store,
    resolved_cwd,
    resolved_shell,
)
from .terminal import printable_command, terminal_command


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def self_command() -> list[str]:
    wrapper = project_root() / "bin" / "fingerprint-terminal"
    if wrapper.exists():
        return [str(wrapper)]
    return [sys.executable, "-m", "fingerprint_terminal.cli"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fingerprint-terminal",
        description="Launch a real local terminal with a named environment/profile.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="subcommand", required=True)

    sub.add_parser("list", help="list configured profiles")

    show = sub.add_parser("show", help="show one profile")
    show.add_argument("profile")

    launch = sub.add_parser("launch", help="open a profile in the native terminal")
    launch.add_argument("profile", nargs="?", default="local")
    launch.add_argument("--dry-run", action="store_true", help="print the terminal command without launching it")

    shell = sub.add_parser("shell", help="internal: apply a profile and execute its shell")
    shell.add_argument("--profile", required=True)
    shell.add_argument("--command", help="run a command through the profile shell instead of opening interactively")

    clone = sub.add_parser("clone", help="clone an existing profile")
    clone.add_argument("source")
    clone.add_argument("new_id")
    clone.add_argument("--name")

    identity = sub.add_parser("identity", help="resolve the real proxy exit and its IP-derived identity")
    identity.add_argument("profile", nargs="?", default="auto-ip")

    sub.add_parser("config", help="print the active profile config path")
    sub.add_parser("doctor", help="check native terminal/profile prerequisites")
    sub.add_parser("manager", help="open the GTK profile manager")
    return parser


def cmd_list() -> int:
    store = load_store()
    for profile in store.profiles:
        terminal = profile.get("terminal", {})
        proxy = profile.get("proxy", {})
        network = profile.get("network", {})
        sandbox = profile.get("sandbox", {})
        print(
            f"{profile['id']:<18} {profile['name']:<24} "
            f"terminal={terminal.get('backend', 'auto'):<8} "
            f"network={network.get('mode', 'inherit'):<11} "
            f"sandbox={sandbox.get('mode', 'off'):<6} "
            f"proxy={proxy.get('mode', 'inherit')}"
        )
    return 0


def cmd_show(profile_id: str) -> int:
    profile = load_store().get(profile_id)
    print(json.dumps(profile, ensure_ascii=False, indent=2))
    return 0


def launch_command(profile_id: str) -> tuple[list[str], dict[str, str]]:
    store = load_store()
    profile = store.get(profile_id)
    inner = [*self_command(), "shell", "--profile", profile_id]
    command = terminal_command(profile, inner)
    env = dict(os.environ)
    env["FT_PROFILES_FILE"] = str(store.path)
    return command, env


def cmd_launch(profile_id: str, *, dry_run: bool) -> int:
    command, env = launch_command(profile_id)
    if dry_run:
        print(printable_command(command))
        return 0
    subprocess.Popen(command, env=env, start_new_session=True)
    return 0


def cmd_shell(profile_id: str, command: str | None) -> int:
    profile = load_store().get(profile_id)
    shell = resolved_shell(profile)
    strict = is_strict(profile)
    cwd = None if strict else resolved_cwd(profile)
    env = build_environment(profile)
    if cwd is not None:
        os.chdir(cwd)
    else:
        os.chdir("/")

    if command is not None:
        child = [shell, "-lc", command]
    else:
        child = [shell, "-l"]

    if is_transparent(profile):
        env, identity = prepare_transparent_environment(profile, env)
        print(
            "Fingerprint Terminal: "
            f"exit={identity['ip']} country={identity['country_code']} "
            f"timezone={identity['timezone']} locale={identity['locale']}",
            file=sys.stderr,
        )
        if strict:
            child = strict_command(profile, child, env)
        isolated = isolation_command(child)
        os.execvpe(isolated[0], isolated, env)
        raise AssertionError("os.execvpe returned unexpectedly")

    if command is not None:
        os.execve(shell, [shell, "-lc", command], env)
    shell_name = Path(shell).name
    argv0 = f"-{shell_name}"
    os.execve(shell, [argv0, "-l"], env)
    raise AssertionError("os.execve returned unexpectedly")


def cmd_identity(profile_id: str) -> int:
    profile = load_store().get(profile_id)
    if not is_transparent(profile):
        raise ProfileError(
            f"profile {profile_id!r} is not configured for transparent networking"
        )
    identity = detect_exit_identity(profile.get("network", {}))
    print(json.dumps(identity, ensure_ascii=False, indent=2))
    return 0


def cmd_clone(source: str, new_id: str, name: str | None) -> int:
    store = load_store()
    profile = clone_profile(store, source, new_id, name=name)
    print(f"created {profile['id']!r} in {store.path}")
    return 0


def cmd_doctor() -> int:
    store = load_store()
    print(f"Fingerprint Terminal {__version__}")
    print(f"Profiles: {store.path}")
    print(f"Host SHELL: {os.environ.get('SHELL') or '(unset)'}")
    failures = 0
    doctor_binaries = ("kitty", "konsole", "bwrap", "sing-box") + TRANSPARENT_DEPENDENCIES
    for binary in doctor_binaries:
        found = shutil.which(binary)
        print(f"{binary:9}: {found or 'not found'}")
    for profile in store.profiles:
        try:
            shell = resolved_shell(profile)
            cwd = resolved_cwd(profile)
            terminal_command(profile, ["true"])
            print(f"profile {profile['id']}: OK (shell={shell}, cwd={cwd})")
        except ProfileError as exc:
            failures += 1
            print(f"profile {profile.get('id', '?')}: ERROR: {exc}")
    return 1 if failures else 0


def cmd_manager() -> int:
    try:
        from .manager import main as manager_main
    except Exception as exc:
        print(f"cannot load GTK manager: {exc}", file=sys.stderr)
        return 1
    return manager_main([])


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.subcommand == "list":
            return cmd_list()
        if args.subcommand == "show":
            return cmd_show(args.profile)
        if args.subcommand == "launch":
            return cmd_launch(args.profile, dry_run=args.dry_run)
        if args.subcommand == "shell":
            return cmd_shell(args.profile, args.command)
        if args.subcommand == "clone":
            return cmd_clone(args.source, args.new_id, args.name)
        if args.subcommand == "identity":
            return cmd_identity(args.profile)
        if args.subcommand == "config":
            print(config_path())
            return 0
        if args.subcommand == "doctor":
            return cmd_doctor()
        if args.subcommand == "manager":
            return cmd_manager()
    except (ProfileError, NetworkError, IdentityError, SandboxError) as exc:
        print(f"fingerprint-terminal: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

