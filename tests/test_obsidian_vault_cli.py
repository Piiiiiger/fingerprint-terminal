from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "host/obsidian-vault-cli.py"
SPEC = importlib.util.spec_from_file_location("obsidian_vault_cli", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class ObsidianVaultCliTests(unittest.TestCase):
    def test_delete_moves_file_to_vault_trash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "未命名.base"
            source.write_text("views:\n", encoding="utf-8")
            self.assertEqual(
                MODULE.run([f"vault={root.name}", "delete", "path=未命名.base"], root),
                0,
            )
            self.assertFalse(source.exists())
            self.assertEqual((root / ".trash/未命名.base").read_text(), "views:\n")

    def test_rejects_parent_path_and_unsupported_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(MODULE.run(["delete", "path=../outside"], root), 2)
            self.assertEqual(MODULE.run(["files"], root), 2)

    def test_discovers_vault_from_nested_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".obsidian").mkdir()
            nested = root / "notes" / "project"
            nested.mkdir(parents=True)
            with mock.patch.object(MODULE.Path, "cwd", return_value=nested):
                self.assertEqual(MODULE.vault_root(), root.resolve())

    def test_rejects_another_vault_without_moving_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "note.md"
            source.write_text("keep", encoding="utf-8")
            self.assertEqual(MODULE.run(["vault=another-vault", "delete", "path=note.md"], root), 2)
            self.assertEqual(source.read_text(), "keep")


if __name__ == "__main__":
    unittest.main()
