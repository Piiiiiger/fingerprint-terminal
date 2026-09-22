"""Strict filesystem/process sandbox construction for Fingerprint Terminal.

The strict sandbox is deliberately layered *inside* the transparent network
namespace.  The outer network supervisor owns the private network/UTS view;
Bubblewrap then replaces the filesystem/process view seen by the actual shell.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import shutil
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence


PROFILE_DATA_ROOT = Path.home() / ".local" / "share" / "fingerprint-terminal" / "profiles"
_USERNAME_RE = re.compile(r"[a-z_][a-z0-9_-]{0,31}")
_MACHINE_ID_RE = re.compile(r"[0-9a-f]{32}")

# Identity files are copied into the sandbox's private /etc tmpfs from inherited
# descriptors rather than bind-mounted, so /proc/self/mountinfo inside the
# sandbox does not list their host-side source paths.  /etc is remounted
# read-only once populated.  procfs cannot hold new files, so boot_id is the one
# remaining bind; --ro-bind-data still keeps its host path out of mountinfo.
_FIRST_INJECTED_FD = 10
_INJECTED_FILES = (
    ("--file", "0444", "machine_id", "/etc/machine-id"),
    ("--file", "0644", "passwd", "/etc/passwd"),
    ("--file", "0644", "group", "/etc/group"),
    ("--file", "0644", "locale_conf", "/etc/locale.conf"),
    ("--file", "0644", "environment", "/etc/environment"),
    ("--file", "0644", "vconsole", "/etc/vconsole.conf"),
    ("--file", "0644", "fstab", "/etc/fstab"),
    ("--file", "0644", "hostname", "/etc/hostname"),
    ("--file", "0644", "hosts", "/etc/hosts"),
    ("--file", "0644", "resolv", "/etc/resolv.conf"),
    ("--ro-bind-data", "0444", "boot_id", "/proc/sys/kernel/random/boot_id"),
)


class SandboxError(RuntimeError):
    """Raised when a strict sandbox cannot be prepared safely."""


def settings(profile: Mapping[str, Any]) -> Mapping[str, Any]:
    raw = profile.get("sandbox", {})
    return raw if isinstance(raw, Mapping) else {}


def is_strict(profile: Mapping[str, Any]) -> bool:
    return str(settings(profile).get("mode", "off") or "off") == "strict"


def clipboard_mode(profile: Mapping[str, Any]) -> str:
    return str(settings(profile).get("clipboard", "off") or "off")


def sandbox_username(profile: Mapping[str, Any]) -> str:
    username = str(settings(profile).get("username", "dev") or "dev")
    if not _USERNAME_RE.fullmatch(username):
        raise SandboxError(f"invalid strict sandbox username: {username!r}")
    return username


def sandbox_home(profile: Mapping[str, Any]) -> str:
    return f"/home/{sandbox_username(profile)}"


def _wayland_clipboard_socket(
    profile: Mapping[str, Any], env: Mapping[str, str]
) -> tuple[Path, str] | None:
    """Return the host Wayland socket and sandbox display name when enabled.

    Strict mode keeps /run private by default. Clipboard integration therefore
    exposes exactly the compositor socket selected by WAYLAND_DISPLAY, rather
    than the whole host runtime directory or D-Bus session bus.
    """

    if clipboard_mode(profile) != "wayland":
        return None

    display = str(env.get("WAYLAND_DISPLAY", "") or "").strip()
    runtime_dir = str(env.get("XDG_RUNTIME_DIR", "") or "").strip()
    if not display or not runtime_dir:
        raise SandboxError(
            "strict Wayland clipboard requires WAYLAND_DISPLAY and XDG_RUNTIME_DIR"
        )
    if "/" in display or display in {".", ".."}:
        raise SandboxError(f"unsafe WAYLAND_DISPLAY for strict clipboard: {display!r}")

    source = (Path(runtime_dir) / display).resolve()
    runtime_root = Path(runtime_dir).resolve()
    if source.parent != runtime_root:
        raise SandboxError(f"unsafe Wayland socket path: {source}")
    if not source.exists() or not source.is_socket():
        raise SandboxError(f"Wayland socket does not exist: {source}")
    return source, display


def _profile_root(profile: Mapping[str, Any]) -> Path:
    return PROFILE_DATA_ROOT / str(profile["id"])


def _host_profile_home(profile: Mapping[str, Any]) -> Path:
    return _profile_root(profile) / "home"


def _sandbox_hostname(profile: Mapping[str, Any], env: Mapping[str, str]) -> str:
    configured = str(env.get("FT_HOSTNAME") or "")
    if configured:
        return configured
    digest = hashlib.sha256(str(profile["id"]).encode("utf-8")).hexdigest()[:6]
    return f"desktop-{digest}"


def _write_if_missing(path: Path, text: str, mode: int = 0o644) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(mode)


def _stable_machine_id(profile: Mapping[str, Any]) -> Path:
    path = _profile_root(profile) / "machine-id"
    value = ""
    try:
        value = path.read_text(encoding="ascii").strip().lower()
    except FileNotFoundError:
        pass
    if not _MACHINE_ID_RE.fullmatch(value):
        path.parent.mkdir(parents=True, exist_ok=True)
        value = secrets.token_hex(16)
        path.write_text(value + "\n", encoding="ascii")
        path.chmod(0o600)
    return path


def _session_boot_id(profile: Mapping[str, Any]) -> Path:
    runtime = _profile_root(profile) / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    path = runtime / "boot-id"
    temporary = runtime / f".boot-id-{os.getpid()}-{secrets.token_hex(4)}"
    temporary.write_text(str(uuid.uuid4()) + "\n", encoding="ascii")
    temporary.chmod(0o600)
    temporary.replace(path)
    return path


def _prepare_home(profile: Mapping[str, Any]) -> Path:
    home = _host_profile_home(profile)
    for relative in (
        ".config",
        ".cache",
        ".local",
        ".local/bin",
        ".local/share",
        ".local/state",
    ):
        (home / relative).mkdir(parents=True, exist_ok=True)

    _write_if_missing(
        home / ".bash_profile",
        '[[ -f "$HOME/.bashrc" ]] && . "$HOME/.bashrc"\n',
        0o600,
    )
    _write_if_missing(
        home / ".bashrc",
        "case $- in\n"
        "  *i*) ;;\n"
        "  *) return ;;\n"
        "esac\n"
        "umask 077\n"
        "PS1='[\\u@\\h \\W]\\$ '\n",
        0o600,
    )
    return home


def _identity_files(
    profile: Mapping[str, Any],
    env: Mapping[str, str],
    shell: str,
) -> dict[str, Path]:
    root = _profile_root(profile)
    root.mkdir(parents=True, exist_ok=True)
    username = sandbox_username(profile)
    home = sandbox_home(profile)
    timezone = str(env.get("TZ") or "UTC")
    locale = str(env.get("LC_ALL") or env.get("LANG") or "en_US.UTF-8")
    hostname = _sandbox_hostname(profile, env)

    passwd = root / "passwd"
    passwd.write_text(
        "root:x:0:0:root:/root:/usr/bin/bash\n"
        f"{username}:x:1000:1000::{home}:{shell}\n"
        "nobody:x:65534:65534:Nobody:/:/usr/bin/nologin\n",
        encoding="utf-8",
    )
    passwd.chmod(0o644)

    group = root / "group"
    group.write_text(
        "root:x:0:\n"
        f"{username}:x:1000:\n"
        "nobody:x:65534:\n",
        encoding="utf-8",
    )
    group.chmod(0o644)

    locale_conf = root / "locale.conf"
    locale_conf.write_text(f"LANG={locale}\nLC_ALL={locale}\n", encoding="utf-8")
    locale_conf.chmod(0o644)

    environment = root / "environment"
    environment.write_text("", encoding="utf-8")
    environment.chmod(0o644)

    vconsole = root / "vconsole.conf"
    vconsole.write_text("", encoding="utf-8")
    vconsole.chmod(0o644)

    fstab = root / "fstab"
    fstab.write_text(
        "# Static information about the filesystems.\n# See fstab(5) for details.\n",
        encoding="utf-8",
    )
    fstab.chmod(0o644)

    hostname_file = root / "strict-hostname"
    hostname_file.write_text(hostname + "\n", encoding="utf-8")
    hostname_file.chmod(0o644)

    hosts = root / "strict-hosts"
    hosts.write_text(
        "127.0.0.1 localhost\n"
        f"127.0.1.1 {hostname}\n"
        "::1 localhost ip6-localhost ip6-loopback\n",
        encoding="utf-8",
    )
    hosts.chmod(0o644)

    zoneinfo = Path("/usr/share/zoneinfo") / timezone
    if not zoneinfo.is_file():
        raise SandboxError(f"strict sandbox timezone is unavailable: {timezone!r}")

    resolv = Path(str(env.get("FT_RESOLV_CONF") or ""))
    if not resolv.is_file():
        raise SandboxError("strict sandbox requires the transparent DNS runtime file")

    return {
        "machine_id": _stable_machine_id(profile),
        "boot_id": _session_boot_id(profile),
        "passwd": passwd,
        "group": group,
        "locale_conf": locale_conf,
        "environment": environment,
        "vconsole": vconsole,
        "fstab": fstab,
        "hostname": hostname_file,
        "hosts": hosts,
        "zoneinfo": zoneinfo,
        "resolv": resolv,
    }


def _append_safe_etc_bindings(command: list[str]) -> None:
    """Expose only non-personal system configuration needed by CLI tools."""

    for path in (
        "/etc/nsswitch.conf",
        "/etc/gai.conf",
        "/etc/host.conf",
        "/etc/protocols",
        "/etc/services",
        "/etc/shells",
        "/etc/profile",
        "/etc/bash.bashrc",
        "/etc/inputrc",
        "/etc/ld.so.cache",
        "/etc/ld.so.conf",
    ):
        source = Path(path)
        if source.is_file():
            command.extend(["--ro-bind", path, path])

    for path in (
        "/etc/ssl",
        "/etc/ca-certificates",
        "/etc/pki",
        "/etc/ld.so.conf.d",
    ):
        source = Path(path)
        if source.is_dir():
            command.extend(["--ro-bind", path, path])


def _share_entries(profile: Mapping[str, Any]) -> list[tuple[Path, str, str]]:
    result: list[tuple[Path, str, str]] = []
    home = sandbox_home(profile)
    for raw in settings(profile).get("shares", []) or []:
        source_raw = str(raw.get("source", "") or "")
        target_raw = str(raw.get("target", "") or "")
        mode = str(raw.get("mode", "rw") or "rw")
        source = Path(os.path.expandvars(source_raw)).expanduser().resolve()
        if not source.exists() or not source.is_dir():
            raise SandboxError(f"strict share source is not a directory: {source}")
        if source == Path.home().resolve() or source == Path("/"):
            raise SandboxError("strict shares may not expose the host home or filesystem root")

        relative = PurePosixPath(target_raw)
        if relative.is_absolute() or not relative.parts or ".." in relative.parts:
            raise SandboxError(
                f"strict share target must be a relative path inside sandbox HOME: {target_raw!r}"
            )
        if mode not in {"rw", "ro"}:
            raise SandboxError(f"strict share mode must be rw or ro: {mode!r}")
        target = str(PurePosixPath(home) / relative)
        result.append((source, target, mode))
    return result


def sandbox_cwd(profile: Mapping[str, Any]) -> str:
    """Map the configured host-side cwd into the strict sandbox."""

    raw = str(profile.get("terminal", {}).get("cwd", "~") or "~")
    sandbox_root = PurePosixPath(sandbox_home(profile))
    if raw in {"~", "$HOME", "${HOME}"}:
        return str(sandbox_root)

    host_cwd = Path(os.path.expandvars(raw)).expanduser().resolve()
    if host_cwd == Path.home().resolve():
        return str(sandbox_root)
    for source, target, _mode in _share_entries(profile):
        try:
            relative = host_cwd.relative_to(source)
        except ValueError:
            continue
        return str(PurePosixPath(target) / PurePosixPath(relative.as_posix()))
    return str(sandbox_root)


def _safe_environment(
    profile: Mapping[str, Any], env: Mapping[str, str], shell: str
) -> dict[str, str]:
    username = sandbox_username(profile)
    home = sandbox_home(profile)
    safe = {
        "HOME": home,
        "USER": username,
        "LOGNAME": username,
        "SHELL": shell,
        "PATH": f"{home}/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/bin:/bin",
        "XDG_CONFIG_HOME": f"{home}/.config",
        "XDG_CACHE_HOME": f"{home}/.cache",
        "XDG_DATA_HOME": f"{home}/.local/share",
        "XDG_STATE_HOME": f"{home}/.local/state",
        "XDG_RUNTIME_DIR": "/run/user/1000",
        "TMPDIR": "/tmp",
        "TZ": str(env.get("TZ") or "UTC"),
        "LANG": str(env.get("LANG") or "en_US.UTF-8"),
        "LC_ALL": str(env.get("LC_ALL") or env.get("LANG") or "en_US.UTF-8"),
        "LANGUAGE": str(env.get("LANGUAGE") or "en_US"),
        "HOSTNAME": _sandbox_hostname(profile, env),
        "TERM": str(env.get("TERM") or "xterm-256color"),
        "COLORTERM": str(env.get("COLORTERM") or "truecolor"),
    }

    wayland = _wayland_clipboard_socket(profile, env)
    if wayland is not None:
        _source, display = wayland
        safe["WAYLAND_DISPLAY"] = display

    reserved = {
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "PATH",
        "PWD",
        "OLDPWD",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
        "XDG_RUNTIME_DIR",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
    }
    for key, value in (profile.get("environment", {}) or {}).items():
        if key in reserved or value is None:
            continue
        safe[str(key)] = str(value)
    return safe


def strict_command(
    profile: Mapping[str, Any],
    child: Sequence[str],
    env: Mapping[str, str],
) -> list[str]:
    """Return a Bubblewrap command for the strict fingerprint filesystem view."""

    if not is_strict(profile):
        return [str(part) for part in child]
    bwrap = shutil.which("bwrap")
    if not bwrap:
        raise SandboxError("strict sandbox requires bubblewrap (bwrap)")
    if not child:
        raise SandboxError("strict sandbox child command is empty")

    shell = str(child[0])
    host_home = _prepare_home(profile)
    files = _identity_files(profile, env, shell)
    username = sandbox_username(profile)
    home = sandbox_home(profile)
    hostname = _sandbox_hostname(profile, env)
    wayland = _wayland_clipboard_socket(profile, env)
    safe_env = _safe_environment(profile, env, shell)

    command: list[str] = [
        bwrap,
        "--die-with-parent",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--unshare-cgroup-try",
        "--hostname",
        hostname,
        "--tmpfs",
        "/",
        "--ro-bind",
        "/usr",
        "/usr",
        "--tmpfs",
        "/etc",
        "--symlink",
        "usr/bin",
        "/bin",
        "--symlink",
        "usr/bin",
        "/sbin",
        "--symlink",
        "usr/lib",
        "/lib",
        "--symlink",
        "usr/lib",
        "/lib64",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/run",
        "--dir",
        "/run/user",
        "--dir",
        "/run/user/1000",
        "--tmpfs",
        "/tmp",
        "--dir",
        "/var",
        "--dir",
        "/var/tmp",
        "--dir",
        "/var/log",
        "--dir",
        "/var/cache",
        "--dir",
        "/var/lib",
        "--symlink",
        "../run",
        "/var/run",
        "--dir",
        "/sys",
        "--dir",
        "/home",
        "--dir",
        home,
        "--bind",
        str(host_home),
        home,
        "--symlink",
        "../" + str(files["zoneinfo"]).lstrip("/"),
        "/etc/localtime",
    ]

    injected: list[str] = []
    for fd, (option, perms, key, target) in enumerate(
        _INJECTED_FILES, start=_FIRST_INJECTED_FD
    ):
        command.extend(["--perms", perms, option, str(fd), target])
        injected.append(str(files[key]))

    if wayland is not None:
        source, display = wayland
        command.extend(
            [
                "--ro-bind",
                str(source),
                f"/run/user/1000/{display}",
            ]
        )

    _append_safe_etc_bindings(command)
    command.extend(["--remount-ro", "/etc"])

    # Re-expose only explicitly approved host directories under the private
    # profile HOME.  Everything else from /home is absent.
    for source, target, mode in _share_entries(profile):
        target_relative = PurePosixPath(target).relative_to(PurePosixPath(home))
        target_host = host_home / Path(target_relative.as_posix())
        target_host.mkdir(parents=True, exist_ok=True)
        command.extend([
            "--ro-bind" if mode == "ro" else "--bind",
            str(source),
            target,
        ])

    command.append("--clearenv")
    for key, value in safe_env.items():
        command.extend(["--setenv", key, value])
    command.extend(["--chdir", sandbox_cwd(profile)])
    command.extend(["--", *[str(part) for part in child]])
    return _with_injected_descriptors(command, injected)


def _with_injected_descriptors(command: list[str], sources: Sequence[str]) -> list[str]:
    """Open each injected source on its descriptor in the final exec of bwrap.

    Opening them here rather than in the CLI keeps the descriptors away from the
    network supervisor's other children.  bwrap closes each one after copying.
    """

    bash = shutil.which("bash") or "/usr/bin/bash"
    redirects = " ".join(
        f'{fd}<"${{{index}}}"'
        for index, fd in enumerate(
            range(_FIRST_INJECTED_FD, _FIRST_INJECTED_FD + len(sources)), start=1
        )
    )
    script = f'exec "${{@:{len(sources) + 1}}}" {redirects}'
    return [bash, "-c", script, "bwrap", *sources, *command]

