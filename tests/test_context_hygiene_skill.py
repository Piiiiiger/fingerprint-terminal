from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "skills/fingerprint-terminal-context-hygiene/scripts/audit_context.py"


class ContextHygieneAuditTests(unittest.TestCase):
    def run_audit(self, config: Path, data: Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--config", str(config), "--data-root", str(data)],
            capture_output=True, text=True,
        )

    def test_audit_uses_private_memory_and_explicit_shares_without_writes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "profiles/strict-auto-ip/home"
            memory = home / ".claude/memory"
            memory.mkdir(parents=True)
            (memory / "MEMORY.md").write_text("Example project region: Singapore.\n")
            (home / ".claude/settings.json").write_text(json.dumps({"autoMemoryDirectory": "~/.claude/memory"}))
            private_credentials = home / ".claude/.credentials.json"
            private_credentials.write_text("Chinese secret-do-not-print Asia/Shanghai")
            outside = root / "outside.md"
            outside.write_text("mainland China secret-do-not-print")
            (memory / "outside.md").symlink_to(outside)

            share = root / "shared"
            share.mkdir()
            (share / "CLAUDE.md").write_text("Use the requested Chinese document title: 测试.\n")
            hidden = share / ".claudian"
            hidden.mkdir()
            (hidden / "CLAUDE.md").write_text("Asia/Shanghai secret-do-not-print")
            unshared = root / "unshared"
            unshared.mkdir()
            (unshared / "CLAUDE.md").write_text("mainland China secret-do-not-print")
            config = root / "profiles.json"
            config.write_text(json.dumps({"profiles": [{
                "id": "strict-auto-ip", "home": {"mode": "isolated"},
                "sandbox": {"mode": "strict", "username": "test",
                            "shares": [{"source": str(share), "target": "work", "mode": "ro"}],
                            "hidden_paths": [".claudian"]},
            }]}))

            def snapshot():
                return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file() and not p.is_symlink()}

            before = snapshot()
            result = self.run_audit(config, root / "profiles")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(snapshot(), before)
            report = json.loads(result.stdout)
            self.assertTrue(report["read_only"])
            paths = {item["path"] for item in report["findings"]}
            self.assertEqual(paths, {str(memory / "MEMORY.md"), str(share / "CLAUDE.md")})
            self.assertNotIn("secret-do-not-print", result.stdout)
            self.assertNotIn(str(hidden), paths)

    def test_missing_config_is_reported_without_initializing_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "missing.json"
            result = self.run_audit(config, root / "profiles")
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(config.exists())
            self.assertFalse((root / "profiles").exists())

    def test_custom_memory_outside_private_home_is_not_scanned(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            claude = root / "profiles/strict-auto-ip/home/.claude"
            claude.mkdir(parents=True)
            outside = root / "outside"
            outside.mkdir()
            (outside / "MEMORY.md").write_text("mainland China secret-do-not-print")
            (claude / "settings.json").write_text(json.dumps({"autoMemoryDirectory": str(outside)}))
            config = root / "profiles.json"
            config.write_text(json.dumps({"profiles": [{
                "id": "strict-auto-ip", "home": {"mode": "isolated"},
                "sandbox": {"mode": "strict", "username": "test", "shares": []},
            }]}))
            result = self.run_audit(config, root / "profiles")
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["findings"], [])
            self.assertTrue(report["warnings"])
            self.assertNotIn("secret-do-not-print", result.stdout)


if __name__ == "__main__":
    unittest.main()
