from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fingerprint_terminal import sandbox
from fingerprint_terminal.profiles import ProfileError, validate_profile


class SandboxTests(unittest.TestCase):
    def profile(self, share_source: str) -> dict:
        return {
            "id": "strict-test",
            "name": "Strict Test",
            "terminal": {"backend": "auto", "shell": "/usr/bin/bash", "cwd": "~"},
            "identity": {"timezone": "America/Los_Angeles", "locale": "en_US.UTF-8"},
            "environment": {},
            "proxy": {"mode": "off"},
            "network": {
                "mode": "transparent",
                "proxy_host": "127.0.0.1",
                "proxy_port": 7898,
                "proxy_protocol": "mixed",
            },
            "home": {"mode": "isolated"},
            "sandbox": {
                "mode": "strict",
                "username": "dev",
                "shares": [
                    {"source": share_source, "target": "code", "mode": "rw"}
                ],
            },
        }

    def test_strict_profile_requires_transparent_network_and_isolated_home(self) -> None:
        profile = self.profile("/tmp")
        validate_profile(profile)

        bad_network = copy.deepcopy(profile)
        bad_network["network"]["mode"] = "inherit"
        with self.assertRaises(ProfileError):
            validate_profile(bad_network)

        bad_home = copy.deepcopy(profile)
        bad_home["home"]["mode"] = "inherit"
        with self.assertRaises(ProfileError):
            validate_profile(bad_home)

    def test_share_target_cannot_escape_private_home(self) -> None:
        profile = self.profile("/tmp")
        profile["sandbox"]["shares"][0]["target"] = "../escape"
        with self.assertRaises(ProfileError):
            validate_profile(profile)

    def test_command_masks_host_home_and_clears_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            share = tmp_path / "share"
            share.mkdir()
            resolv = tmp_path / "resolv.conf"
            resolv.write_text("nameserver 192.168.1.1\n", encoding="utf-8")
            profile_root = tmp_path / "profiles"
            profile = self.profile(str(share))
            env = {
                "HOME": "/home/real-user",
                "PATH": "/home/real-user/bin:/usr/bin",
                "AWS_SECRET_ACCESS_KEY": "do-not-inherit",
                "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
                "TERM": "xterm-kitty",
                "COLORTERM": "truecolor",
                "TZ": "America/Los_Angeles",
                "LANG": "en_US.UTF-8",
                "LC_ALL": "en_US.UTF-8",
                "FT_PROFILE_ID": "strict-test",
                "FT_PROFILE_NAME": "Strict Test",
                "FT_NETWORK_MODE": "transparent",
                "FT_HOSTNAME": "desktop-us-test",
                "FT_RESOLV_CONF": str(resolv),
                "FT_EXIT_IP": "203.0.113.7",
                "FT_COUNTRY_CODE": "US",
            }
            with patch.object(sandbox, "PROFILE_DATA_ROOT", profile_root):
                command = sandbox.strict_command(
                    profile, ["/usr/bin/bash", "-l"], env
                )

            self.assertIn("--clearenv", command)
            self.assertIn("--tmpfs", command)
            self.assertNotIn("/home/real-user", command)
            rendered = "\0".join(command)
            self.assertNotIn("AWS_SECRET_ACCESS_KEY", rendered)
            self.assertNotIn("DBUS_SESSION_BUS_ADDRESS", rendered)
            self.assertIn("/home/dev", command)
            self.assertIn(str(share.resolve()), command)
            self.assertIn("/home/dev/code", command)

    def test_machine_id_is_stable_per_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "profiles"
            profile = self.profile("/tmp")
            with patch.object(sandbox, "PROFILE_DATA_ROOT", root):
                first = sandbox._stable_machine_id(profile).read_text().strip()
                second = sandbox._stable_machine_id(profile).read_text().strip()
            self.assertEqual(first, second)
            self.assertRegex(first, r"^[0-9a-f]{32}$")


if __name__ == "__main__":
    unittest.main()
