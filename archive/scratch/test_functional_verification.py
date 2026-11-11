"""
test_functional_verification.py - Автоматизированная практическая верификация сценариев устойчивости.
"""

import asyncio
import json
import os
import sys
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from test_isolation import activate_test_isolation, get_output_dir, reset_isolated_test_env

activate_test_isolation()

from database import db_session, init_db, reset_stuck_sending_statuses, reset_stuck_background_jobs
from process_manager import get_process_manager
from app import (
    app_startup_cleanup,
    get_results,
    delete_selected_hosts,
    api_search_erase_all,
    get_status,
    DeleteHostsPayload,
    EraseSearchPayload,
)


class FunctionalVerificationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        reset_isolated_test_env()
        init_db()

    async def test_01_background_process_decoupling_and_live_sqlite_saving(self):
        """
        Тест 1: Процесс поиска работает независимо от клиента и сохраняет хостов в SQLite по ходу работы.
        """
        pm = get_process_manager()
        worker_code = """
import time, sys
from database import db_session

trip_id = 'test_trip_kyoto_live'
for i in range(1, 4):
    username = f'live_kyoto_host_{i}'
    with db_session() as conn:
        cur = conn.cursor()
        cur.execute('''
            INSERT INTO hosts (id, username, name, clean_name, profile_url, stay_request_url, location, first_scraped_at, updated_at)
            VALUES (?, ?, ?, ?, 'http://test', '', 'Kyoto', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ON CONFLICT(id) DO UPDATE SET updated_at=CURRENT_TIMESTAMP;
        ''', (username, username, f'Name {i}', f'CleanName {i}'))
        cur.execute('''
            INSERT OR IGNORE INTO trip_hosts (trip_id, host_id, city, arrival_date, departure_date, created_at)
            VALUES (?, ?, 'Kyoto', '2026-11-19', '2026-11-21', CURRENT_TIMESTAMP);
        ''', (trip_id, username))
    print(f'DISCOVERED_HOST: {username}', flush=True)
    time.sleep(0.4)
"""
        cmd = [sys.executable, "-c", worker_code]
        res = await pm.start_job("Тестовый поиск хостов", cmd, {"trip_id": "test_trip_kyoto_live"})
        self.assertTrue(res.get("success"), f"Не удалось запустить задачу: {res}")
        self.assertTrue(pm.is_running, "Менеджер процессов должен быть в статусе running")

        # Клиент "закрыт" (никаких HTTP запросов к серверу не идет). Ждем завершения подпроцесса.
        max_wait = 10.0
        start_t = asyncio.get_event_loop().time()
        while pm.is_running and (asyncio.get_event_loop().time() - start_t) < max_wait:
            await asyncio.sleep(0.2)

        self.assertFalse(pm.is_running, "Подпроцесс должен успешно завершиться")

        # Проверяем, что хосты сохранились в SQLite по мере обнаружения
        with db_session() as conn:
            cur = conn.cursor()
            cur.execute("SELECT host_id FROM trip_hosts WHERE trip_id = 'test_trip_kyoto_live' ORDER BY host_id;")
            saved_hosts = [r[0] for r in cur.fetchall()]

        self.assertEqual(saved_hosts, ['live_kyoto_host_1', 'live_kyoto_host_2', 'live_kyoto_host_3'])
        print("\n[OK] Тест 1: Фоновый процесс выполнился автономно и сохранил хостов в SQLite.")

    async def test_02_client_reconnection_and_state_restoration(self):
        """
        Тест 2: При подключении клиента восстанавливаются результаты и статус.
        """
        # Сначала убедимся, что данные теста 1 в базе
        with db_session() as conn:
            cur = conn.cursor()
            for i in range(1, 4):
                username = f'live_kyoto_host_{i}'
                cur.execute("""
                    INSERT OR REPLACE INTO hosts (id, username, name, clean_name, profile_url, stay_request_url, location, first_scraped_at, updated_at)
                    VALUES (?, ?, ?, ?, 'http://test', '', 'Kyoto', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP);
                """, (username, username, f'Name {i}', f'CleanName {i}'))
                cur.execute("""
                    INSERT OR IGNORE INTO trip_hosts (trip_id, host_id, city, arrival_date, departure_date, created_at)
                    VALUES ('test_trip_kyoto_live', ?, 'Kyoto', '2026-11-19', '2026-11-21', CURRENT_TIMESTAMP);
                """, (username,))

        # Запрашиваем хостов по trip_id через API
        hosts = await get_results(trip_id='test_trip_kyoto_live')
        self.assertEqual(len(hosts), 3, "API должно вернуть 3 сохраненных хоста для поездки")
        self.assertTrue(all(h['location'] == 'Kyoto' for h in hosts))

        # Запрашиваем телеметрию
        status = await get_status(city='Kyoto')
        self.assertFalse(status['is_running'], "Процесс завершен, is_running должно быть False")
        print("[OK] Тест 2: Клиент успешно получает сохраненные результаты и актуальный статус.")

    async def test_03_server_restart_during_search_and_interruption_detection(self):
        """
        Тест 3: Прерывание сервера во время поиска: статус переходит в interrupted, данные не теряются.
        """
        job_id = "test_search_job_crash_123"
        trip_id = "test_trip_interrupted"

        # 1. Записываем задачу со статусом 'running' в background_jobs и сохраняем хоста
        with db_session() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO background_jobs (job_id, job_type, status, parameters_json, started_at)
                VALUES (?, 'Сбор анкет (трип)', 'running', ?, CURRENT_TIMESTAMP);
            """, (job_id, json.dumps({"trip_id": trip_id})))

            cur.execute("""
                INSERT INTO hosts (id, username, name, clean_name, profile_url, stay_request_url, location, first_scraped_at, updated_at)
                VALUES ('host_before_crash', 'host_before_crash', 'Before Crash', 'Before Crash', 'http://test', '', 'Kyoto', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(id) DO UPDATE SET updated_at=CURRENT_TIMESTAMP;
            """)
            cur.execute("""
                INSERT OR IGNORE INTO trip_hosts (trip_id, host_id, city, arrival_date, departure_date, created_at)
                VALUES (?, 'host_before_crash', 'Kyoto', '2026-11-19', '2026-11-21', CURRENT_TIMESTAMP);
            """, (trip_id,))

        # 2. Эмулируем рестарт сервера (вызов app_startup_cleanup)
        await app_startup_cleanup()

        # 3. Проверяем статус задачи в SQLite
        with db_session() as conn:
            cur = conn.cursor()
            cur.execute("SELECT status, message FROM background_jobs WHERE job_id = ?;", (job_id,))
            row = cur.fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], 'interrupted', f"Статус должен быть 'interrupted', получено: {row[0]}")
            self.assertIn("restarted", row[1])

            # 4. Проверяем, что хост, сохраненный до рестарта, на месте
            cur.execute("SELECT host_id FROM trip_hosts WHERE trip_id = ?;", (trip_id,))
            hosts = [r[0] for r in cur.fetchall()]
            self.assertIn('host_before_crash', hosts, "Хосты, найденные до рестарта, должны сохраниться")

        print("[OK] Тест 3: Рестарт сервера корректно переводит задачу в interrupted и сохраняет данные.")

    async def test_04_sending_queue_crash_recovery_prevents_duplicate_sends(self):
        """
        Тест 4: Восстановление очереди отправки: статус sending переходит в Verification_Required,
        предотвращая автоматическую повторную отправку.
        """
        test_host = "test_queue_host_crash"

        with db_session() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO hosts (id, username, name, clean_name, profile_url, stay_request_url, location, first_scraped_at, updated_at)
                VALUES (?, ?, 'Queue Host', 'Queue Host', 'http://test', '', 'Kyoto', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(id) DO NOTHING;
            """, (test_host, test_host))
            cur.execute("""
                INSERT INTO sending_queue (id, host_id, status, created_at, updated_at)
                VALUES ('test_sq_1', ?, 'sending', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP);
            """, (test_host,))

        # Также пишем во временный queue_file для проверки синхронизации
        out_dir = get_output_dir()
        out_dir.mkdir(parents=True, exist_ok=True)
        q_file = out_dir / "requests_queue.json"
        q_data = [{"username": test_host, "status": "sending"}]
        with open(q_file, "w", encoding="utf-8") as f:
            json.dump(q_data, f, indent=2)

        # Рестарт сервера
        await app_startup_cleanup()

        # Проверка SQLite
        with db_session() as conn:
            cur = conn.cursor()
            cur.execute("SELECT status, notes FROM sending_queue WHERE host_id = ?;", (test_host,))
            row = cur.fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], "Verification_Required", f"Статус в SQLite должен быть Verification_Required, а не {row[0]}")

        # Проверка JSON файла
        with open(q_file, "r", encoding="utf-8") as f:
            q_file_data = json.load(f)
        status_in_file = q_file_data[0]["status"]
        self.assertEqual(status_in_file, "Verification_Required", f"Статус в файле должен быть Verification_Required, а не {status_in_file}")
        print("[OK] Тест 4: Зависшие отправки безопасно переведены в Verification_Required без авто-повтора.")

    async def test_05_strict_trip_id_isolation_same_city_different_dates(self):
        """
        Тест 5: Изоляция двух Trip Cards одного города (Kyoto) с разными датами.
        Trip A: 19–21 Nov (trip_kyoto_nov_19_21)
        Trip B: 25–28 Nov (trip_kyoto_nov_25_28)
        """
        trip_a = "trip_kyoto_nov_19_21"
        trip_b = "trip_kyoto_nov_25_28"

        with db_session() as conn:
            cur = conn.cursor()
            # Добавляем хостов Trip A
            for i in [1, 2]:
                h = f"kyoto_host_A{i}"
                cur.execute("INSERT OR REPLACE INTO hosts (id, username, name, clean_name, profile_url, stay_request_url, location, first_scraped_at, updated_at) VALUES (?, ?, ?, ?, 'http://test', '', 'Kyoto', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP);", (h, h, h, h))
                cur.execute("INSERT OR REPLACE INTO trip_hosts (trip_id, host_id, city, arrival_date, departure_date, created_at) VALUES (?, ?, 'Kyoto', '2026-11-19', '2026-11-21', CURRENT_TIMESTAMP);", (trip_a, h))

            # Добавляем хостов Trip B
            for i in [1, 2]:
                h = f"kyoto_host_B{i}"
                cur.execute("INSERT OR REPLACE INTO hosts (id, username, name, clean_name, profile_url, stay_request_url, location, first_scraped_at, updated_at) VALUES (?, ?, ?, ?, 'http://test', '', 'Kyoto', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP);", (h, h, h, h))
                cur.execute("INSERT OR REPLACE INTO trip_hosts (trip_id, host_id, city, arrival_date, departure_date, created_at) VALUES (?, ?, 'Kyoto', '2026-11-25', '2026-11-28', CURRENT_TIMESTAMP);", (trip_b, h))

        # 1. Запрос по trip_a должен вернуть ТОЛЬКО хостов Trip A
        res_a = await get_results(trip_id=trip_a)
        usernames_a = {h["username"] for h in res_a}
        self.assertEqual(usernames_a, {"kyoto_host_A1", "kyoto_host_A2"}, f"Результаты Trip A не изолированы: {usernames_a}")

        # 2. Запрос по trip_b должен вернуть ТОЛЬКО хостов Trip B
        res_b = await get_results(trip_id=trip_b)
        usernames_b = {h["username"] for h in res_b}
        self.assertEqual(usernames_b, {"kyoto_host_B1", "kyoto_host_B2"}, f"Результаты Trip B не изолированы: {usernames_b}")

        # 3. Удаление одного хоста из Trip A не должно затрагивать Trip B
        del_res = await delete_selected_hosts(DeleteHostsPayload(usernames=["kyoto_host_A1"], trip_id=trip_a, city="Kyoto"))
        self.assertEqual(del_res["status"], "ok")

        res_a_after = await get_results(trip_id=trip_a)
        self.assertEqual({h["username"] for h in res_a_after}, {"kyoto_host_A2"})

        res_b_after = await get_results(trip_id=trip_b)
        self.assertEqual({h["username"] for h in res_b_after}, {"kyoto_host_B1", "kyoto_host_B2"})

        # 4. Очистка Trip A (Erase All) не должна затронуть Trip B
        erase_res = await api_search_erase_all(EraseSearchPayload(trip_id=trip_a, city="Kyoto"))
        self.assertEqual(erase_res["status"], "ok")

        res_a_final = await get_results(trip_id=trip_a)
        self.assertEqual(len(res_a_final), 0, "Trip A должен быть полностью очищен")

        res_b_final = await get_results(trip_id=trip_b)
        self.assertEqual({h["username"] for h in res_b_final}, {"kyoto_host_B1", "kyoto_host_B2"}, "Trip B должен остаться нетронутым!")

        print("[OK] Тест 5: Полная изоляция результатов, удаления и очистки по trip_id подтверждена.")

    async def test_06_dead_process_cannot_show_running_or_completed(self):
        """
        Тест 6: Незавершенная задача не отображается как running или completed при фактически мертвом процессе.
        """
        pm = get_process_manager()
        self.assertFalse(pm.is_running, "При отсутствии запущенного подпроцесса is_running обязан быть False")

        # Проверяем, что в SQLite нет задач со статусом running без живого процесса
        reset_count = reset_stuck_background_jobs()
        with db_session() as conn:
            cur = conn.cursor()
            cur.execute("SELECT count(*) FROM background_jobs WHERE status = 'running';")
            running_count = cur.fetchone()[0]
        self.assertEqual(running_count, 0, "В базе данных не может оставаться задач со статусом 'running'")
        print("[OK] Тест 6: Мертвые процессы не могут отображаться как running или completed.")

    async def asyncTearDown(self):
        try:
            with db_session() as conn:
                cur = conn.cursor()
                cur.execute("DELETE FROM trip_hosts WHERE trip_id LIKE 'test_%';")
                cur.execute("DELETE FROM hosts WHERE id LIKE 'test_%' OR id LIKE 'kyoto_host_%' OR id LIKE 'live_kyoto_host_%';")
                cur.execute("DELETE FROM background_jobs WHERE job_id LIKE 'test_%';")
        except Exception:
            pass


if __name__ == "__main__":
    unittest.main()
