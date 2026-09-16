from __future__ import annotations

import unittest
from unittest import mock

from fingerprint_terminal import terminal


def profile(backend: str = "kitty") -> dict:
    return {
        "id": "local",
        "name": "Local",
        "terminal": {"backend": backend, "shell": "/bin/sh", "cwd": "/"},
        "identity": {},
        "environment": {},
        "proxy": {"mode": "inherit"},
        "home": {"mode": "inherit"},
    }


class TerminalTests(unittest.TestCase):
    @mock.patch.object(terminal.shutil, "which", side_effect=lambda name: f"/usr/bin/{name}")
    def test_kitty_command_uses_native_terminal(self, _which: mock.Mock) -> None:
        command = terminal.terminal_command(
            profile("kitty"), ["ft", "shell", "--profile", "local"]
        )
        self.assertEqual(command[0], "/usr/bin/kitty")
        self.assertIn("--directory", command)
        self.assertEqual(command[-4:], ["ft", "shell", "--profile", "local"])

    @mock.patch.object(terminal.shutil, "which", side_effect=lambda name: f"/usr/bin/{name}")
    def test_konsole_command_uses_native_terminal(self, _which: mock.Mock) -> None:
        command = terminal.terminal_command(profile("konsole"), ["ft", "shell"])
        self.assertEqual(command[0], "/usr/bin/konsole")
        self.assertIn("--workdir", command)
        self.assertIn("-e", command)


if __name__ == "__main__":
    unittest.main()

