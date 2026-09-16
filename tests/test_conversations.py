from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fingerprint_terminal.conversations import ConversationManager


CLAUDE_ID = "11111111-1111-4111-8111-111111111111"
CODEX_ID = "22222222-2222-4222-8222-222222222222"


class ConversationManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / "strict-auto-ip" / "home"
        self.home.mkdir(parents=True)
        self._make_claude()
        self._make_codex()
        self.manager = ConversationManager(
            profile_data_root=self.root,
            now=lambda: 2_000_000_000.0,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _make_claude(self) -> None:
        root = self.home / ".claude"
        project = root / "projects" / "-home-test"
        project.mkdir(parents=True)
        records = [
            {"type": "mode", "sessionId": CLAUDE_ID},
            {
                "type": "ai-title",
                "aiTitle": "Claude 测试会话",
                "sessionId": CLAUDE_ID,
            },
            {
                "type": "last-prompt",
                "lastPrompt": "最后一个问题",
                "sessionId": CLAUDE_ID,
            },
            {
                "type": "user",
                "sessionId": CLAUDE_ID,
                "cwd": "/home/test/project",
                "timestamp": "2026-09-16T12:00:00Z",
            },
        ]
        (project / f"{CLAUDE_ID}.jsonl").write_text(
            "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in records),
            encoding="utf-8",
        )
        subagent = project / CLAUDE_ID / "subagents"
        subagent.mkdir(parents=True)
        (subagent / "agent.jsonl").write_text("{}\n", encoding="utf-8")
        for sidecar in ("file-history", "session-env"):
            path = root / sidecar / CLAUDE_ID
            path.mkdir(parents=True)
            (path / "state").write_text("x", encoding="utf-8")
        history = [
            {"display": "hello", "timestamp": 1000, "sessionId": CLAUDE_ID},
            {"display": "keep", "timestamp": 2000, "sessionId": "other"},
        ]
        (root / "history.jsonl").write_text(
            "".join(json.dumps(item) + "\n" for item in history),
            encoding="utf-8",
        )

    def _make_codex(self) -> None:
        root = self.home / ".codex"
        session_dir = root / "sessions" / "2026" / "09" / "16"
        session_dir.mkdir(parents=True)
        rollout = session_dir / f"rollout-2026-09-16T12-00-00-{CODEX_ID}.jsonl"
        rollout.write_text(
            json.dumps(
                {
                    "timestamp": "2026-09-16T12:00:00Z",
                    "type": "session_meta",
                    "payload": {
                        "id": CODEX_ID,
                        "cwd": "/home/test/codex",
                        "timestamp": "2026-09-16T12:00:00Z",
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        (root / "shell_snapshots").mkdir()
        (root / "shell_snapshots" / f"{CODEX_ID}.123.sh").write_text("echo ok\n")
        (root / "history.jsonl").write_text(
            json.dumps({"session_id": CODEX_ID, "ts": 1000, "text": "hello"})
            + "\n"
            + json.dumps({"session_id": "other", "ts": 2000, "text": "keep"})
            + "\n",
            encoding="utf-8",
        )
        (root / "session_index.jsonl").write_text(
            json.dumps({"id": CODEX_ID, "thread_name": "test", "updated_at": "2026-09-16T12:00:00Z"})
            + "\n"
            + json.dumps({"id": "other", "thread_name": "keep", "updated_at": "2026-09-16T13:00:00Z"})
            + "\n",
            encoding="utf-8",
        )

        state = sqlite3.connect(root / "state_5.sqlite")
        state.execute(
            "create table threads (id text primary key, rollout_path text, cwd text, title text, "
            "first_user_message text, preview text, name text, updated_at_ms integer)"
        )
        state.execute("create table thread_artifacts (id integer, thread_id text, payload text)")
        state.execute(
            "insert into threads values (?,?,?,?,?,?,?,?)",
            (CODEX_ID, str(rollout), "/home/test/codex", "Codex 测试会话", "hello", "hello", "test", 2_000_000),
        )
        state.execute("insert into thread_artifacts values (1,?,?)", (CODEX_ID, "artifact"))
        state.commit()
        state.close()

        history = sqlite3.connect(root / "thread_history_1.sqlite")
        history.execute("create table thread_turns (thread_id text, turn_id text, status text)")
        history.execute("insert into thread_turns values (?,?,?)", (CODEX_ID, "turn-1", "done"))
        history.commit()
        history.close()

    def test_discovers_both_providers_and_categories(self) -> None:
        records = {item.key: item for item in self.manager.discover()}
        self.assertIn(f"claude:{CLAUDE_ID}", records)
        self.assertIn(f"codex:{CODEX_ID}", records)
        self.assertEqual(records[f"claude:{CLAUDE_ID}"].title, "Claude 测试会话")
        self.assertEqual(records[f"codex:{CODEX_ID}"].title, "Codex 测试会话")

        self.manager.add_category("重要项目")
        self.manager.assign_category(f"claude:{CLAUDE_ID}", "重要项目")
        records = {item.key: item for item in self.manager.discover()}
        self.assertEqual(records[f"claude:{CLAUDE_ID}"].category, "重要项目")

    @patch("fingerprint_terminal.conversations.provider_is_running", return_value=False)
    def test_bulk_category_trash_can_be_scoped_to_provider(self, _running) -> None:
        claude_key = f"claude:{CLAUDE_ID}"
        codex_key = f"codex:{CODEX_ID}"
        self.manager.assign_category(claude_key, "工作")
        self.manager.assign_category(codex_key, "工作")
        moved, errors = self.manager.move_category_to_trash("工作", provider="claude")
        self.assertEqual((moved, errors), (1, []))
        remaining = {item.key for item in self.manager.discover()}
        self.assertNotIn(claude_key, remaining)
        self.assertIn(codex_key, remaining)

    @patch("fingerprint_terminal.conversations.provider_is_running", return_value=False)
    def test_claude_trash_hides_and_restore_recovers(self, _running) -> None:
        key = f"claude:{CLAUDE_ID}"
        self.manager.move_to_trash(key)
        self.assertNotIn(key, {item.key for item in self.manager.discover()})
        self.assertIn(key, {item.key for item in self.manager.trash_entries()})
        history = (self.home / ".claude" / "history.jsonl").read_text()
        self.assertNotIn(CLAUDE_ID, history)
        self.assertFalse((self.home / ".claude" / "file-history" / CLAUDE_ID).exists())

        self.manager.restore(key)
        self.assertIn(key, {item.key for item in self.manager.discover()})
        self.assertNotIn(key, {item.key for item in self.manager.trash_entries()})
        self.assertIn(CLAUDE_ID, (self.home / ".claude" / "history.jsonl").read_text())
        self.assertTrue((self.home / ".claude" / "file-history" / CLAUDE_ID).exists())

    @patch("fingerprint_terminal.conversations.provider_is_running", return_value=False)
    def test_codex_trash_removes_indexes_and_sqlite_then_restores(self, _running) -> None:
        key = f"codex:{CODEX_ID}"
        self.manager.move_to_trash(key)
        self.assertNotIn(key, {item.key for item in self.manager.discover()})
        self.assertNotIn(CODEX_ID, (self.home / ".codex" / "history.jsonl").read_text())
        self.assertNotIn(CODEX_ID, (self.home / ".codex" / "session_index.jsonl").read_text())
        state = sqlite3.connect(self.home / ".codex" / "state_5.sqlite")
        self.assertEqual(state.execute("select count(*) from threads where id=?", (CODEX_ID,)).fetchone()[0], 0)
        self.assertEqual(state.execute("select count(*) from thread_artifacts where thread_id=?", (CODEX_ID,)).fetchone()[0], 0)
        state.close()

        self.manager.restore(key)
        self.assertIn(key, {item.key for item in self.manager.discover()})
        state = sqlite3.connect(self.home / ".codex" / "state_5.sqlite")
        self.assertEqual(state.execute("select count(*) from threads where id=?", (CODEX_ID,)).fetchone()[0], 1)
        self.assertEqual(state.execute("select count(*) from thread_artifacts where thread_id=?", (CODEX_ID,)).fetchone()[0], 1)
        state.close()

    @patch("fingerprint_terminal.conversations.provider_is_running", return_value=False)
    def test_expired_trash_is_permanently_removed(self, _running) -> None:
        key = f"claude:{CLAUDE_ID}"
        self.manager.move_to_trash(key)
        later = ConversationManager(
            profile_data_root=self.root,
            now=lambda: 2_000_000_000.0 + 15 * 86400,
        )
        self.assertEqual(later.cleanup_expired(), 1)
        self.assertNotIn(key, {item.key for item in later.trash_entries()})


    @patch("fingerprint_terminal.conversations.provider_is_running", return_value=False)
    def test_rename_claude_updates_session_and_discovery(self, _running) -> None:
        key = f"claude:{CLAUDE_ID}"
        self.manager.rename_conversation(key, "新的 Claude 标题")
        record = next(item for item in self.manager.discover() if item.key == key)
        self.assertEqual(record.title, "新的 Claude 标题")
        values = [json.loads(line) for line in Path(record.source_path).read_text().splitlines()]
        self.assertEqual(values[-1]["type"], "ai-title")
        self.assertEqual(values[-1]["aiTitle"], "新的 Claude 标题")
        self.assertEqual(values[-1]["sessionId"], CLAUDE_ID)

    @patch("fingerprint_terminal.conversations.provider_is_running", return_value=False)
    def test_rename_codex_updates_database_index_and_discovery(self, _running) -> None:
        key = f"codex:{CODEX_ID}"
        self.manager.rename_conversation(key, "新的 Codex 标题")
        record = next(item for item in self.manager.discover() if item.key == key)
        self.assertEqual(record.title, "新的 Codex 标题")

        connection = sqlite3.connect(self.manager.home / ".codex" / "state_5.sqlite")
        row = connection.execute(
            "select title from threads where id = ?", (CODEX_ID,)
        ).fetchone()
        connection.close()
        self.assertEqual(row, ("新的 Codex 标题",))

        index = self.manager.home / ".codex" / "session_index.jsonl"
        names = []
        for line in index.read_text().splitlines():
            value = json.loads(line)
            if value.get("id") == CODEX_ID:
                names.append(value.get("thread_name"))
        self.assertEqual(names, ["新的 Codex 标题"])


if __name__ == "__main__":
    unittest.main()
