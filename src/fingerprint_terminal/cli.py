"""Command-line entrypoint for Fingerprint Terminal."""

from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Sequence

from . import __version__
from .conversations import ConversationError, ConversationManager
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

    bridge = sub.add_parser(
        "bridge",
        help="run an argv-preserving child inside a profile for GUI/stdio integrations",
    )
    bridge.add_argument("--profile", default="strict-auto-ip")
    bridge.add_argument(
        "--cwd",
        help="host working directory to map through an approved strict share",
    )
    bridge.add_argument("command", nargs=argparse.REMAINDER)

    clone = sub.add_parser("clone", help="clone an existing profile")
    clone.add_argument("source")
    clone.add_argument("new_id")
    clone.add_argument("--name")

    identity = sub.add_parser("identity", help="resolve the real proxy exit and its IP-derived identity")
    identity.add_argument("profile", nargs="?", default="auto-ip")

    sub.add_parser("config", help="print the active profile config path")
    sub.add_parser("doctor", help="check native terminal/profile prerequisites")
    sub.add_parser("manager", help="open the GTK profile manager")

    conversations = sub.add_parser(
        "conversations", help="manage Claude Code and Codex local conversations"
    )
    conversations_sub = conversations.add_subparsers(
        dest="conversation_command", required=True
    )
    conversations_sub.add_parser(
        "cleanup", help="permanently delete conversation trash older than 14 days"
    )
    conversations_sub.add_parser(
        "status", help="show conversation and trash counts without message bodies"
    )
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
    if profile_id == "strict-auto-ip":
        # Opportunistic retention enforcement in addition to the daily timer.
        # Never block terminal startup if stale trash cleanup itself fails.
        try:
            ConversationManager(profile_id).cleanup_expired()
        except Exception:
            pass
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


def cmd_bridge(profile_id: str, cwd: str | None, command: Sequence[str]) -> int:
    """Execute *command* inside a profile without inserting a shell or terminal.

    This entrypoint is intended for GUI integrations that communicate with a CLI
    over stdin/stdout (for example Codex app-server or Claude Code stream-json).
    It deliberately emits no status text of its own and preserves the caller's
    stdio file descriptors through exec.
    """

    child = [str(part) for part in command]
    if child and child[0] == "--":
        child = child[1:]
    if not child:
        raise ProfileError("bridge requires a child command after --")

    profile = copy.deepcopy(load_store().get(profile_id))
    strict = is_strict(profile)
    if cwd:
        host_cwd = Path(cwd).expanduser().resolve()
        if not host_cwd.is_dir():
            raise ProfileError(f"bridge cwd does not exist: {host_cwd}")

        # GUI integrations commonly pass absolute vault paths to their agents.
        # If the cwd is already covered by an approved strict share below the
        # real host HOME, add an ephemeral duplicate bind at the same relative
        # path inside the private HOME.  This exposes no new host content, but
        # keeps /home/<user>/... paths identical on both sides of the bridge.
        if strict:
            host_home = Path.home().resolve()
            shares = list(profile.get("sandbox", {}).get("shares", []) or [])
            for share in shares:
                source = Path(
                    os.path.expandvars(str(share.get("source", "")))
                ).expanduser().resolve()
                try:
                    host_cwd.relative_to(source)
                    source_relative = source.relative_to(host_home)
                except ValueError:
                    continue
                if source_relative.parts:
                    duplicate = {
                        "source": str(source),
                        "target": source_relative.as_posix(),
                        "mode": str(share.get("mode", "rw")),
                    }
                    sandbox = dict(profile.get("sandbox", {}))
                    sandbox["shares"] = [duplicate, *shares]
                    profile["sandbox"] = sandbox
                break
        profile.setdefault("terminal", {})["cwd"] = str(host_cwd)

    env = build_environment(profile)
    env["FT_QUIET"] = "1"
    if not strict and cwd:
        os.chdir(Path(cwd).expanduser().resolve())
    else:
        os.chdir("/")

    if is_transparent(profile):
        env, _identity = prepare_transparent_environment(profile, env)
        if strict:
            child = strict_command(profile, child, env)
        isolated = isolation_command(child)
        os.execvpe(isolated[0], isolated, env)
        raise AssertionError("os.execvpe returned unexpectedly")

    if strict:
        child = strict_command(profile, child, env)
    os.execvpe(child[0], child, env)
    raise AssertionError("os.execvpe returned unexpectedly")


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


def cmd_conversations(command: str) -> int:
    manager = ConversationManager("strict-auto-ip")
    if command == "cleanup":
        removed = manager.cleanup_expired()
        print(f"conversation trash cleanup: removed={removed}")
        return 0
    if command == "status":
        active = manager.discover()
        trash = manager.trash_entries()
        claude = sum(1 for item in active if item.provider == "claude")
        codex = sum(1 for item in active if item.provider == "codex")
        print(
            f"active={len(active)} claude={claude} codex={codex} "
            f"trash={len(trash)} retention_days=14"
        )
        return 0
    return 2


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
        if args.subcommand == "bridge":
            return cmd_bridge(args.profile, args.cwd, args.command)
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
        if args.subcommand == "conversations":
            return cmd_conversations(args.conversation_command)
    except (ProfileError, NetworkError, IdentityError, SandboxError, ConversationError) as exc:
        print(f"fingerprint-terminal: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

