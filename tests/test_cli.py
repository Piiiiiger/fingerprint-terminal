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
from fingerprint_terminal.profiles import EXAMPLE_PROFILES, ProfileError


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


class BridgeTests(unittest.TestCase):
    def run_bridge(self, profile: dict, cwd: str | None) -> dict:
        with mock.patch.object(cli, "load_store") as store, \
             mock.patch.object(cli, "build_environment", return_value={}), \
             mock.patch.object(cli.os, "chdir"), \
             mock.patch.object(cli, "prepare_transparent_environment", return_value=({}, {})), \
             mock.patch.object(cli, "strict_command", return_value=["sandbox-child"]) as strict, \
             mock.patch.object(cli, "isolation_command", return_value=["supervisor"]), \
             mock.patch.object(cli, "supervisor_environment", return_value={}), \
             mock.patch.object(cli, "create_bridge_lease", return_value=None), \
             mock.patch.object(cli.os, "execvpe", side_effect=RuntimeError("bridge executed")):
            store.return_value.get.return_value = profile
            with self.assertRaisesRegex(RuntimeError, "bridge executed"):
                cli.cmd_bridge(profile["id"], cwd, ["claude"])
            return strict.call_args.args[0]

    def test_empty_shares_allow_private_home_without_exposing_host_content(self) -> None:
        profile = strict_profile("")
        profile["sandbox"]["shares"] = []
        for cwd in (None, str(Path.home())):
            with self.subTest(cwd=cwd):
                launched = self.run_bridge(profile, cwd)
                self.assertEqual(launched["sandbox"]["shares"], [])

    def test_unshared_cwd_is_rejected_before_environment_or_network_setup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            share = root / "shared"
            unshared = root / "shared-other"
            share.mkdir()
            unshared.mkdir()
            for shares in ([], strict_profile(str(share))["sandbox"]["shares"]):
                with self.subTest(shares=shares):
                    profile = strict_profile("")
                    profile["sandbox"]["shares"] = shares
                    with mock.patch.object(cli, "load_store") as store, \
                         mock.patch.object(cli, "build_environment") as environment, \
                         mock.patch.object(cli, "prepare_transparent_environment") as network:
                        store.return_value.get.return_value = profile
                        with self.assertRaisesRegex(ProfileError, "not covered by sandbox.shares"):
                            cli.cmd_bridge(profile["id"], str(unshared), ["claude"])
                        environment.assert_not_called()
                        network.assert_not_called()
                    self.assertEqual(profile["sandbox"]["shares"], shares)

    def test_configured_share_outside_host_home_is_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cwd = Path(temporary) / "project"
            cwd.mkdir()
            profile = strict_profile(temporary)
            profile["sandbox"]["shares"][0]["mode"] = "ro"
            launched = self.run_bridge(profile, str(cwd))
            self.assertEqual(launched["sandbox"]["shares"], profile["sandbox"]["shares"])
            self.assertEqual(launched["terminal"]["cwd"], str(cwd.resolve()))

    def test_compatibility_mount_reuses_only_configured_source_and_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            share = home / "work/vault"
            share.mkdir(parents=True)
            profile = strict_profile(str(share))
            profile["sandbox"]["shares"][0]["mode"] = "ro"
            with mock.patch.object(cli.Path, "home", return_value=home):
                launched = self.run_bridge(profile, str(share))
            self.assertEqual(
                launched["sandbox"]["shares"],
                [{"source": str(share), "target": "work/vault", "mode": "ro"},
                 *profile["sandbox"]["shares"]],
            )
            self.assertEqual(len(profile["sandbox"]["shares"]), 1)


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
