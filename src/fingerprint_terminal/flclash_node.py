"""Keep one profile-owned local proxy pinned to a named FlClash node."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import signal
import socket
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Mapping


DEFAULT_CONFIG = Path.home() / ".local/share/com.follow.clash/config.yaml"
STATE_ROOT = Path.home() / ".local/state/fingerprint-terminal"


class FlClashNodeError(RuntimeError):
    """The selected node cannot provide a dedicated, verified proxy."""


def _load_nodes(path: Path) -> dict[str, dict[str, Any]]:
    try:
        import yaml  # Optional on machines without a FlClash-bound profile.
    except ImportError as exc:
        raise FlClashNodeError("FlClash node profiles require python-yaml") from exc
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise FlClashNodeError("could not read the active FlClash configuration") from exc
    if not isinstance(data, dict) or not isinstance(data.get("proxies"), list):
        raise FlClashNodeError("the active FlClash configuration has no proxy list")
    nodes: dict[str, dict[str, Any]] = {}
    for node in data["proxies"]:
        if isinstance(node, dict) and isinstance(node.get("name"), str):
            if node["name"] in nodes:
                raise FlClashNodeError("the FlClash configuration has duplicate node names")
            nodes[node["name"]] = node
    return nodes


def _vless_outbound(node: Mapping[str, Any], tag: str, detour: str | None) -> dict[str, Any]:
    supported = {
        "name", "type", "server", "port", "uuid", "encryption", "flow",
        "network", "tls", "servername", "skip-cert-verify", "udp", "dialer-proxy",
    }
    if set(node) - supported or node.get("type") != "vless":
        raise FlClashNodeError("the selected FlClash node uses unsupported options")
    if node.get("network", "tcp") != "tcp" or node.get("encryption", "none") != "none":
        raise FlClashNodeError("the selected FlClash node is not a TCP VLESS node")
    if node.get("tls") is not True:
        raise FlClashNodeError("the selected FlClash node must use TLS")
    try:
        port = int(node["port"])
        uuid.UUID(str(node["uuid"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise FlClashNodeError("the selected FlClash node has invalid connection fields") from exc
    if not 1 <= port <= 65535 or not str(node.get("server") or ""):
        raise FlClashNodeError("the selected FlClash node has invalid server or port")
    outbound: dict[str, Any] = {
        "type": "vless",
        "tag": tag,
        "server": str(node["server"]),
        "server_port": port,
        "uuid": str(node["uuid"]),
        "tls": {
            "enabled": True,
            "server_name": str(node.get("servername") or node["server"]),
            "insecure": bool(node.get("skip-cert-verify", False)),
        },
    }
    flow = str(node.get("flow") or "")
    if flow:
        if flow != "xtls-rprx-vision":
            raise FlClashNodeError("the selected FlClash node uses an unsupported VLESS flow")
        outbound["flow"] = flow
    if detour:
        outbound["detour"] = detour
    return outbound


def node_proxy_config(nodes: Mapping[str, Mapping[str, Any]], name: str, port: int) -> dict[str, Any]:
    """Build only the named node and its explicit dialer chain."""

    chain: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    current = name
    while current:
        if current in seen or current not in nodes or len(chain) >= 8:
            raise FlClashNodeError("the selected FlClash node or its dialer chain is unavailable")
        seen.add(current)
        node = nodes[current]
        chain.append(node)
        current = str(node.get("dialer-proxy") or "")
    outbounds = [
        _vless_outbound(node, f"node-{index}", f"node-{index + 1}" if index + 1 < len(chain) else None)
        for index, node in enumerate(chain)
    ]
    return {
        "log": {"level": "warn"},
        "inbounds": [{"type": "mixed", "tag": "profile-in", "listen": "127.0.0.1", "listen_port": port}],
        "outbounds": outbounds,
        "route": {"final": "node-0"},
    }


def _start_time(pid: int) -> str | None:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text(encoding="ascii").rsplit(") ", 1)[1].split()
        return None if fields[0] == "Z" else fields[19]
    except (OSError, IndexError):
        return None


def _listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return True
    except OSError:
        return False


def _stop_owned_process(pid: int, start_time: str) -> None:
    if _start_time(pid) != start_time:
        return
    try:
        fd = os.pidfd_open(pid)
    except ProcessLookupError:
        return
    try:
        if _start_time(pid) == start_time:
            signal.pidfd_send_signal(fd, signal.SIGTERM)
    finally:
        os.close(fd)
    for _ in range(30):
        if _start_time(pid) != start_time:
            return
        time.sleep(0.1)
    raise FlClashNodeError("the previous dedicated node proxy did not stop")


def ensure_node_proxy(profile: Mapping[str, Any]) -> None:
    """Start or refresh the dedicated proxy before IP identity preflight."""

    network = profile.get("network", {})
    node_name = str(network.get("flclash_node") or "")
    port = int(network.get("proxy_port", 0))
    source = Path(str(network.get("flclash_config") or DEFAULT_CONFIG)).expanduser()
    nodes = _load_nodes(source)
    config = node_proxy_config(nodes, node_name, port)
    payload = json.dumps(config, ensure_ascii=False, separators=(",", ":")) + "\n"
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    state = STATE_ROOT / str(profile["id"]) / "flclash-node"
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    state.chmod(0o700)
    lock_path = state / "lock"
    with lock_path.open("a+") as lock:
        lock_path.chmod(0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        pid_path = state / "process.json"
        try:
            recorded = json.loads(pid_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            recorded = {}
        try:
            pid = int(recorded.get("pid") or 0)
        except (TypeError, ValueError):
            pid = 0
        start_time = str(recorded.get("start_time") or "")
        owned = bool(pid > 0 and start_time and _start_time(pid) == start_time)
        if owned and recorded.get("digest") == digest and _listening(port):
            return
        if owned:
            _stop_owned_process(pid, start_time)
        if _listening(port):
            raise FlClashNodeError(f"dedicated node proxy port {port} is already in use")

        config_path = state / "config.json"
        temporary = state / f".config-{os.getpid()}"
        temporary.write_text(payload, encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(config_path)
        try:
            subprocess.run(
                ["sing-box", "check", "-c", str(config_path)],
                check=True, capture_output=True, timeout=15,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise FlClashNodeError("the dedicated node proxy configuration is invalid") from exc

        env = {key: value for key, value in os.environ.items() if "proxy" not in key.lower()}
        log_path = state / "sing-box.log"
        with log_path.open("ab") as log:
            log_path.chmod(0o600)
            process = subprocess.Popen(
                ["sing-box", "run", "-c", str(config_path)],
                cwd=state, env=env, stdin=subprocess.DEVNULL,
                stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
            )
        for _ in range(50):
            if process.poll() is not None:
                raise FlClashNodeError("the dedicated node proxy exited during startup")
            if _listening(port):
                break
            time.sleep(0.1)
        else:
            process.terminate()
            raise FlClashNodeError("the dedicated node proxy did not open its port")
        pid_path.write_text(json.dumps({
            "pid": process.pid, "start_time": _start_time(process.pid), "digest": digest,
        }) + "\n", encoding="utf-8")
        pid_path.chmod(0o600)
