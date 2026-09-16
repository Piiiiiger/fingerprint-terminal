from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from fingerprint_terminal.conversation_ui import _date_bound


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


if __name__ == "__main__":
    unittest.main()
