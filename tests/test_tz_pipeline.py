"""
tests/test_tz_pipeline.py - Комплексный тест-сьют на соответствие глобальному ТЗ и архитектуре воронки.
Работает через стандартный unittest на ИЗОЛИРОВАННОЙ тестовой базе данных (zero mutation on production db).

Проверяет все 8 шагов ТЗ:
1. TRIP (Корень агрегата, изоляция по странам/городам/датам, целостность внешних ключей)
2. CONTEXT (Истина о госте, Женева, запрет галлюцинаций)
3. SEARCH & COLLECTOR (Прием, дедупликация, неизменяемый снимок, скоринг 1-5★)
4. SELECTOR (Like / Pass / VIP, переходы состояний)
5. QUEUE + GENERATE (Strict QC: 60-90 слов, проверка чистоты текста, Approve)
6. AUTOPILOT (Выборка только approved, лимит, джиттер 5-10 мин, фиксация sent)
7. INBOX (Фиксация ответов, генерация автоответов)
8. LOGS (Сквозной аудит всех шагов)
"""

import json
import sqlite3
import unittest
from pathlib import Path

from core.database import init_core_tables, get_connection, set_active_trip, get_active_trip
from core.context import GUEST, validate_text_purity
from core.models import HostState
from core.collector import CollectorEngine, calculate_match_score
from core.selector import SelectorEngine
from core.queue import QueueEngine, build_personalized_message
from core.autopilot import AutopilotEngine
from core.inbox import InboxEngine

TEST_DB_PATH = Path("output/test_tz_pipeline.db")
TEST_TRIP_ID = "kyoto-trip-3lhdfema5epclndo82mclxklzfytp-3LhDfEmA5Epclndo82MclXkLzFy_tp"
TEST_URL = f"https://www.couchsurfing.com/c/trips/{TEST_TRIP_ID}"


