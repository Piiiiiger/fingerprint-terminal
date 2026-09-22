from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fingerprint_terminal import cli
from fingerprint_terminal.profiles import EXAMPLE_PROFILES


def local_profile() -> dict:
    return {
        "id": "local",
        "name": "Local",
        "terminal": {"backend": "auto", "shell": "/bin/sh", "cwd": "/"},
        "identity": {},
        "environment": {},
        "proxy": {"mode": "inherit"},
        "home": {"mode": "inherit"},
    }


def strict_profile(share: str) -> dict:
    return {
        "id": "strict-test",
        "name": "Strict Test",
        "terminal": {"backend": "auto", "shell": "/bin/sh", "cwd": "/"},
        "identity": {"timezone": "inherit", "locale": "inherit"},
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
            "shares": [{"source": share, "target": "code", "mode": "rw"}],
        },
    }


class IdentityDefaultTests(unittest.TestCase):
    def test_identity_defaults_to_shipped_strict_profile(self) -> None:
        args = cli.build_parser().parse_args(["identity"])
        shipped = json.loads(EXAMPLE_PROFILES.read_text(encoding="utf-8"))
        self.assertIn(args.profile, [profile["id"] for profile in shipped["profiles"]])
        self.assertEqual(args.profile, "strict-auto-ip")


class DoctorTests(unittest.TestCase):
    def run_doctor(self, profiles: list[dict], missing: set[str]) -> tuple[int, str]:
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "profiles.json"
            config.write_text(
                json.dumps({"version": 1, "profiles": profiles}), encoding="utf-8"
            )
            real_which = shutil.which

            def which(name: str, *args, **kwargs):
                if name in missing:
                    return None
                return real_which(name, *args, **kwargs) or f"/usr/bin/{name}"

            output = io.StringIO()
            env = {"FT_PROFILES_FILE": str(config)}
            with mock.patch.dict(os.environ, env), mock.patch.object(
                shutil, "which", side_effect=which
            ), contextlib.redirect_stdout(output):
                os.environ.pop("FT_TERMINAL", None)
                status = cli.cmd_doctor()
        return status, output.getvalue()

    def test_local_profile_does_not_require_isolation_dependencies(self) -> None:
        status, output = self.run_doctor(
            [local_profile()], {"bwrap", *cli.TRANSPARENT_DEPENDENCIES}
        )
        self.assertEqual(status, 0, output)
        self.assertIn("profile local: OK", output)

    def test_one_terminal_backend_is_enough(self) -> None:
        status, output = self.run_doctor([local_profile()], {"kitty"})
        self.assertEqual(status, 0, output)

    def test_strict_profile_fails_without_bwrap(self) -> None:
        with tempfile.TemporaryDirectory() as share:
            status, output = self.run_doctor([strict_profile(share)], {"bwrap"})
        self.assertEqual(status, 1, output)
        self.assertIn("profile strict-test: ERROR: missing dependencies: bwrap", output)

    def test_transparent_profile_fails_without_network_tools(self) -> None:
        with tempfile.TemporaryDirectory() as share:
            status, output = self.run_doctor(
                [local_profile(), strict_profile(share)], {"sing-box", "nft"}
            )
        self.assertEqual(status, 1, output)
        self.assertIn("profile local: OK", output)
        self.assertIn("missing dependencies: nft, sing-box", output)


if __name__ == "__main__":
    unittest.main()
