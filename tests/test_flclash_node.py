from __future__ import annotations

import unittest

from fingerprint_terminal.flclash_node import FlClashNodeError, node_proxy_config


def _vless(name: str, *, upstream: str | None = None) -> dict:
    node = {
        "name": name,
        "type": "vless",
        "server": "example.invalid",
        "port": 443,
        "uuid": "11111111-1111-4111-8111-111111111111",
        "encryption": "none",
        "network": "tcp",
        "tls": True,
        "servername": "example.invalid",
        "skip-cert-verify": False,
        "udp": True,
    }
    if upstream:
        node["dialer-proxy"] = upstream
    return node


class FlClashNodeTests(unittest.TestCase):
    def test_named_node_uses_its_upstream_without_selector(self) -> None:
        nodes = {
            "example-node": _vless("example-node", upstream="example-upstream"),
            "example-upstream": _vless("example-upstream"),
            "unrelated": _vless("unrelated"),
        }
        nodes["example-node"]["flow"] = "xtls-rprx-vision"
        config = node_proxy_config(nodes, "example-node", 7900)
        self.assertEqual(config["inbounds"][0]["listen_port"], 7900)
        self.assertEqual(len(config["outbounds"]), 2)
        self.assertEqual(config["outbounds"][0]["detour"], "node-1")
        self.assertEqual(config["outbounds"][0]["flow"], "xtls-rprx-vision")
        self.assertEqual(config["route"]["final"], "node-0")

    def test_missing_or_unsupported_node_fails_closed(self) -> None:
        with self.assertRaises(FlClashNodeError):
            node_proxy_config({}, "missing", 7900)
        node = _vless("node")
        node["reality-opts"] = {"public-key": "placeholder"}
        with self.assertRaisesRegex(FlClashNodeError, "unsupported"):
            node_proxy_config({"node": node}, "node", 7900)


if __name__ == "__main__":
    unittest.main()
