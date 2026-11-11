import sys
import unittest
import json
from pathlib import Path

# Add project root to path
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from database import db_session
from hosting_guard import HostingAvailabilityGuard
from filter_hosts import HostFilter
from gender_classifier import classify_host
from scraper import CouchsurfingScraper

class TestAuditVerification(unittest.TestCase):
    def setUp(self):
        self.guard = HostingAvailabilityGuard()
        self.filter = HostFilter()
        self.test_username = "test_audit_host_9999"
        self.test_trip_id = "test_audit_trip_kyoto_2026"

    def tearDown(self):
        # Clean up test rows
        with db_session() as conn:
            conn.execute("DELETE FROM trip_hosts WHERE trip_id = ?", (self.test_trip_id,))
            conn.execute("DELETE FROM profile_analyses WHERE host_id = ?", (self.test_username,))
            conn.execute("DELETE FROM hosts WHERE id = ? OR username = ?", (self.test_username, self.test_username))

    def test_1_db_atomic_save_and_deduplication(self):
        scraper = CouchsurfingScraper(city="Kyoto", trip_id=self.test_trip_id)
        
        test_host = {
            "username": self.test_username,
            "name": "Audit Test Host",
            "gender": "male",
            "host_type": "solo",
            "can_host_overnight": True,
            "hosting_verdict_reason": "Good host",
            "hosting_confidence": 0.95,
            "sleeping_arrangement": "Private Room",
            "house_rules": "No smoking",
            "secret_code_detected": True,
            "secret_code_hint": "secret123",
            "conversation_hooks": ["Tea", "Zen"],
            "compatibility_points": [{"title": "Coffee", "category": "Food"}],
            "conversation_strategy": "Ask about Kyoto gardens",
            "compatibility_score": 95.5,
            "location": "Kyoto, Japan",
            "about_me": "Hello from Kyoto, I like tea.",
            "references_count": 10,
            "response_rate_pct": 100.0,
        }

        # 1. Первое сохранение хоста с явным compatibility_score
        scraper._upsert_hosts_to_db([test_host])

        # Проверяем, что всё записалось в БД
        with db_session() as conn:
            h = conn.execute("SELECT * FROM hosts WHERE id = ?", (self.test_username,)).fetchone()
            self.assertIsNotNone(h, "Host must exist in hosts table")
            self.assertEqual(h["username"], self.test_username)

            pa = conn.execute("SELECT * FROM profile_analyses WHERE host_id = ?", (self.test_username,)).fetchone()
            self.assertIsNotNone(pa, "Profile analysis must exist in profile_analyses table")
            self.assertEqual(pa["estimated_gender"], "Male")
            self.assertEqual(pa["compatibility_score"], 95.5)
            self.assertEqual(pa["sleeping_arrangement"], "Private Room")

            th = conn.execute("SELECT * FROM trip_hosts WHERE trip_id = ? AND host_id = ?", (self.test_trip_id, self.test_username)).fetchone()
            self.assertIsNotNone(th, "Trip host link must exist in trip_hosts")
            self.assertEqual(th["city"], "Kyoto")

            count_before = conn.execute("SELECT count(*) FROM hosts WHERE username = ?", (self.test_username,)).fetchone()[0]
            self.assertEqual(count_before, 1)

        # 2. Повторное сохранение того же хоста (проверка дедупликации)
        test_host["name"] = "Audit Test Host Updated"
        scraper._upsert_hosts_to_db([test_host])

        with db_session() as conn:
            count_after = conn.execute("SELECT count(*) FROM hosts WHERE username = ?", (self.test_username,)).fetchone()[0]
            self.assertEqual(count_after, 1, "Deduplication failed: duplicates created!")
            updated_h = conn.execute("SELECT name FROM hosts WHERE id = ?", (self.test_username,)).fetchone()
            self.assertEqual(updated_h["name"], "Audit Test Host Updated", "Update failed")

        # 3. Сохранение хоста без compatibility_score (должен записаться NULL, а не фиктивные 90.0)
        host_no_score_uname = "test_audit_host_no_score"
        try:
            scraper._upsert_hosts_to_db([{
                "username": host_no_score_uname,
                "name": "Host Without Score",
                "about_me": "Bio without score",
            }])
            with db_session() as conn:
                pa_null = conn.execute("SELECT compatibility_score FROM profile_analyses WHERE host_id = ?", (host_no_score_uname,)).fetchone()
                self.assertIsNotNone(pa_null)
                self.assertIsNone(pa_null["compatibility_score"], "Compatibility score must be NULL when not calculated, not fake 90.0")
        finally:
            with db_session() as conn:
                conn.execute("DELETE FROM profile_analyses WHERE host_id = ?", (host_no_score_uname,))
                conn.execute("DELETE FROM hosts WHERE id = ?", (host_no_score_uname,))

    def test_2_diet_and_lifestyle_not_rejected(self):
        diet_samples = [
            "I am currently on a diet and eating healthy food.",
            "Special diet? No, eat everything.",
            "CURRENT MISSION dieting!! Trying to get fit.",
            "I teach cosmetics and dietology in Kyoto.",
            "I am currently on a gluten-free lifestyle.",
            "Currently in Kyoto hosting travelers from around the world."
        ]
        for text in diet_samples:
            res = self.guard.check_regex_blacklist(text)
            self.assertIsNone(res, f"Diet phrase '{text}' was incorrectly matched by HostingGuard: {res}")

            filter_triggers = self.filter.check_stop_triggers(text)
            self.assertEqual(len(filter_triggers), 0, f"Diet phrase '{text}' was incorrectly matched by HostFilter: {filter_triggers}")

    def test_3_hosting_guard_rejections_and_travel_allowance(self):
        # 1. Фразы о путешествиях (traveling around, traveling abroad) НЕ должны автоматически отсекаться
        travel_samples = [
            "Currently traveling around Europe for the next two weeks.",
            "I love traveling around Japan.",
            "Traveling abroad has always been my dream."
        ]
        for text in travel_samples:
            res = self.guard.check_regex_blacklist(text)
            self.assertIsNone(res, f"Travel phrase '{text}' should NOT be matched by HostingGuard")

        # 2. Однозначные отказы и строгие ограничения ДОЛЖНЫ отсекаться
        rejection_samples = [
            "Currently not in Japan, moved away.",
            "I cannot host anyone due to moving.",
            "Female guests only please.",
            "I can only meet for coffee."
        ]
        for text in rejection_samples:
            res = self.guard.check_regex_blacklist(text)
            self.assertIsNotNone(res, f"Rejection phrase '{text}' MUST be matched by HostingGuard")

    def test_4_gender_classification_international_names(self):
        # 1. Без контекста любые имена (включая международные и японские) должны быть Unknown
        self.assertEqual(classify_host("Luka", "Software engineer", "luka_eu")[0], "Unknown")
        self.assertEqual(classify_host("Slava", "Backpacker from Prague", "slava_cz")[0], "Unknown")
        self.assertEqual(classify_host("Krishna", "Solo engineer from India", "krishna_in")[0], "Unknown")
        self.assertEqual(classify_host("Sami", "Exploring the world", "sami_fi")[0], "Unknown")
        self.assertEqual(classify_host("Mika", "Just traveling", "mika_travel")[0], "Unknown")
        self.assertEqual(classify_host("Haruka", "Welcome to Kyoto", "haruka_jp")[0], "Unknown")
        self.assertEqual(classify_host("Ayami", "Living in Tokyo", "ayami_jp")[0], "Unknown")
        self.assertEqual(classify_host("Rina", "Hello friends", "rina_jp")[0], "Unknown")

        # 2. С явными контекстными подтверждениями (Bio или References)
        self.assertEqual(classify_host("Slava", "Tech guy from Prague (he/him)", "slava_cz")[0], "Male")
        self.assertEqual(classify_host("Luka", "Solo traveler", "luka_eu", references=["He was a great host, very kind."])[0], "Male")
        self.assertEqual(classify_host("Krishna", "Engineer", "krishna_in", references=["He helped us with bags."])[0], "Male")
        self.assertEqual(classify_host("Sami", "Coffee lover", "sami_fi", references=["He is a polite gentleman."])[0], "Male")
        self.assertEqual(classify_host("Mika", "Artist (she/her)", "mika_travel")[0], "Female")
        self.assertEqual(classify_host("Haruka", "Japanese woman living in Kyoto (she/her)", "haruka_jp")[0], "Female")

if __name__ == "__main__":
    unittest.main()
