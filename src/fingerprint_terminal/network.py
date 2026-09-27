"""Transparent network namespace preparation for Fingerprint Terminal."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any, Mapping, Sequence

from .identity import IdentityError, detect_exit_identity, identity_environment
from .flclash_node import FlClashNodeError, ensure_node_proxy


STATE_ROOT = Path.home() / ".local" / "state" / "fingerprint-terminal"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
SUPERVISOR = PROJECT_ROOT / "host" / "network-supervisor.sh"

TRANSPARENT_DEPENDENCIES = (
    "unshare",
    "nsenter",
    "mount",
    "ip",
    "nft",
    "slirp4netns",
    "sing-box",
    "ss",
    "setpriv",
    "curl",
    "sysctl",
)

_PROXY_ENV_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
)


class NetworkError(RuntimeError):
    """Raised when transparent isolation cannot be prepared safely."""


def settings(profile: Mapping[str, Any]) -> dict[str, Any]:
    raw = profile.get("network", {})
    return dict(raw) if isinstance(raw, Mapping) else {}


def is_transparent(profile: Mapping[str, Any]) -> bool:
    return str(settings(profile).get("mode", "inherit")) == "transparent"


def _direct_bypass_environment(network: Mapping[str, Any]) -> str:
    """Serialize validated IPv4 destination/port bypasses for the supervisor."""

    entries: list[str] = []
    for rule in network.get("direct_bypass", []) or []:
        destination = ipaddress.ip_network(
            str(rule.get("destination", "") or "").strip(), strict=False
        )
        protocol = str(rule.get("protocol", "tcp") or "tcp").lower()
        port = int(rule.get("port"))
        entries.append(f"{destination.with_prefixlen},{protocol},{port}")
    return "\n".join(entries)


def _runtime_dir(profile: Mapping[str, Any]) -> Path:
    profile_id = str(profile["id"])
    return STATE_ROOT / profile_id


def _safe_hostname(profile: Mapping[str, Any], identity: Mapping[str, Any]) -> str:
    configured = str(profile.get("identity", {}).get("hostname", "auto") or "auto")
    if configured not in {"inherit", "auto"}:
        value = configured
    else:
        country = str(identity.get("country_code") or "xx").lower()
        digest = hashlib.sha256(str(profile["id"]).encode("utf-8")).hexdigest()[:6]
        value = f"desktop-{country}-{digest}"
    value = re.sub(r"[^A-Za-z0-9-]+", "-", value).strip("-")[:63]
    return value or "fingerprint-terminal"


def _zoneinfo_path(timezone: str) -> Path:
    zone_root = Path("/usr/share/zoneinfo").resolve()
    candidate = (zone_root / timezone).resolve()
    if zone_root != candidate and zone_root not in candidate.parents:
        raise NetworkError(f"unsafe timezone path: {timezone!r}")
    if not candidate.is_file():
        raise NetworkError(f"timezone file does not exist: {candidate}")
    return candidate


def _singbox_config(network: Mapping[str, Any]) -> dict[str, Any]:
    protocol = str(network.get("proxy_protocol", "mixed")).lower()
    port = int(network.get("proxy_port", 7898))
    if protocol in {"mixed", "socks5"}:
        outbound: dict[str, Any] = {
            "type": "socks",
            "tag": "profile-proxy",
            "server": "10.0.2.2",
            "server_port": port,
            "version": "5",
        }
    elif protocol == "http":
        outbound = {
            "type": "http",
            "tag": "profile-proxy",
            "server": "10.0.2.2",
            "server_port": port,
        }
    else:
        raise NetworkError(f"unsupported transparent proxy protocol: {protocol}")

    return {
        "log": {"level": "info", "timestamp": True},
        "dns": {
            "servers": [
                {
                    "type": "https",
                    "tag": "remote-dns",
                    "server": "1.1.1.1",
                    "server_port": 443,
                    "path": "/dns-query",
                    "detour": "profile-proxy",
                }
            ],
            "final": "remote-dns",
            "strategy": "ipv4_only",
        },
        "inbounds": [
            {
                "type": "tproxy",
                "tag": "transparent-in",
                "listen": "0.0.0.0",
                "listen_port": 7893,
                "network": ["tcp", "udp"],
            }
        ],
        "outbounds": [outbound],
        "route": {
            "rules": [
                {"action": "sniff"},
                {"protocol": "dns", "action": "hijack-dns"},
            ],
            "final": "profile-proxy",
            "auto_detect_interface": True,
        },
    }


def detect_profile_identity(profile: Mapping[str, Any]) -> dict[str, Any]:
    """Observe and validate the exit selected for this profile."""

    network = settings(profile)
    try:
        if network.get("flclash_node"):
            ensure_node_proxy(profile)
        identity = detect_exit_identity(network)
    except (IdentityError, FlClashNodeError) as exc:
        raise NetworkError(str(exc)) from exc
    expected_country = str(network.get("expected_country") or "").upper()
    if expected_country and identity.get("country_code") != expected_country:
        raise NetworkError(
            f"proxy exit country is {identity.get('country_code') or 'unknown'}, expected {expected_country}"
        )
    expected_timezone = str(network.get("expected_timezone") or "")
    if expected_timezone and identity.get("timezone") != expected_timezone:
        raise NetworkError(
            f"proxy exit timezone is {identity.get('timezone') or 'unknown'}, expected {expected_timezone}"
        )
    return identity


def prepare_transparent_environment(
    profile: Mapping[str, Any], env: Mapping[str, str]
) -> tuple[dict[str, str], dict[str, Any]]:
    """Preflight the real exit, derive identity, and create runtime files."""

    network = settings(profile)
    if str(network.get("proxy_host", "127.0.0.1")) not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        raise NetworkError(
            "transparent mode currently requires a proxy listening on the host; "
            "use proxy_host=127.0.0.1"
        )

    missing = [name for name in TRANSPARENT_DEPENDENCIES if not shutil.which(name)]
    if missing:
        raise NetworkError(
            "missing transparent-network dependencies: " + ", ".join(missing)
        )
    if not SUPERVISOR.is_file():
        raise NetworkError(f"network supervisor is missing: {SUPERVISOR}")

    identity = detect_profile_identity(profile)

    result = dict(env)
    for key in _PROXY_ENV_KEYS:
        result.pop(key, None)
    if bool(network.get("auto_identity", True)):
        result.update(identity_environment(identity))

    runtime = _runtime_dir(profile)
    log_dir = runtime / "logs"
    runtime.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    identity_file = runtime / "identity.json"
    identity_file.write_text(
        json.dumps(identity, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    singbox_file = runtime / "sing-box.json"
    singbox_file.write_text(
        json.dumps(_singbox_config(network), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    resolv_file = runtime / "resolv.conf"
    resolv_file.write_text(
        "nameserver 192.168.1.1\noptions timeout:2 attempts:2\n", encoding="utf-8"
    )

    hostname = _safe_hostname(profile, identity)
    hostname_file = runtime / "hostname"
    hostname_file.write_text(hostname + "\n", encoding="utf-8")
    hosts_file = runtime / "hosts"
    hosts_file.write_text(
        "127.0.0.1 localhost\n"
        f"127.0.1.1 {hostname}\n"
        "::1 localhost ip6-localhost ip6-loopback\n",
        encoding="utf-8",
    )

    timezone = str(identity.get("timezone") or result.get("TZ") or "UTC")
    zoneinfo = _zoneinfo_path(timezone)

    result.update(
        {
            "FT_NETWORK_MODE": "transparent",
            "FT_RUNTIME_DIR": str(runtime),
            "FT_SINGBOX_CONFIG": str(singbox_file),
            "FT_RESOLV_CONF": str(resolv_file),
            "FT_HOSTNAME_FILE": str(hostname_file),
            "FT_HOSTS_FILE": str(hosts_file),
            "FT_HOSTNAME": hostname,
            "FT_PRIVATE_HOSTNAME": "1"
            if bool(network.get("private_hostname", True))
            else "0",
            "FT_ZONEINFO_FILE": str(zoneinfo),
            "FT_EXPECTED_IP": str(identity["ip"]),
            "FT_COUNTRY_CODE": str(identity["country_code"]),
            "FT_PROXY_PORT": str(int(network.get("proxy_port", 7898))),
            "FT_TPROXY_PORT": "7893",
            "FT_DIRECT_BYPASS_RULES": _direct_bypass_environment(network),
            "FT_IDENTITY_FILE": str(identity_file),
        }
    )
    return result, identity


def isolation_command(child: Sequence[str]) -> list[str]:
    """Run the supervisor with namespace capabilities, keeping UID 1000 visible."""

    unshare = shutil.which("unshare") or "unshare"
    bash = shutil.which("bash") or "/usr/bin/bash"
    return [
        unshare,
        "--user",
        "--map-current-user",
        "--keep-caps",
        "--mount",
        "--pid",
        "--fork",
        "--kill-child",
        "--mount-proc",
        bash,
        str(SUPERVISOR),
        *[str(part) for part in child],
    ]


def supervisor_environment(env: Mapping[str, str], *, strict: bool) -> dict[str, str]:
    """Keep host-side setup on an installed locale; bwrap sets the guest locale."""

    result = dict(env)
    if strict:
        result["LANG"] = "C.UTF-8"
        result["LC_ALL"] = "C.UTF-8"
        result.pop("LANGUAGE", None)
    return result
