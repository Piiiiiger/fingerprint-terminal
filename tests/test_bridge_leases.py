from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fingerprint_terminal import bridge_leases


class BridgeLeaseTests(unittest.TestCase):
    def roots(self, temporary: str):
        root = Path(temporary)
        return (
            mock.patch.object(bridge_leases, "STATE_ROOT", root / "state"),
            mock.patch.object(bridge_leases, "PROFILE_DATA_ROOT", root / "profiles"),
        )

    def test_last_lease_removes_empty_target_and_parent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_patch, profiles_patch = self.roots(temporary)
            with state_patch, profiles_patch:
                target = (
                    bridge_leases.PROFILE_DATA_ROOT
                    / "strict-test"
                    / "home"
                    / "code"
                    / "vault"
                )
                target.mkdir(parents=True)
                lease = bridge_leases.create_bridge_lease(
                    "strict-test", {"code/vault"}, {"vault"}
                )
                self.assertIsNotNone(lease)

                removed = bridge_leases.release_bridge_lease(lease)

                self.assertEqual(removed, 2)
                self.assertFalse(target.parent.exists())

    def test_concurrent_lease_keeps_target_until_last_release(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_patch, profiles_patch = self.roots(temporary)
            with state_patch, profiles_patch:
                target = (
                    bridge_leases.PROFILE_DATA_ROOT
                    / "strict-test"
                    / "home"
                    / "code"
                    / "vault"
                )
                target.mkdir(parents=True)
                first = bridge_leases.create_bridge_lease(
                    "strict-test", {"code/vault"}, {"vault"}
                )
                second = bridge_leases.create_bridge_lease(
                    "strict-test", {"code/vault"}, {"vault"}
                )
                self.assertIsNotNone(first)
                self.assertIsNotNone(second)

                self.assertEqual(bridge_leases.release_bridge_lease(first), 0)
                self.assertTrue(target.is_dir())
                self.assertEqual(bridge_leases.release_bridge_lease(second), 2)
                self.assertFalse(target.parent.exists())

    def test_nonempty_target_and_protected_parent_are_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_patch, profiles_patch = self.roots(temporary)
            with state_patch, profiles_patch:
                home = bridge_leases.PROFILE_DATA_ROOT / "strict-test" / "home"
                target = home / "code" / "vault"
                target.mkdir(parents=True)
                (target / "keep.txt").write_text("keep", encoding="utf-8")
                lease = bridge_leases.create_bridge_lease(
                    "strict-test", {"code/vault"}, {"code"}
                )
                self.assertIsNotNone(lease)
                self.assertEqual(bridge_leases.release_bridge_lease(lease), 0)
                self.assertTrue((target / "keep.txt").is_file())

                (target / "keep.txt").unlink()
                lease = bridge_leases.create_bridge_lease(
                    "strict-test", {"code/vault"}, {"code"}
                )
                self.assertIsNotNone(lease)
                self.assertEqual(bridge_leases.release_bridge_lease(lease), 1)
                self.assertTrue((home / "code").is_dir())

    def test_refresh_collects_stale_lease(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_patch, profiles_patch = self.roots(temporary)
            with state_patch, profiles_patch:
                target = (
                    bridge_leases.PROFILE_DATA_ROOT
                    / "strict-test"
                    / "home"
                    / "code"
                    / "vault"
                )
                target.mkdir(parents=True)
                lease = bridge_leases.create_bridge_lease(
                    "strict-test", {"code/vault"}, {"vault"}
                )
                self.assertIsNotNone(lease)
                manifest = json.loads(lease.read_text(encoding="utf-8"))
                manifest["pid"] = 999_999_999
                lease.write_text(json.dumps(manifest), encoding="utf-8")

                removed = bridge_leases.cleanup_stale_bridge_leases(
                    "strict-test", {"vault"}
                )

                self.assertEqual(removed, 2)
                self.assertFalse(target.parent.exists())


if __name__ == "__main__":
    unittest.main()
