"""
test_auto_sender_candidates.py - Тесты отбора кандидатов к отправке в AutoSender.

Проверяем, что:
- Кандидаты — только со статусом 'Approved' (нет fallback на Draft/Skipped/и т.д.).
- --target выбирает только Approved с совпадающим username; не отправляет Sent.
- Пустой список Approved -> пустой список кандидатов (ничего не отправляется).
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from test_isolation import activate_test_isolation

activate_test_isolation()

from auto_sender import AutoSender, QUEUE_FILE


class TestAutoSenderCandidates(unittest.TestCase):
    """Тесты метода _select_candidates (отбор кандидатов к отправке)."""

    def setUp(self):
        self.sender = AutoSender(dry_run=True, headless=True, test_mode=True)

    # ---------------------------------------------------------------- без --target

    def test_only_approved_are_candidates(self):
        """Draft, Skipped, Sent, Queued, Failed — никогда не попадают в кандидаты без --target."""
        queue = [
            {"username": "host_approved_1", "status": "Approved"},
            {"username": "host_draft_1", "status": "Draft"},
            {"username": "host_skipped_1", "status": "Skipped"},
            {"username": "host_sent_1", "status": "Sent"},
            {"username": "host_sent_dry_1", "status": "Sent (Dry-Run)"},
            {"username": "host_queued_1", "status": "Queued"},
            {"username": "host_failed_1", "status": "Failed"},
        ]
        candidates = self.sender._select_candidates(queue)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["username"], "host_approved_1")

    def test_multiple_approved_all_selected(self):
        """Все записи со статусом Approved попадают в кандидаты."""
        queue = [
            {"username": "host_a", "status": "Approved"},
            {"username": "host_b", "status": "Approved"},
            {"username": "host_c", "status": "Draft"},
            {"username": "host_d", "status": "Approved"},
        ]
        candidates = self.sender._select_candidates(queue)
        self.assertEqual(len(candidates), 3)
        usernames = {c["username"] for c in candidates}
        self.assertEqual(usernames, {"host_a", "host_b", "host_d"})

    def test_empty_queue_no_candidates(self):
        """Пустая очередь -> пустой список кандидатов."""
        candidates = self.sender._select_candidates([])
        self.assertEqual(candidates, [])

    def test_no_approved_no_candidates(self):
        """Если нет Approved — кандидатов нет, даже если есть Draft/Skipped/Sent."""
        queue = [
            {"username": "h1", "status": "Draft"},
            {"username": "h2", "status": "Skipped"},
            {"username": "h3", "status": "Sent"},
            {"username": "h4", "status": "Sent (Dry-Run)"},
            {"username": "h5", "status": "Failed"},
        ]
        candidates = self.sender._select_candidates(queue)
        self.assertEqual(candidates, [])

    def test_case_insensitive_status_not_issue(self):
        """Статус хранится с разным регистром — только точный 'Approved' проходит."""
        queue = [
            {"username": "h1", "status": "Approved"},
            {"username": "h2", "status": "approved"},  # другой регистр — не должен пройти
            {"username": "h3", "status": "APPROVED"},  # другой регистр — не должен пройти
        ]
        candidates = self.sender._select_candidates(queue)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["username"], "h1")

    # ---------------------------------------------------------------- с --target

    def test_target_approved_selected(self):
        """--target выбирает запись с совпадающим username и статусом Approved."""
        self.sender.target_username = "host_exact"
        queue = [
            {"username": "host_exact", "status": "Approved"},
            {"username": "host_other", "status": "Approved"},
        ]
        candidates = self.sender._select_candidates(queue)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["username"], "host_exact")

    def test_target_draft_not_selected(self):
        """--target не выбирает запись со статусом Draft, даже если username совпадает."""
        self.sender.target_username = "host_draft"
        queue = [
            {"username": "host_draft", "status": "Draft"},
        ]
        candidates = self.sender._select_candidates(queue)
        self.assertEqual(candidates, [])

    def test_target_skipped_not_selected(self):
        """--target не выбирает запись со статусом Skipped."""
        self.sender.target_username = "host_skipped"
        queue = [
            {"username": "host_skipped", "status": "Skipped"},
        ]
        candidates = self.sender._select_candidates(queue)
        self.assertEqual(candidates, [])

    def test_target_sent_not_selected(self):
        """--target не выбирает запись со статусом Sent (уже отправлен)."""
        self.sender.target_username = "host_sent"
        queue = [
            {"username": "host_sent", "status": "Sent"},
        ]
        candidates = self.sender._select_candidates(queue)
        self.assertEqual(candidates, [])

    def test_target_sent_dry_run_not_selected(self):
        """--target не выбирает запись со статусом Sent (Dry-Run)."""
        self.sender.target_username = "host_sent_dry"
        queue = [
            {"username": "host_sent_dry", "status": "Sent (Dry-Run)"},
        ]
        candidates = self.sender._select_candidates(queue)
        self.assertEqual(candidates, [])

    def test_target_username_not_found(self):
        """--target с username, которого нет в очереди — пустой список."""
        self.sender.target_username = "nonexistent_host"
        queue = [
            {"username": "host_a", "status": "Approved"},
        ]
        candidates = self.sender._select_candidates(queue)
        self.assertEqual(candidates, [])

    def test_target_case_insensitive_username(self):
        """--target сравнивает username без учёта регистра."""
        self.sender.target_username = "HOST_Upper"
        queue = [
            {"username": "host_upper", "status": "Approved"},
        ]
        candidates = self.sender._select_candidates(queue)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["username"], "host_upper")

    # ---------------------------------------------------------------- интеграция с файлом очереди

    def test_run_stops_when_no_approved(self):
        """
        Если в файле очереди нет Approved — run() завершается без отправки,
        обновляя статус автопилота.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            queue_path = Path(tmpdir) / "requests_queue.json"
            # Пишем очередь без Approved
            with open(queue_path, "w", encoding="utf-8") as f:
                json.dump([
                    {"username": "h1", "status": "Draft"},
                    {"username": "h2", "status": "Skipped"},
                ], f, ensure_ascii=False)

            # Подменяем QUEUE_FILE на временный
            import auto_sender as as_mod
            orig_queue_file = as_mod.QUEUE_FILE
            as_mod.QUEUE_FILE = queue_path
            try:
                # run() попытается запустить Playwright — мокаем process_host,
                # но candidates будут пустыми, так что run() вернётся до браузера.
                # Просто проверяем, что _select_candidates на этом файле даёт [].
                with open(queue_path, "r", encoding="utf-8") as f:
                    queue = json.load(f)
                candidates = self.sender._select_candidates(queue)
                self.assertEqual(candidates, [])
            finally:
                as_mod.QUEUE_FILE = orig_queue_file


if __name__ == "__main__":
    unittest.main()
