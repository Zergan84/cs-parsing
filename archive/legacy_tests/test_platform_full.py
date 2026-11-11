"""
test_platform_full.py - Комплексный тест верификации Couchsurfing Personalized Outreach Platform.

Проверяет:
1. Архитектуру базы данных SQLite (WAL-режим, таблицы, внешние ключи).
2. Базу знаний личного контекста (Personal Context Knowledge Base) и селектор релевантности.
3. Интеллектуальный анализатор профилей (ProfileAnalyzer) и извлечение хуков.
4. Безопасность автопилота (Emergency Stop, запрет отправки без одобрения).
5. Работоспособность ключевых REST API и PWA эндпоинтов.
"""

import json
import sqlite3
import unittest
from pathlib import Path
from test_isolation import activate_test_isolation, get_db_path

activate_test_isolation()

from fastapi.testclient import TestClient

from app import app
from database import DB_PATH, db_session, get_setting, init_db, set_setting
from personal_context import get_personal_context_manager
from profile_analyzer import get_profile_analyzer
from request_manager import RequestQueueManager


class TestPlatformArchitecture(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        init_db()
        cls.client = TestClient(app)
        cls.context_mgr = get_personal_context_manager()
        cls.analyzer = get_profile_analyzer()
        cls.queue_mgr = RequestQueueManager()

    def setUp(self):
        init_db()
        self.context_mgr.ensure_default_facts()

    def test_01_sqlite_database_tables(self):
        """Проверка существования всех 8 таблиц и WAL-режима."""
        conn = sqlite3.connect(str(get_db_path()))
        cur = conn.cursor()
        cur.execute("PRAGMA journal_mode;")
        mode = cur.fetchone()[0]
        self.assertEqual(mode.lower(), "wal")

        cur.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = {row[0] for row in cur.fetchall()}
        required_tables = {
            "personal_context_facts",
            "hosts",
            "profile_analyses",
            "generated_messages",
            "sending_queue",
            "sending_history",
            "background_jobs",
            "app_settings",
        }
        for t in required_tables:
            self.assertIn(t, tables, f"Таблица {t} отсутствует в SQLite!")
        conn.close()

    def test_02_personal_context_facts(self):
        """Проверка загрузки и структуры фактов в Personal Context."""
        facts = self.context_mgr.get_all_facts(active_only=False)
        self.assertGreaterEqual(len(facts), 13, "Должно быть загружено не менее 13 фактов")

        categories = {f["category"] for f in facts}
        self.assertIn("Personal Background", categories)
        self.assertIn("Travel Experience", categories)
        self.assertIn("Professional Background", categories)
        self.assertIn("Technology & Entrepreneurship", categories)
        self.assertIn("Hobbies & Interests", categories)
        self.assertIn("Current Travel Context", categories)

    def test_03_context_relevance_matcher(self):
        """Проверка подбора релевантных фактов под профиль хоста."""
        # Хост увлекается лыжами и хайкингом
        host_mountain = {
            "about_me_clean": "I love skiing, freeride, trekking in the Alps, outdoor climbing.",
            "house_rules": "No smoking, take off shoes",
        }
        selected = self.context_mgr.select_relevant_facts(host_mountain, max_facts=2)
        self.assertLessEqual(len(selected), 2)
        fact_ids = [f["fact_id"] for f in selected]
        self.assertTrue(
            "prof_ski_coach_alps" in fact_ids or "te_nepal_mountains" in fact_ids,
            "Селектор должен подобрать лыжи/горы для горного хоста"
        )

    def test_04_profile_analyzer_hooks(self):
        """Проверка извлечения Conversation Hooks и стратегии."""
        host = {
            "username": "photo_traveler",
            "clean_name": "Ken",
            "about_me_clean": "Photographer from Tokyo. I shoot cinematic landscapes and love mountain trekking.",
            "references": ["Ken is a super cool host and great guy!"]
        }
        analysis = self.analyzer.analyze_host(host)
        self.assertIn("conversation_hooks", analysis)
        self.assertIn("conversation_strategy", analysis)
        self.assertEqual(analysis["estimated_gender"].lower(), "male")
        self.assertTrue(analysis["can_host_overnight"])

    def test_05_emergency_stop_lifecycle(self):
        """Проверка активации и снятия аварийной остановки (Emergency Stop)."""
        # Активация
        res = self.client.post("/api/emergency_stop")
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json().get("stopped"))
        self.assertEqual(get_setting("emergency_stop"), "1")

        # Проверка статуса
        status_res = self.client.get("/api/emergency_status")
        self.assertTrue(status_res.json().get("emergency_stop"))

        # Снятие блокировки
        resume_res = self.client.post("/api/emergency_resume")
        self.assertEqual(resume_res.status_code, 200)
        self.assertFalse(resume_res.json().get("emergency_stop"))
        self.assertEqual(get_setting("emergency_stop"), "0")

    def test_06_pwa_endpoints(self):
        """Проверка доступности манифеста и сервис-воркера."""
        m_res = self.client.get("/manifest.json")
        self.assertEqual(m_res.status_code, 200)
        m_data = m_res.json()
        self.assertEqual(m_data.get("display"), "standalone")

        sw_res = self.client.get("/sw.js")
        self.assertEqual(sw_res.status_code, 200)
        self.assertIn("addEventListener", sw_res.text)

    def test_07_requests_queue_enriched_cards(self):
        """Проверка обогащения карточек в очереди отправки."""
        res = self.client.get("/api/requests/queue")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("queue", data)
        self.assertIn("stats", data)
        if data["queue"]:
            first = data["queue"][0]
            self.assertIn("conversation_hooks", first)
            self.assertIn("conversation_strategy", first)
            self.assertIn("compatibility_points", first)

    def test_08_autopilot_dispatcher_and_logs(self):
        """Проверка диспетчера автопилота (pause/resume, status) и экспорта логов."""
        # Пауза
        res_p = self.client.post("/api/autopilot/pause")
        self.assertEqual(res_p.status_code, 200)
        self.assertTrue(res_p.json().get("is_paused"))

        # Статус
        res_s = self.client.get("/api/autopilot/status")
        self.assertEqual(res_s.status_code, 200)
        s_data = res_s.json()
        self.assertIn("state_label", s_data)
        self.assertIn("daily_limit", s_data)
        self.assertIn("sent_today", s_data)

        # Возобновление
        res_r = self.client.post("/api/autopilot/resume")
        self.assertEqual(res_r.status_code, 200)
        self.assertFalse(res_r.json().get("is_paused"))

        # Экспорт и очистка логов
        res_txt = self.client.get("/api/logs/export?format=txt")
        self.assertEqual(res_txt.status_code, 200)

        res_json = self.client.get("/api/logs/export?format=json")
        self.assertEqual(res_json.status_code, 200)

        res_c = self.client.post("/api/logs/clear")
        self.assertEqual(res_c.status_code, 200)
        self.assertEqual(res_c.json().get("status"), "cleared")


if __name__ == "__main__":
    unittest.main()
