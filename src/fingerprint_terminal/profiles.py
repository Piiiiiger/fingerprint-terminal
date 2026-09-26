"""Profile storage, validation, and child environment construction."""

from __future__ import annotations

import copy
import ipaddress
import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any, Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_PROFILES = PROJECT_ROOT / "config" / "profiles.json"
DEFAULT_CONFIG = Path.home() / ".config" / "fingerprint-terminal" / "profiles.json"
PROFILE_DATA_ROOT = Path.home() / ".local" / "share" / "fingerprint-terminal" / "profiles"

_PROFILE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_PROXY_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
)


class ProfileError(ValueError):
    """Raised when profile storage or data is invalid."""


@dataclass(frozen=True)
class ProfileStore:
    path: Path
    document: dict[str, Any]

    @property
    def profiles(self) -> list[dict[str, Any]]:
        return list(self.document.get("profiles", []))

    def get(self, profile_id: str) -> dict[str, Any]:
        for profile in self.profiles:
            if profile.get("id") == profile_id:
                return copy.deepcopy(profile)
        available = ", ".join(p.get("id", "?") for p in self.profiles)
        raise ProfileError(f"unknown profile {profile_id!r}; available: {available or '(none)'}")

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(self.document, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.path)


def config_path() -> Path:
    override = os.environ.get("FT_PROFILES_FILE", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return DEFAULT_CONFIG


def ensure_config(path: Path | None = None) -> Path:
    target = path or config_path()
    if target.exists():
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    if not EXAMPLE_PROFILES.exists():
        raise ProfileError(f"example profiles missing: {EXAMPLE_PROFILES}")
    shutil.copy2(EXAMPLE_PROFILES, target)
    return target


def load_store(path: Path | None = None, *, create: bool = True) -> ProfileStore:
    target = path or config_path()
    if create:
        target = ensure_config(target)
    if not target.exists():
        raise ProfileError(f"profile config not found: {target}")
    try:
        document = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ProfileError(f"invalid JSON in {target}: {exc}") from exc
    validate_document(document)
    return ProfileStore(target, document)


def validate_document(document: Any) -> None:
    if not isinstance(document, dict):
        raise ProfileError("profile document must be a JSON object")
    if document.get("version") != 1:
        raise ProfileError("profile document version must be 1")
    profiles = document.get("profiles")
    if not isinstance(profiles, list) or not profiles:
        raise ProfileError("profile document must contain a non-empty profiles list")
    seen: set[str] = set()
    for profile in profiles:
        validate_profile(profile)
        profile_id = profile["id"]
        if profile_id in seen:
            raise ProfileError(f"duplicate profile id: {profile_id}")
        seen.add(profile_id)


def validate_profile(profile: Any) -> None:
    if not isinstance(profile, dict):
        raise ProfileError("each profile must be a JSON object")
    profile_id = profile.get("id")
    if not isinstance(profile_id, str) or not _PROFILE_ID.fullmatch(profile_id):
        raise ProfileError(
            "profile id must match [a-z0-9][a-z0-9._-]{0,63}"
        )
    name = profile.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ProfileError(f"profile {profile_id!r} needs a non-empty name")

    terminal = profile.get("terminal", {})
    if not isinstance(terminal, dict):
        raise ProfileError(f"profile {profile_id!r}: terminal must be an object")
    backend = terminal.get("backend", "auto")
    if backend not in {"auto", "kitty", "konsole"}:
        raise ProfileError(
            f"profile {profile_id!r}: terminal.backend must be auto, kitty, or konsole"
        )

    identity = profile.get("identity", {})
    if not isinstance(identity, dict):
        raise ProfileError(f"profile {profile_id!r}: identity must be an object")

    environment = profile.get("environment", {})
    if not isinstance(environment, dict):
        raise ProfileError(f"profile {profile_id!r}: environment must be an object")
    for key, value in environment.items():
        if not isinstance(key, str) or not key or "=" in key or "\x00" in key:
            raise ProfileError(f"profile {profile_id!r}: invalid environment key {key!r}")
        if value is not None and not isinstance(value, (str, int, float, bool)):
            raise ProfileError(
                f"profile {profile_id!r}: environment value for {key!r} must be scalar or null"
            )

    proxy = profile.get("proxy", {})
    if not isinstance(proxy, dict):
        raise ProfileError(f"profile {profile_id!r}: proxy must be an object")
    if proxy.get("mode", "inherit") not in {"inherit", "off", "environment"}:
        raise ProfileError(
            f"profile {profile_id!r}: proxy.mode must be inherit, off, or environment"
        )

    network = profile.get("network", {})
    if not isinstance(network, dict):
        raise ProfileError(f"profile {profile_id!r}: network must be an object")
    network_mode = network.get("mode", "inherit")
    if network_mode not in {"inherit", "transparent"}:
        raise ProfileError(
            f"profile {profile_id!r}: network.mode must be inherit or transparent"
        )
    if network_mode == "transparent":
        proxy_host = str(network.get("proxy_host", "127.0.0.1"))
        if proxy_host not in {"127.0.0.1", "localhost", "::1"}:
            raise ProfileError(
                f"profile {profile_id!r}: transparent proxy_host must be local"
            )
        try:
            proxy_port = int(network.get("proxy_port", 7898))
        except (TypeError, ValueError) as exc:
            raise ProfileError(
                f"profile {profile_id!r}: network.proxy_port must be an integer"
            ) from exc
        if not 1 <= proxy_port <= 65535:
            raise ProfileError(
                f"profile {profile_id!r}: network.proxy_port out of range"
            )
        if network.get("proxy_protocol", "mixed") not in {"mixed", "http", "socks5"}:
            raise ProfileError(
                f"profile {profile_id!r}: network.proxy_protocol must be mixed, http, or socks5"
            )
        for key in ("auto_identity", "lock_exit", "private_hostname"):
            if key in network and not isinstance(network[key], bool):
                raise ProfileError(
                    f"profile {profile_id!r}: network.{key} must be a boolean"
                )

        direct_bypass = network.get("direct_bypass", []) or []
        if not isinstance(direct_bypass, list):
            raise ProfileError(
                f"profile {profile_id!r}: network.direct_bypass must be a list"
            )
        for index, rule in enumerate(direct_bypass):
            if not isinstance(rule, dict):
                raise ProfileError(
                    f"profile {profile_id!r}: network.direct_bypass[{index}] must be an object"
                )
            destination = str(rule.get("destination", "") or "").strip()
            try:
                destination_network = ipaddress.ip_network(destination, strict=False)
            except ValueError as exc:
                raise ProfileError(
                    f"profile {profile_id!r}: network.direct_bypass[{index}].destination is invalid"
                ) from exc
            if destination_network.version != 4 or destination_network.prefixlen < 24:
                raise ProfileError(
                    f"profile {profile_id!r}: network.direct_bypass[{index}].destination must be an IPv4 /24-/32 network"
                )
            protocol = str(rule.get("protocol", "tcp") or "tcp").lower()
            if protocol not in {"tcp", "udp"}:
                raise ProfileError(
                    f"profile {profile_id!r}: network.direct_bypass[{index}].protocol must be tcp or udp"
                )
            try:
                port = int(rule.get("port"))
            except (TypeError, ValueError) as exc:
                raise ProfileError(
                    f"profile {profile_id!r}: network.direct_bypass[{index}].port must be an integer"
                ) from exc
            if not 1 <= port <= 65535:
                raise ProfileError(
                    f"profile {profile_id!r}: network.direct_bypass[{index}].port out of range"
                )

    home = profile.get("home", {})
    if not isinstance(home, dict):
        raise ProfileError(f"profile {profile_id!r}: home must be an object")
    if home.get("mode", "inherit") not in {"inherit", "isolated"}:
        raise ProfileError(
            f"profile {profile_id!r}: home.mode must be inherit or isolated"
        )

    sandbox = profile.get("sandbox", {})
    if not isinstance(sandbox, dict):
        raise ProfileError(f"profile {profile_id!r}: sandbox must be an object")
    sandbox_mode = sandbox.get("mode", "off")
    if sandbox_mode not in {"off", "strict"}:
        raise ProfileError(
            f"profile {profile_id!r}: sandbox.mode must be off or strict"
        )
    system_mode = sandbox.get("system", "host")
    if system_mode not in {"host", "private"}:
        raise ProfileError(
            f"profile {profile_id!r}: sandbox.system must be host or private"
        )
    if system_mode == "private" and sandbox_mode != "strict":
        raise ProfileError(
            f"profile {profile_id!r}: private system view requires strict sandbox"
        )
    dmi_profile = sandbox.get("dmi_profile", "none")
    if dmi_profile not in {"none", "thinkbook-14-g7-iml"}:
        raise ProfileError(
            f"profile {profile_id!r}: sandbox.dmi_profile must be none or thinkbook-14-g7-iml"
        )
    if dmi_profile != "none" and (sandbox_mode != "strict" or system_mode != "private"):
        raise ProfileError(
            f"profile {profile_id!r}: sandbox.dmi_profile requires strict mode and a private system view"
        )
    clipboard_mode = str(sandbox.get("clipboard", "off") or "off")
    if clipboard_mode not in {"off", "wayland"}:
        raise ProfileError(
            f"profile {profile_id!r}: sandbox.clipboard must be off or wayland"
        )
    if sandbox_mode == "strict":
        if network_mode != "transparent":
            raise ProfileError(
                f"profile {profile_id!r}: strict sandbox requires transparent network mode"
            )
        if home.get("mode", "inherit") != "isolated":
            raise ProfileError(
                f"profile {profile_id!r}: strict sandbox requires home.mode=isolated"
            )
        username = str(sandbox.get("username", "dev") or "dev")
        if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", username):
            raise ProfileError(
                f"profile {profile_id!r}: sandbox.username is invalid"
            )
        shares = sandbox.get("shares", []) or []
        if not isinstance(shares, list):
            raise ProfileError(
                f"profile {profile_id!r}: sandbox.shares must be a list"
            )
        seen_targets: set[str] = set()
        for share in shares:
            if not isinstance(share, dict):
                raise ProfileError(
                    f"profile {profile_id!r}: each sandbox share must be an object"
                )
            source = str(share.get("source", "") or "").strip()
            target = str(share.get("target", "") or "").strip()
            mode = str(share.get("mode", "rw") or "rw")
            if not source or source in {"/", "~", "$HOME", "${HOME}"}:
                raise ProfileError(
                    f"profile {profile_id!r}: sandbox share source is unsafe"
                )
            pure_target = PurePosixPath(target)
            if (
                not target
                or pure_target.is_absolute()
                or ".." in pure_target.parts
                or target in {".", ".."}
            ):
                raise ProfileError(
                    f"profile {profile_id!r}: sandbox share target must be a relative path"
                )
            if target in seen_targets:
                raise ProfileError(
                    f"profile {profile_id!r}: duplicate sandbox share target {target!r}"
                )
            seen_targets.add(target)
            if mode not in {"rw", "ro"}:
                raise ProfileError(
                    f"profile {profile_id!r}: sandbox share mode must be rw or ro"
                )
        hidden_paths = sandbox.get("hidden_paths", []) or []
        if not isinstance(hidden_paths, list):
            raise ProfileError(
                f"profile {profile_id!r}: sandbox.hidden_paths must be a list"
            )
        for hidden in hidden_paths:
            if not isinstance(hidden, str):
                raise ProfileError(
                    f"profile {profile_id!r}: sandbox.hidden_paths entries must be strings"
                )
            relative = PurePosixPath(hidden)
            if (
                not hidden
                or hidden in {".", ".."}
                or relative.is_absolute()
                or ".." in relative.parts
            ):
                raise ProfileError(
                    f"profile {profile_id!r}: sandbox.hidden_paths must be relative directories"
                )


def resolved_shell(profile: Mapping[str, Any], host_env: Mapping[str, str] | None = None) -> str:
    env = host_env or os.environ
    terminal = profile.get("terminal", {})
    requested = str(terminal.get("shell", "inherit") or "inherit")
    if requested == "inherit":
        requested = env.get("SHELL", "") or "/usr/bin/bash"
    path = Path(requested).expanduser()
    if not path.is_absolute():
        found = shutil.which(requested)
        if found:
            path = Path(found)
    if not path.exists():
        raise ProfileError(f"shell does not exist: {path}")
    if not os.access(path, os.X_OK):
        raise ProfileError(f"shell is not executable: {path}")
    return str(path)


def resolved_cwd(profile: Mapping[str, Any]) -> Path:
    terminal = profile.get("terminal", {})
    raw = str(terminal.get("cwd", "~") or "~")
    path = Path(os.path.expandvars(raw)).expanduser().resolve()
    if not path.exists() or not path.is_dir():
        raise ProfileError(f"working directory is not a directory: {path}")
    return path


def build_environment(
    profile: Mapping[str, Any],
    host_env: Mapping[str, str] | None = None,
    *,
    create_home: bool = True,
) -> dict[str, str]:
    env = dict(host_env or os.environ)
    profile_id = str(profile["id"])
    env["FT_PROFILE_ID"] = profile_id
    env["FT_PROFILE_NAME"] = str(profile.get("name", profile_id))

    identity = profile.get("identity", {})
    timezone = str(identity.get("timezone", "inherit") or "inherit")
    locale = str(identity.get("locale", "inherit") or "inherit")
    if timezone != "inherit":
        env["TZ"] = timezone
        env["FT_TIMEZONE"] = timezone
    if locale != "inherit":
        env["LANG"] = locale
        env["LC_ALL"] = locale
        env["FT_LOCALE"] = locale

    custom = profile.get("environment", {})
    for key, value in custom.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = str(value)

    proxy = profile.get("proxy", {})
    mode = proxy.get("mode", "inherit")
    network = profile.get("network", {})
    transparent = network.get("mode", "inherit") == "transparent"
    if transparent or mode in {"off", "environment"}:
        for key in _PROXY_KEYS:
            env.pop(key, None)
    if mode == "environment" and not transparent:
        mapping = {
            "HTTP_PROXY": proxy.get("http", ""),
            "HTTPS_PROXY": proxy.get("https", ""),
            "ALL_PROXY": proxy.get("all", ""),
            "NO_PROXY": proxy.get("no_proxy", ""),
        }
        for upper, raw_value in mapping.items():
            value = str(raw_value or "").strip()
            if not value:
                continue
            env[upper] = value
            env[upper.lower()] = value

    if transparent:
        env["FT_NETWORK_MODE"] = "transparent"

    sandbox = profile.get("sandbox", {})
    if sandbox.get("mode", "off") == "strict":
        env["FT_SANDBOX_MODE"] = "strict"

    home = profile.get("home", {})
    if home.get("mode", "inherit") == "isolated":
        home_path = PROFILE_DATA_ROOT / profile_id / "home"
        if create_home:
            home_path.mkdir(parents=True, exist_ok=True)
        env["HOME"] = str(home_path)
        env["XDG_CONFIG_HOME"] = str(home_path / ".config")
        env["XDG_CACHE_HOME"] = str(home_path / ".cache")
        env["XDG_DATA_HOME"] = str(home_path / ".local" / "share")
        env["XDG_STATE_HOME"] = str(home_path / ".local" / "state")

    return env


def clone_profile(
    store: ProfileStore,
    source_id: str,
    new_id: str,
    *,
    name: str | None = None,
) -> dict[str, Any]:
    if not _PROFILE_ID.fullmatch(new_id):
        raise ProfileError("new profile id must match [a-z0-9][a-z0-9._-]{0,63}")
    if any(p.get("id") == new_id for p in store.profiles):
        raise ProfileError(f"profile already exists: {new_id}")
    profile = store.get(source_id)
    profile["id"] = new_id
    profile["name"] = name.strip() if name and name.strip() else new_id
    store.document["profiles"].append(profile)
    validate_document(store.document)
    store.save()
    return profile
