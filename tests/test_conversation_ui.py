from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta
from types import SimpleNamespace

from fingerprint_terminal.conversation_ui import ConversationWindow, _date_bound
from fingerprint_terminal.conversations import Conversation


class ConversationUiTests(unittest.TestCase):
    def test_date_bound_uses_local_calendar_day(self) -> None:
        day = datetime.strptime("2026-09-16", "%Y-%m-%d")
        self.assertEqual(_date_bound("2026-09-16"), day.timestamp())
        self.assertEqual(
            _date_bound("2026-09-16", end=True),
            (day + timedelta(days=1)).timestamp(),
        )

    def test_date_bound_rejects_invalid_date(self) -> None:
        with self.assertRaises(ValueError):
            _date_bound("2026-99-99")

    def test_available_calendar_dates_follow_provider_and_category(self) -> None:
        def record(provider: str, day: int, category: str) -> Conversation:
            return Conversation(
                provider=provider,
                session_id=f"{provider}-{day}-{category}",
                title="test",
                cwd="/home/test",
                updated_at=datetime(2026, 9, day, 12, 0).timestamp(),
                source_path="/tmp/test.jsonl",
                category=category,
            )

        dummy = SimpleNamespace(
            selected_provider="codex",
            selected_category="全部",
            conversations=[
                record("codex", 16, "工作"),
                record("codex", 16, "学习"),
                record("codex", 18, "工作"),
                record("claude", 17, "工作"),
            ],
        )
        counts = ConversationWindow._available_date_counts(dummy)
        self.assertEqual(counts, {date(2026, 9, 16): 2, date(2026, 9, 18): 1})

        dummy.selected_category = "工作"
        counts = ConversationWindow._available_date_counts(dummy)
        self.assertEqual(counts, {date(2026, 9, 16): 1, date(2026, 9, 18): 1})


if __name__ == "__main__":
    unittest.main()
