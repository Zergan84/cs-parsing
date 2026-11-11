"""
test_cleaner.py - Тесты модуля data_cleaner и API стандартизации таблицы хостов.
"""

import unittest
from test_isolation import activate_test_isolation

activate_test_isolation()

from data_cleaner import (
    sanitize_host_name,
    sanitize_host_clean_name,
    sanitize_host_location,
)
from database import db_session, init_db
from fastapi.testclient import TestClient
from app import app


class TestDataCleaner(unittest.TestCase):
    def setUp(self):
        init_db()
        with db_session() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT OR IGNORE INTO hosts (id, username, name, clean_name, profile_url, stay_request_url, location, first_scraped_at, updated_at)
                VALUES ('test_clean_user_99', 'test_clean_user_99', 'Clean User', 'Clean', 'http://localhost/u/99', '', 'Tokyo', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP);
                """
            )
            cur.execute(
                """
                INSERT OR IGNORE INTO trip_hosts (trip_id, host_id, city, arrival_date, departure_date, created_at)
                VALUES ('trip_cleaner_test', 'test_clean_user_99', 'Tokyo', '2026-11-18', '2026-11-20', CURRENT_TIMESTAMP);
                """
            )
    def test_name_cleaner(self):
        raw1 = "Ahmed H. 92 References 46 Friends Lives in Tokyo Kind, sincere..."
        self.assertEqual(sanitize_host_name(raw1), "Ahmed H.")
        self.assertEqual(sanitize_host_clean_name(raw1), "Ahmed")

        raw2 = "Rintaro H.55 References23 FriendsLives in HatsudaiHello there!"
        self.assertEqual(sanitize_host_name(raw2), "Rintaro H.")
        self.assertEqual(sanitize_host_clean_name(raw2), "Rintaro")

        raw3 = "Kenya I.\nSome bio line"
        self.assertEqual(sanitize_host_name(raw3), "Kenya I.")
        self.assertEqual(sanitize_host_clean_name(raw3), "Kenya")

        raw4 = "Dec Dec T."
        self.assertEqual(sanitize_host_name(raw4), "Dec Dec T.")
        self.assertEqual(sanitize_host_clean_name(raw4), "Dec Dec")

    def test_location_cleaner(self):
        self.assertEqual(sanitize_host_location("TokyoKind , sincere , honest guy"), "Tokyo")
        self.assertEqual(sanitize_host_location("HatsudaiHello there!"), "Hatsudai")
        self.assertEqual(sanitize_host_location("Lives in Kita-ku, Tokyo"), "Kita-ku, Tokyo")
        self.assertEqual(sanitize_host_location("TokyoCURRENT MISSION to see the world"), "Tokyo")
        self.assertEqual(sanitize_host_location({"city": "Tokyo"}), "Tokyo")
        self.assertEqual(sanitize_host_location(None), "Tokyo")

    def test_api_results_and_queue_add(self):
        client = TestClient(app)
        res = client.get("/api/results?curated=true")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIsInstance(data, list)
        if len(data) > 0:
            first = data[0]
            self.assertNotIn("References", first.get("name", ""))
            self.assertNotIn("CURRENT MISSION", first.get("location", ""))

        # Test queue add (в изолированной тестовой БД с явным trip_id)
        res_add = client.post(
            "/api/queue/add",
            json={"username": "test_clean_user_99", "trip_id": "trip_cleaner_test"},
        )
        self.assertEqual(res_add.status_code, 200)
        res_data = res_add.json()
        self.assertIn("status", res_data)


if __name__ == "__main__":
    unittest.main()
