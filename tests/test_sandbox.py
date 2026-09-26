from __future__ import annotations

import copy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fingerprint_terminal import sandbox
from fingerprint_terminal import system_view
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

    def test_dmi_profile_requires_private_strict_system(self) -> None:
        profile = self.profile("/tmp")
        profile["sandbox"]["dmi_profile"] = "thinkbook-14-g7-iml"
        with self.assertRaisesRegex(ProfileError, "private system view"):
            validate_profile(profile)
        profile["sandbox"]["system"] = "private"
        validate_profile(profile)

    def test_share_target_cannot_escape_private_home(self) -> None:
        profile = self.profile("/tmp")
        profile["sandbox"]["shares"][0]["target"] = "../escape"
        with self.assertRaises(ProfileError):
            validate_profile(profile)

    def test_hidden_share_directory_is_masked_at_each_mount(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            share = root / "share"
            (share / ".claudian").mkdir(parents=True)
            profile = self.profile(str(share))
            profile["sandbox"]["hidden_paths"] = [".claudian"]
            profile["sandbox"]["shares"].append(
                {"source": str(share), "target": "code/vault", "mode": "rw"}
            )
            resolv = root / "resolv.conf"
            resolv.write_text("nameserver 192.168.1.1\n", encoding="utf-8")
            with patch.object(sandbox, "PROFILE_DATA_ROOT", root / "profiles"):
                command = sandbox.strict_command(
                    profile, ["/usr/bin/true"], {"FT_RESOLV_CONF": str(resolv)}
                )
            masks = [
                command[index + 1]
                for index, arg in enumerate(command[:-1])
                if arg == "--tmpfs"
            ]
            self.assertIn("/home/dev/code/.claudian", masks)
            self.assertIn("/home/dev/code/vault/.claudian", masks)

    def test_hidden_paths_reject_parent_traversal(self) -> None:
        profile = self.profile("/tmp")
        profile["sandbox"]["hidden_paths"] = ["../private"]
        with self.assertRaises(ProfileError):
            validate_profile(profile)

    def test_private_system_mounts_only_profile_usr_and_etc(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            share = root / "share"
            share.mkdir()
            resolv = root / "resolv.conf"
            resolv.write_text("nameserver 192.168.1.1\n", encoding="utf-8")
            profile = self.profile(str(share))
            profile["sandbox"]["system"] = "private"
            release = root / "profiles/strict-test/system/releases/test"
            for relative in (
                "usr/bin/bash", "usr/bin/python3",
                "usr/share/zoneinfo/America/Los_Angeles",
                "usr/share/wayland-sessions/niri.desktop", "ready.json",
                "etc/nsswitch.conf", "etc/ssl/cert.pem",
            ):
                path = release / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("test", encoding="utf-8")
            (release.parent.parent / "current").symlink_to("releases/test")
            with patch.object(sandbox, "PROFILE_DATA_ROOT", root / "profiles"), \
                 patch.object(system_view, "PROFILE_DATA_ROOT", root / "profiles"):
                command = sandbox.strict_command(
                    profile, ["/usr/bin/bash", "-l"],
                    {"TZ": "America/Los_Angeles", "FT_RESOLV_CONF": str(resolv)},
                )
            mounts = [
                (command[index + 1], command[index + 2])
                for index, value in enumerate(command[:-2]) if value == "--ro-bind"
            ]
            self.assertIn((str(release / "usr"), "/usr"), mounts)
            self.assertIn((str(release / "etc/ssl"), "/etc/ssl"), mounts)
            self.assertNotIn(("/usr", "/usr"), mounts)
            self.assertNotIn(("/etc/ssl", "/etc/ssl"), mounts)
            self.assertEqual(command[command.index("XDG_CURRENT_DESKTOP") + 1], "niri")

    def test_thinkbook_dmi_is_copied_into_private_sys_view(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            share = root / "share"
            share.mkdir()
            resolv = root / "resolv.conf"
            resolv.write_text("nameserver 192.168.1.1\n", encoding="utf-8")
            profile = self.profile(str(share))
            profile["sandbox"].update(system="private", dmi_profile="thinkbook-14-g7-iml")
            release = root / "profiles/strict-test/system/releases/test"
            for relative in (
                "usr/bin/bash", "usr/bin/python3",
                "usr/share/zoneinfo/America/Los_Angeles",
                "usr/share/wayland-sessions/niri.desktop", "ready.json",
            ):
                path = release / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("test", encoding="utf-8")
            (release.parent.parent / "current").symlink_to("releases/test")
            with patch.object(sandbox, "PROFILE_DATA_ROOT", root / "profiles"), \
                 patch.object(system_view, "PROFILE_DATA_ROOT", root / "profiles"):
                command = sandbox.strict_command(
                    profile, ["/usr/bin/bash"],
                    {"TZ": "America/Los_Angeles", "FT_RESOLV_CONF": str(resolv)},
                )
            self.assertIn("/sys/class/dmi/id", command)
            self.assertIn("/sys/devices/virtual/dmi/id/product_name", command)
            self.assertEqual(
                (root / "profiles/strict-test/dmi/product_name").read_text(),
                "ThinkBook 14 G7 IML\n",
            )
            self.assertNotIn(str(root / "profiles/strict-test/dmi/product_name"),
                             command[command.index("--die-with-parent"):])

    def test_private_system_fails_closed_when_unprepared(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            profile = self.profile(tmp)
            profile["sandbox"]["system"] = "private"
            with patch.object(system_view, "PROFILE_DATA_ROOT", Path(tmp) / "profiles"):
                with self.assertRaisesRegex(sandbox.SandboxError, "prepare-system"):
                    sandbox.strict_command(profile, ["/usr/bin/bash"], {})

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

            setenv = {
                command[index + 1]
                for index, arg in enumerate(command)
                if arg == "--setenv"
            }
            self.assertEqual([key for key in setenv if key.startswith("FT_")], [])
            self.assertEqual(
                command[command.index("HOSTNAME") + 1], "desktop-us-test"
            )

    def test_identity_files_are_copied_not_bind_mounted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            resolv = tmp_path / "resolv.conf"
            resolv.write_text("nameserver 192.168.1.1\n", encoding="utf-8")
            profile_root = tmp_path / "profiles"
            profile = self.profile(str(tmp_path))
            env = {"TZ": "UTC", "FT_RESOLV_CONF": str(resolv)}
            with patch.object(sandbox, "PROFILE_DATA_ROOT", profile_root):
                command = sandbox.strict_command(
                    profile, ["/usr/bin/bash", "-l"], env
                )

            bwrap_args = command[command.index("--die-with-parent") - 1 :]
            # The persistent HOME is the only host path under the profile
            # storage that bwrap mounts; everything else is copied in.
            self.assertEqual(
                [arg for arg in bwrap_args if arg.startswith(str(profile_root))],
                [str(profile_root / "strict-test" / "home")],
            )
            for _option, _perms, _key, target in sandbox._INJECTED_FILES:
                self.assertIn(
                    bwrap_args[bwrap_args.index(target) - 2],
                    {"--file", "--ro-bind-data"},
                )
            self.assertGreater(
                bwrap_args.index("--remount-ro"), bwrap_args.index("/etc/resolv.conf")
            )
            passwd = (profile_root / "strict-test" / "passwd").read_text()
            self.assertNotIn("Fingerprint", passwd)
            fstab = (profile_root / "strict-test" / "fstab").read_text()
            self.assertNotIn("fingerprint", fstab)

    def test_injected_descriptors_reach_the_final_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sources = []
            for index in range(len(sandbox._INJECTED_FILES)):
                path = Path(tmp) / f"source-{index}"
                path.write_text(f"{index};", encoding="utf-8")
                sources.append(str(path))
            first = sandbox._FIRST_INJECTED_FD
            reader = (
                "import os, sys; sys.stdout.write(''.join("
                f"os.read(fd, 64).decode() for fd in range({first}, {first + len(sources)})))"
            )
            command = sandbox._with_injected_descriptors(
                [sys.executable, "-c", reader], sources
            )
            result = subprocess.run(command, capture_output=True, text=True, check=True)
        self.assertEqual(
            result.stdout, "".join(f"{index};" for index in range(len(sources)))
        )

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
