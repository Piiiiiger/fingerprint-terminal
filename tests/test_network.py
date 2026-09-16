from __future__ import annotations

import unittest
from pathlib import Path

from fingerprint_terminal.identity import identity_environment, locale_for_country, parse_cloudflare_trace
from fingerprint_terminal.network import (
    _direct_bypass_environment,
    _singbox_config,
    isolation_command,
    is_transparent,
)
from fingerprint_terminal.profiles import ProfileError, build_environment, validate_profile


class NetworkTests(unittest.TestCase):
    def transparent_profile(self) -> dict:
        return {
            "id": "auto-ip",
            "name": "Auto IP",
            "terminal": {"backend": "auto", "shell": "inherit", "cwd": "~"},
            "identity": {"timezone": "inherit", "locale": "inherit"},
            "environment": {},
            "proxy": {"mode": "inherit"},
            "network": {
                "mode": "transparent",
                "proxy_host": "127.0.0.1",
                "proxy_port": 7898,
                "proxy_protocol": "mixed",
                "auto_identity": True,
                "lock_exit": True,
            },
            "home": {"mode": "inherit"},
        }

    def test_cloudflare_trace_parser(self) -> None:
        trace = parse_cloudflare_trace("ip=1.2.3.4\nloc=US\ncolo=LAX\n")
        self.assertEqual(trace["ip"], "1.2.3.4")
        self.assertEqual(trace["loc"], "US")

    def test_identity_environment_aligns_timezone_and_locale(self) -> None:
        env = identity_environment(
            {
                "ip": "1.2.3.4",
                "country_code": "US",
                "timezone": "America/Los_Angeles",
                "locale": "en_US.UTF-8",
            }
        )
        self.assertEqual(env["TZ"], "America/Los_Angeles")
        self.assertEqual(env["LANG"], "en_US.UTF-8")
        self.assertEqual(env["FT_EXIT_IP"], "1.2.3.4")

    def test_locale_mapping(self) -> None:
        self.assertEqual(locale_for_country("US"), "en_US.UTF-8")
        self.assertEqual(locale_for_country("JP"), "ja_JP.UTF-8")

    def test_transparent_profile_strips_host_proxy_environment(self) -> None:
        profile = self.transparent_profile()
        validate_profile(profile)
        env = build_environment(
            profile,
            {
                "HOME": "/home/test",
                "SHELL": "/bin/bash",
                "HTTP_PROXY": "http://leak.invalid:1234",
                "https_proxy": "http://leak.invalid:1234",
            },
            create_home=False,
        )
        self.assertTrue(is_transparent(profile))
        self.assertNotIn("HTTP_PROXY", env)
        self.assertNotIn("https_proxy", env)
        self.assertEqual(env["FT_NETWORK_MODE"], "transparent")

    def test_singbox_config_routes_dns_and_traffic_through_host_proxy(self) -> None:
        config = _singbox_config(self.transparent_profile()["network"])
        self.assertEqual(config["outbounds"][0]["type"], "socks")
        self.assertEqual(config["outbounds"][0]["server"], "10.0.2.2")
        self.assertEqual(config["outbounds"][0]["server_port"], 7898)
        self.assertEqual(config["route"]["final"], "profile-proxy")
        self.assertEqual(config["dns"]["servers"][0]["detour"], "profile-proxy")

    def test_direct_bypass_is_normalized_to_destination_protocol_port(self) -> None:
        profile = self.transparent_profile()
        profile["network"]["direct_bypass"] = [
            {
                "name": "Campus SSH",
                "destination": "10.20.30.40",
                "protocol": "tcp",
                "port": 22,
            }
        ]
        validate_profile(profile)
        self.assertEqual(
            _direct_bypass_environment(profile["network"]),
            "10.20.30.40/32,tcp,22",
        )

    def test_direct_bypass_rejects_broad_networks(self) -> None:
        profile = self.transparent_profile()
        profile["network"]["direct_bypass"] = [
            {"destination": "0.0.0.0/0", "protocol": "tcp", "port": 22}
        ]
        with self.assertRaises(ProfileError):
            validate_profile(profile)

    def test_isolation_command_keeps_current_uid_mapping(self) -> None:
        command = isolation_command(["/bin/bash", "-l"])
        self.assertIn("--map-current-user", command)
        self.assertIn("--keep-caps", command)
        self.assertNotIn("--map-root-user", command)
        supervisor_index = command.index(str(Path(__file__).resolve().parents[1] / "host" / "network-supervisor.sh"))
        self.assertEqual(Path(command[supervisor_index - 1]).name, "bash")


if __name__ == "__main__":
    unittest.main()
