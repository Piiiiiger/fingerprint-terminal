from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from fingerprint_terminal import manager


class ManagerHelperTests(unittest.TestCase):
    def test_relative_time_buckets(self) -> None:
        self.assertEqual(manager._relative_time(20), "刚刚")
        self.assertEqual(manager._relative_time(5 * 60), "5 分钟前")
        self.assertEqual(manager._relative_time(3 * 3600 + 59), "3 小时前")
        self.assertEqual(manager._relative_time(2 * 86400), "2 天前")

    def test_last_exit_reads_latest_launch_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.object(manager, "STATE_ROOT", root):
                self.assertIsNone(manager._last_exit("strict-test"))

                runtime = root / "strict-test"
                runtime.mkdir()
                identity = runtime / "identity.json"
                identity.write_text("{not json", encoding="utf-8")
                self.assertIsNone(manager._last_exit("strict-test"))

                identity.write_text(
                    json.dumps({"ip": "203.0.113.7", "country_code": "JP"}),
                    encoding="utf-8",
                )
                two_hours_ago = time.time() - 2 * 3600
                os.utime(identity, (two_hours_ago, two_hours_ago))
                recorded = manager._last_exit("strict-test")

        self.assertIsNotNone(recorded)
        data, age = recorded
        self.assertEqual(data["country_code"], "JP")
        self.assertAlmostEqual(age, 2 * 3600, delta=60)

    def test_tilde_abbreviates_only_the_host_home(self) -> None:
        home = str(Path.home())
        self.assertEqual(manager._tilde(home), "~")
        self.assertEqual(manager._tilde(f"{home}/code/vault"), "~/code/vault")
        self.assertEqual(manager._tilde("~/Downloads"), "~/Downloads")
        self.assertEqual(manager._tilde(f"{home}-other/x"), f"{home}-other/x")


if __name__ == "__main__":
    unittest.main()