class TestTZGlobalPipeline(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        """Создает изолированную тестовую базу данных."""
        cls._cleanup_db_files()
        init_core_tables(db_file=TEST_DB_PATH)

    @classmethod
    def tearDownClass(cls):
        """Удаляет изолированную тестовую базу данных."""
        cls._cleanup_db_files()

    @classmethod
    def _cleanup_db_files(cls):
        for ext in ["", "-wal", "-shm"]:
            f = Path(f"{TEST_DB_PATH}{ext}")
            if f.exists():
                try:
                    f.unlink()
                except Exception:
                    pass

    def setUp(self):
        """Очистка таблиц перед каждым тестом."""
        init_core_tables(db_file=TEST_DB_PATH)
        with get_connection(TEST_DB_PATH) as conn:
            conn.execute("DELETE FROM core_audit_logs;")
            conn.execute("DELETE FROM core_hosts;")
            conn.execute("DELETE FROM core_host_snapshots;")
            conn.execute("DELETE FROM core_trips;")

    def _create_test_trip(self):
        with get_connection(TEST_DB_PATH) as conn:
            conn.execute(
                """
                INSERT INTO core_trips (id, url, country, city, arrival_date, departure_date, dates_text, is_active, created_at)
                VALUES (?, ?, 'Japan', 'Kyoto', '2026-11-19', '2026-11-21', 'Nov 19 – 21, 2026', 1, '2026-11-10T00:00:00');
                """,
                (TEST_TRIP_ID, TEST_URL)
            )

    # 1. ТЕСТ: TRIP (Корневой агрегат)
    def test_1_trip_invariant(self):
        """Проверяет, что нельзя вставить хоста без зарегистрированного Trip (Foreign Key)."""
        engine = CollectorEngine(trip_id="non-existent-phantom-trip", db_file=TEST_DB_PATH)
        raw_host = {"id": "ghost_1", "username": "ghost_1", "name": "Ghost"}

        with self.assertRaises(sqlite3.IntegrityError):
            engine.ingest_raw_host(raw_host)

        # Регистрируем валидный Trip
        self._create_test_trip()

        # Теперь добавление с этим trip_id обязано пройти успешно
        valid_engine = CollectorEngine(trip_id=TEST_TRIP_ID, db_file=TEST_DB_PATH)
        card = valid_engine.ingest_raw_host(raw_host)
        self.assertIsNotNone(card)
        self.assertEqual(card.trip_id, TEST_TRIP_ID)

        # Проверяем получение активного трипа через единую точку входа
        active_trip = get_active_trip(db_file=TEST_DB_PATH)
        self.assertIsNotNone(active_trip)
        self.assertEqual(active_trip.city, "Kyoto")
        self.assertEqual(active_trip.country, "Japan")
        self.assertEqual(active_trip.arrival_date, "2026-11-19")

    # 2. ТЕСТ: CONTEXT (Истина о госте и отсутствие галлюцинаций)
    def test_2_context_truth_and_forbidden_terms(self):
        """Проверяет константы гостя (Сибирь, ВГИК, Альпы) и жесткий стоп-лист выдуманных историй."""
        self.assertEqual(GUEST.name, "Andrey")
        self.assertEqual(GUEST.origin_city, "Siberia")
        self.assertEqual(GUEST.origin_country, "Russia")
        self.assertIn("French", GUEST.languages)

        # Достоверные факты о ВГИКе и Сибири должны проходить валидацию
        truthful_text = "I studied film directing at VGIK and grew up in Siberia, working as an alpine ski coach."
        is_pure_truth, violations_truth = validate_text_purity(truthful_text)
        self.assertTrue(is_pure_truth)
        self.assertEqual(len(violations_truth), 0)

        # Выдуманное происхождение (швейцарец/из Женевы) и роботизированные клише должны блокироваться
        fake_text = "I am a swiss citizen born in geneva and won't crowd your space."
        is_pure_fake, violations_fake = validate_text_purity(fake_text)
        self.assertFalse(is_pure_fake)
        self.assertGreaterEqual(len(violations_fake), 2)

    # 3. ТЕСТ: COLLECTOR (Прием, дедупликация, скоринг 1-5★)
    def test_3_collector_ingest_dedup_and_scoring(self):
        """Проверяет дедупликацию, скоринг и неизменяемость снимка."""
        self._create_test_trip()
        collector = CollectorEngine(trip_id=TEST_TRIP_ID, db_file=TEST_DB_PATH)

        host_data = {
            "id": "h_sacha",
            "username": "sacha_travel",
            "name": "Sacha",
            "location": "Kyoto, Japan",
            "about": "Passion for backcountry skiing and documentary film directing.",
            "languages": ["Japanese", "English", "French"],
            "interests": ["mountains", "cinema"]
        }

        card = collector.ingest_raw_host(host_data)
        self.assertIsNotNone(card)
        self.assertGreaterEqual(card.score_stars, 3)
        self.assertTrue(any("Лыжи" in r or "Горы" in r for r in card.score_reasons))
        self.assertTrue(any("Кино" in r or "Фото" in r for r in card.score_reasons))
        self.assertTrue(any("французским" in r.lower() for r in card.score_reasons))

        # Дедупликация: повторный инжест возвращает None и не создает дубликатов
        dup_card = collector.ingest_raw_host(host_data)
        self.assertIsNone(dup_card)

        # Проверяем, что снимок неизменяем и сохранен в core_host_snapshots
        with get_connection(TEST_DB_PATH) as conn:
            cur = conn.cursor()
            cur.execute("SELECT raw_data_json FROM core_host_snapshots WHERE trip_id = ? AND host_id = 'h_sacha';", (TEST_TRIP_ID,))
            row = cur.fetchone()
            self.assertIsNotNone(row)
            saved_raw = json.loads(row[0])
            self.assertEqual(saved_raw["username"], "sacha_travel")

    # 4. ТЕСТ: SELECTOR (Like / Pass / VIP)
    def test_4_selector_decisions(self):
        """Проверяет переходы состояний в Селекторе."""
        self._create_test_trip()
        collector = CollectorEngine(trip_id=TEST_TRIP_ID, db_file=TEST_DB_PATH)
        selector = SelectorEngine(trip_id=TEST_TRIP_ID, db_file=TEST_DB_PATH)

        collector.ingest_raw_host({"id": "h_1", "username": "host_1", "name": "Host One", "about": "skiing and snow"})
        collector.ingest_raw_host({"id": "h_2", "username": "host_2", "name": "Host Two", "about": "just traveling"})

        cands = selector.list_candidates(min_stars=1)
        self.assertEqual(len(cands), 2)

        # h_1 -> LIKE (переходит в selected)
        self.assertTrue(selector.decide("h_1", "like"))

        # h_2 -> PASS (переходит в rejected)
        self.assertTrue(selector.decide("h_2", "pass"))

        # В списке 'discovered' никого не должно остаться
        self.assertEqual(len(selector.list_candidates(min_stars=1)), 0)

        # Проверяем, что h_2 НЕ удален из базы, а имеет статус 'rejected'
        with get_connection(TEST_DB_PATH) as conn:
            cur = conn.cursor()
            cur.execute("SELECT state FROM core_hosts WHERE trip_id = ? AND host_id = 'h_2';", (TEST_TRIP_ID,))
            self.assertEqual(cur.fetchone()[0], "rejected")

    # 5. ТЕСТ: QUEUE + GENERATE (Strict QC 60-90 слов, Approve)
    def test_5_queue_strict_qc_and_approve(self):
        """Проверяет генерацию писем, Strict QC (60-90 слов) и ручной Approve."""
        self._create_test_trip()
        collector = CollectorEngine(trip_id=TEST_TRIP_ID, db_file=TEST_DB_PATH)
        selector = SelectorEngine(trip_id=TEST_TRIP_ID, db_file=TEST_DB_PATH)
        queue = QueueEngine(trip_id=TEST_TRIP_ID, db_file=TEST_DB_PATH)

        collector.ingest_raw_host({"id": "h_art", "username": "art_lover", "name": "Kenji Sato", "about": "street photography and indie cinema"})
        selector.decide("h_art", "like")

        # Генерация черновика
        draft_res = queue.generate_draft("h_art")
        self.assertIsNotNone(draft_res)
        self.assertTrue(draft_res["qc_passed"])

        text = draft_res["text"]
        word_count = len(text.split())
        self.assertTrue(50 <= word_count <= 95, f"Word count was {word_count}")
        self.assertIn("Hi Kenji,", text)
        self.assertIn("Siberia", text)
        self.assertIn("Kyoto", text)

        # Утверждение (Approve)
        self.assertTrue(queue.approve("h_art"))

        # Проверяем, что хост перешел в 'approved'
        approved = queue.list_queue(state="approved")
        self.assertEqual(len(approved), 1)
        self.assertEqual(approved[0]["host_id"], "h_art")

    # 6. ТЕСТ: AUTOPILOT (Jitter, Лимит, Фиксация Sent)
    def test_6_autopilot_dispatch_limit_jitter(self):
        """Проверяет диспетчер отправки, джиттер и суточный лимит."""
        self._create_test_trip()
        collector = CollectorEngine(trip_id=TEST_TRIP_ID, db_file=TEST_DB_PATH)
        selector = SelectorEngine(trip_id=TEST_TRIP_ID, db_file=TEST_DB_PATH)
        queue = QueueEngine(trip_id=TEST_TRIP_ID, db_file=TEST_DB_PATH)
        autopilot = AutopilotEngine(trip_id=TEST_TRIP_ID, daily_limit=2, db_file=TEST_DB_PATH)

        collector.ingest_raw_host({"id": "h_auto", "username": "auto_candidate", "name": "Yuki", "about": "travel and hiking"})
        selector.decide("h_auto", "like")
        queue.generate_draft("h_auto")
        queue.approve("h_auto")

        ready = autopilot.get_ready_candidates()
        self.assertEqual(len(ready), 1)
        self.assertEqual(ready[0]["host_id"], "h_auto")

        # Jitter: 300 - 600 сек (5-10 мин)
        jitter = autopilot.calculate_jitter_seconds()
        self.assertTrue(300 <= jitter <= 600)

        # Фиксация отправки
        self.assertTrue(autopilot.mark_as_sent("h_auto", http_status=200))
        self.assertEqual(len(autopilot.get_ready_candidates()), 0)
        self.assertEqual(autopilot.get_sent_today_count(), 1)

    # 7. ТЕСТ: INBOX (Входящие ответы и предложения автоответа)
    def test_7_inbox_replies(self):
        """Проверяет модуль Inbox и автогенератор ответов."""
        self._create_test_trip()
        collector = CollectorEngine(trip_id=TEST_TRIP_ID, db_file=TEST_DB_PATH)
        inbox = InboxEngine(trip_id=TEST_TRIP_ID, db_file=TEST_DB_PATH)

        collector.ingest_raw_host({"id": "h_reply", "username": "replier", "name": "Akira", "about": "host"})

        self.assertTrue(inbox.record_incoming_reply("h_reply", "What time do you arrive in Kyoto?"))
        convs = inbox.list_conversations()
        self.assertEqual(len(convs), 1)
        self.assertEqual(convs[0]["host_id"], "h_reply")

        suggestion = inbox.generate_reply_suggestion("Akira Tanaka", "What time do you arrive in Kyoto?")
        self.assertIn("Hi Akira,", suggestion)
        self.assertIn("late afternoon", suggestion)
        self.assertIn("Andrey", suggestion)

    # 8. ТЕСТ: LOGS (Сквозной аудит)
    def test_8_audit_logs(self):
        """Проверяет сквозное логирование всех этапов в core_audit_logs."""
        self._create_test_trip()
        collector = CollectorEngine(trip_id=TEST_TRIP_ID, db_file=TEST_DB_PATH)
        collector.ingest_raw_host({"id": "h_log", "username": "log_host", "name": "Log Host"})

        with get_connection(TEST_DB_PATH) as conn:
            cur = conn.cursor()
            cur.execute("SELECT module, event FROM core_audit_logs WHERE trip_id = ? ORDER BY id ASC;", (TEST_TRIP_ID,))
            events = cur.fetchall()
            modules_seen = {e["module"] for e in events}
            self.assertIn("COLLECTOR", modules_seen)

    # 9. ТЕСТ: РАСШИРЕННЫЕ ПОКАЗАТЕЛИ ПРОФИЛЯ И ПРОВЕРКА "НЕТ ДАННЫХ"
    def test_9_rich_profile_indicators_and_empty_check(self):
        """Проверяет извлечение возраста, Verified, страны, языков, интересов и детекцию 'НЕТ ДАННЫХ'."""
        self._create_test_trip()
        collector = CollectorEngine(trip_id=TEST_TRIP_ID, db_file=TEST_DB_PATH)

        # 1. Пустой хост -> Должен получить 1★ и причину 'НЕТ ДАННЫХ'
        empty_host = {"id": "h_empty", "username": "ghost_host", "name": "Empty Host"}
        empty_card = collector.ingest_raw_host(empty_host)
        self.assertIsNotNone(empty_card)
        self.assertEqual(empty_card.score_stars, 1)
        self.assertIn("НЕТ ДАННЫХ", empty_card.score_reasons[0])

        # 2. Наполненный хост со всеми показателями ТЗ
        rich_host = {
            "id": "h_rich",
            "username": "kenji_kyoto",
            "name": "Kenji",
            "age": 34,
            "gender": "male",
            "is_verified": True,
            "birth_country": "Japan",
            "residence_country": "Japan",
            "languages": ["Japanese", "English", "French"],
            "interests": ["Skiing", "Photography", "Piano"],
            "countries_visited": ["France", "Switzerland", "Italy"],
            "countries_lived": ["Japan", "France"],
            "is_hosting": "accepting",
            "household": "alone",
            "special_notes": "Only host 1 person / solo",
            "about": "Ski lover and street photography enthusiast."
        }
        rich_card = collector.ingest_raw_host(rich_host)
        self.assertIsNotNone(rich_card)
        self.assertTrue(rich_card.is_verified)
        self.assertEqual(rich_card.age, 34)
        self.assertEqual(rich_card.birth_country, "Japan")
        self.assertEqual(rich_card.residence_country, "Japan")
        self.assertEqual(rich_card.household, "alone")
        self.assertEqual(rich_card.is_hosting, "accepting")
        self.assertIn("Only host 1 person / solo", rich_card.special_notes)
        self.assertGreaterEqual(rich_card.score_stars, 3)
        self.assertFalse(any("НЕТ ДАННЫХ" in r for r in rich_card.score_reasons))

    # 10. ТЕСТ: УДАЛЕНИЕ ИЗ ОЧЕРЕДИ НЕ УДАЛЯЕТ ИЗ БАЗЫ И СОХРАНЯЕТ СВЯЗЬ С ТРИПОМ (ТЗ)
    def test_10_queue_removal_never_deletes_from_database_and_preserves_trip_link(self):
        """
        Проверяет фундаментальное требование ТЗ:
        'Удаление хоста из очереди не означает удаление его из базы данных.
        Коллектор не должен уничтожать исходную информацию'.
        """
        self._create_test_trip()
        collector = CollectorEngine(trip_id=TEST_TRIP_ID, db_file=TEST_DB_PATH)
        selector = SelectorEngine(trip_id=TEST_TRIP_ID, db_file=TEST_DB_PATH)
        queue = QueueEngine(trip_id=TEST_TRIP_ID, db_file=TEST_DB_PATH)

        host_data = {
            "id": "h_preserve",
            "username": "preserve_user",
            "name": "Taro Tanaka",
            "age": 29,
            "is_verified": True,
            "about": "Ski coach in Nagano and Kyoto photography enthusiast."
        }
        card = collector.ingest_raw_host(host_data)
        self.assertIsNotNone(card)

        # 1. Отбор в Селекторе
        selector.decide("h_preserve", "like")
        # 2. Генерация черновика
        queue.generate_draft("h_preserve")
        # 3. Пользователь снимает хоста из очереди (skip)
        self.assertTrue(queue.skip("h_preserve"))

        # ПРОВЕРКА 1: Карточка в core_hosts существует и сохранила ВСЕ данные
        with get_connection(TEST_DB_PATH) as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT trip_id, username, state, is_verified, age, about 
                FROM core_hosts 
                WHERE trip_id = ? AND host_id = 'h_preserve';
            """, (TEST_TRIP_ID,))
            row = cur.fetchone()
            self.assertIsNotNone(row, "Хост был физически удален из core_hosts, что нарушает ТЗ!")
            self.assertEqual(row["trip_id"], TEST_TRIP_ID)
            self.assertEqual(row["state"], "skipped")
            self.assertEqual(row["age"], 29)
            self.assertTrue(bool(row["is_verified"]))

            # ПРОВЕРКА 2: Неизменяемый снимок в core_host_snapshots цел
            cur.execute("SELECT raw_data_json FROM core_host_snapshots WHERE trip_id = ? AND host_id = 'h_preserve';", (TEST_TRIP_ID,))
            snap = cur.fetchone()
            self.assertIsNotNone(snap, "Снимок хоста был уничтожен!")

            # ПРОВЕРКА 3: Аудит зафиксировал событие HOST_SKIPPED
            cur.execute("SELECT event FROM core_audit_logs WHERE trip_id = ? AND host_id = 'h_preserve' AND event = 'HOST_SKIPPED';", (TEST_TRIP_ID,))
            self.assertIsNotNone(cur.fetchone(), "Событие снятия из очереди не отражено в аудите!")

        # 4. Проверяем пакетную очистку очереди (clear_queue)
        collector.ingest_raw_host({"id": "h_batch1", "username": "b1", "name": "B1", "about": "about 1"})
        collector.ingest_raw_host({"id": "h_batch2", "username": "b2", "name": "B2", "about": "about 2"})
        selector.decide("h_batch1", "like")
        selector.decide("h_batch2", "like")

        skipped_count = queue.clear_queue()
        self.assertEqual(skipped_count, 2)

        # Проверяем, что оба хоста по-прежнему в базе со статусом 'skipped'
        with get_connection(TEST_DB_PATH) as conn:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM core_hosts WHERE trip_id = ? AND host_id IN ('h_batch1', 'h_batch2') AND state = 'skipped';", (TEST_TRIP_ID,))
            self.assertEqual(cur.fetchone()[0], 2)


if __name__ == "__main__":
    unittest.main()
