"""
test_auto_sender_dry_run_and_target.py - Тесты поведения автопилота без сети и браузера.

Проверяем:
1. --dry-run НЕ меняет статус записи в requests_queue.json (остаётся Approved);
   результат пишется только в sent_log.json со статусом dry_run_success.
2. При --target: запись найдена, но статус не Approved -> явное сообщение с текущим
   статусом, завершение без отправки и без запуска браузера.
   Записи нет -> отдельное сообщение.
3. При пустом списке кандидатов run() не вызывает sync_playwright и не пишет в sent_log.
"""
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

import auto_sender as as_mod


def _make_sender(dry_run=False, target_username=None):
    sender = as_mod.AutoSender(
        dry_run=dry_run,
        headless=True,
        test_mode=True,
        target_username=target_username,
        daily_limit=1,
    )
    return sender


def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


class TestDryRunDoesNotChangeQueue(unittest.TestCase):
    """Драй-ран не должен менять статус в очереди (остаётся Approved)."""

    def test_record_dry_run_success_writes_only_sent_log(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            queue_file = Path(tmpdir) / "requests_queue.json"
            sent_log_file = Path(tmpdir) / "sent_log.json"
            _write_json(queue_file, [{"username": "host_draft", "status": "Approved"}])

            as_mod.QUEUE_FILE = queue_file
            as_mod.SENT_LOG_FILE = sent_log_file
            try:
                sender = _make_sender(dry_run=True)
                sender._record_dry_run_success(
                    username="host_draft",
                    host_name="Host Draft",
                    target_city="Tokyo",
                    arrival_date="2026-11-16",
                    departure_date="2026-11-19",
                    before_screenshot=Path(tmpdir) / "before_send_host_draft.png",
                    found_selector="Send Request button",
                )

                # sent_log содержит запись со статусом dry_run_success
                log = json.loads(sent_log_file.read_text(encoding="utf-8"))
                self.assertEqual(len(log), 1)
                self.assertEqual(log[0]["status"], "dry_run_success")
                self.assertEqual(log[0]["mode"], "dry_run")
                self.assertEqual(log[0]["username"], "host_draft")

                # Статус в очереди НЕ изменился — остался Approved
                queue = json.loads(queue_file.read_text(encoding="utf-8"))
                self.assertEqual(len(queue), 1)
                self.assertEqual(queue[0]["status"], "Approved")
                self.assertNotIn("sent_at", queue[0])
            finally:
                as_mod.QUEUE_FILE = as_mod.OUTPUT_DIR / "requests_queue.json"
                as_mod.SENT_LOG_FILE = as_mod.OUTPUT_DIR / "sent_log.json"


class TestRunEmptyCandidatesNoBrowser(unittest.TestCase):
    """Пустой список кандидатов -> sync_playwright не вызывается, sent_log не пишется."""

    def test_no_approved_does_not_launch_browser_and_no_sent_log(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            queue_file = Path(tmpdir) / "requests_queue.json"
            sent_log_file = Path(tmpdir) / "sent_log.json"
            status_file = Path(tmpdir) / "autopilot_status.json"
            _write_json(queue_file, [
                {"username": "h1", "status": "Draft"},
                {"username": "h2", "status": "Skipped"},
            ])

            as_mod.QUEUE_FILE = queue_file
            as_mod.SENT_LOG_FILE = sent_log_file
            as_mod.STATUS_FILE = status_file
            try:
                sender = _make_sender(dry_run=False)

                playwright_mock = MagicMock()
                sent_log_calls = []
                log_calls = []

                with patch.object(as_mod, "sync_playwright", return_value=playwright_mock) as sp_mock, \
                     patch.object(sender, "sync_queue_against_sent_and_inbox", return_value=0), \
                     patch.object(sender, "append_sent_log", side_effect=lambda e: sent_log_calls.append(e)), \
                     patch.object(as_mod, "log_step", side_effect=lambda cat, msg: log_calls.append((cat, msg))):
                    sender.run()

                # Браузер не запускался
                sp_mock.assert_not_called()
                # sent_log не писался
                self.assertEqual(sent_log_calls, [])
                # Узнали об отсутствии одобренных хостов
                self.assertTrue(any("В очереди нет хостов" in msg for _, msg in log_calls))
                # Очередь не изменилась
                queue = json.loads(queue_file.read_text(encoding="utf-8"))
                self.assertEqual(len(queue), 2)
                self.assertEqual(queue[0]["status"], "Draft")
                self.assertEqual(queue[1]["status"], "Skipped")
            finally:
                as_mod.QUEUE_FILE = as_mod.OUTPUT_DIR / "requests_queue.json"
                as_mod.SENT_LOG_FILE = as_mod.OUTPUT_DIR / "sent_log.json"
                as_mod.STATUS_FILE = as_mod.OUTPUT_DIR / "autopilot_status.json"


class TestTargetNotApproved(unittest.TestCase):
    """"--target": запись найдена, но не Approved -> сообщение со статусом, без браузера и отправки."""

    def test_target_draft_sends_nothing_and_reports_status(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            queue_file = Path(tmpdir) / "requests_queue.json"
            sent_log_file = Path(tmpdir) / "sent_log.json"
            _write_json(queue_file, [{"username": "host_draft", "status": "Draft"}])

            as_mod.QUEUE_FILE = queue_file
            as_mod.SENT_LOG_FILE = sent_log_file
            try:
                sender = _make_sender(dry_run=False, target_username="host_draft")

                playwright_mock = MagicMock()
                sent_log_calls = []
                log_calls = []

                with patch.object(as_mod, "sync_playwright", return_value=playwright_mock) as sp_mock, \
                     patch.object(sender, "sync_queue_against_sent_and_inbox", return_value=0), \
                     patch.object(sender, "append_sent_log", side_effect=lambda e: sent_log_calls.append(e)), \
                     patch.object(as_mod, "log_step", side_effect=lambda cat, msg: log_calls.append((cat, msg))):
                    sender.run()

                # Браузер не запускался
                sp_mock.assert_not_called()
                # В sent_log ничего не писалось
                self.assertEqual(sent_log_calls, [])
                # Есть явное сообщение с текущим статусом
                joined = " | ".join(msg for _, msg in log_calls)
                self.assertIn("хост 'host_draft' имеет статус 'draft'", joined.lower())
                self.assertIn("ожидается 'approved'", joined.lower())
                # Очередь не изменилась
                queue = json.loads(queue_file.read_text(encoding="utf-8"))
                self.assertEqual(queue[0]["status"], "Draft")
            finally:
                as_mod.QUEUE_FILE = as_mod.OUTPUT_DIR / "requests_queue.json"
                as_mod.SENT_LOG_FILE = as_mod.OUTPUT_DIR / "sent_log.json"

    def test_target_not_found_separate_message(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            queue_file = Path(tmpdir) / "requests_queue.json"
            sent_log_file = Path(tmpdir) / "sent_log.json"
            _write_json(queue_file, [{"username": "host_other", "status": "Approved"}])

            as_mod.QUEUE_FILE = queue_file
            as_mod.SENT_LOG_FILE = sent_log_file
            try:
                sender = _make_sender(dry_run=False, target_username="nonexistent_host")

                playwright_mock = MagicMock()
                sent_log_calls = []
                log_calls = []

                with patch.object(as_mod, "sync_playwright", return_value=playwright_mock) as sp_mock, \
                     patch.object(sender, "sync_queue_against_sent_and_inbox", return_value=0), \
                     patch.object(sender, "append_sent_log", side_effect=lambda e: sent_log_calls.append(e)), \
                     patch.object(as_mod, "log_step", side_effect=lambda cat, msg: log_calls.append((cat, msg))):
                    sender.run()

                sp_mock.assert_not_called()
                self.assertEqual(sent_log_calls, [])
                joined = " | ".join(msg for _, msg in log_calls)
                self.assertIn("не найден в очереди", joined.lower())
            finally:
                as_mod.QUEUE_FILE = as_mod.OUTPUT_DIR / "requests_queue.json"
                as_mod.SENT_LOG_FILE = as_mod.OUTPUT_DIR / "sent_log.json"


if __name__ == "__main__":
    unittest.main()
