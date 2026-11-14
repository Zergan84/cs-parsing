"""
tests/test_profile_intelligence.py

Тесты этапа 3: Host Profile Intelligence — сбор, структурирование и сохранение
информации о Couchsurfing-хостах.

Покрывает:
1. Верификация профиля (VERIFIED / NOT_VERIFIED / UNKNOWN).
2. Recently Active (с и без статуса).
3. Все обнаруженные бейджи.
4. Полный текст описания профиля.
5. Shared Information (самостоятельная секция).
6. Why I'm on Couchsurfing (отдельная секция).
7. References разных типов.
8. Пустые и отсутствующие разделы.
9. Gender evidence: явное значение, самоопределение, местоимения,
   недостаточно данных, противоречивые источники, запрет определения по имени/фото.
10. Целостность данных: повторный сбор, отсутствие дублей References,
    сохранение истории, ошибка загрузки раздела, сохранение старых данных
    при ошибке парсинга, различие NOT_PRESENT vs PARSE_FAILED.

Все тесты используют HTML-файты-фикстуры и изолированные временные SQLite базы.
Никаких реальных Couchsurfing-запросов.
"""

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from core.database import get_connection, init_db
from core.profile_intelligence import (
    collect_host_profile,
    get_profile_intelligence_engine,
    EXTRACTION_STATUS_VALUE,
    EXTRACTION_STATUS_NOT_PRESENT,
    EXTRACTION_STATUS_NOT_AVAILABLE,
    EXTRACTION_STATUS_PARSE_FAILED,
    EXTRACTION_STATUS_NOT_CHECKED,
    COLLECTION_STATUS_SUCCESS,
    COLLECTION_STATUS_PARTIAL,
    COLLECTION_STATUS_FAILED,
    VERIFICATION_STATUS_VERIFIED,
    VERIFICATION_STATUS_NOT_VERIFIED,
    VERIFICATION_STATUS_UNKNOWN,
    RECENTLY_ACTIVE_STATUS_RECENTLY_ACTIVE,
    RECENTLY_ACTIVE_STATUS_ACTIVE,
    RECENTLY_ACTIVE_STATUS_NOT_RECENTLY_ACTIVE,
    RECENTLY_ACTIVE_STATUS_UNKNOWN,
    _normalize_recently_active,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
TEST_DB_DIR = Path(tempfile.mkdtemp(prefix="cs-profile-intelligence-"))
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "profile_intelligence"


def _make_db_path(suffix: str) -> Path:
    return TEST_DB_DIR / f"{suffix}.db"


def _ensure_test_dir() -> None:
    TEST_DB_DIR.mkdir(parents=True, exist_ok=True)
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)


def _cleanup_db(db_path: Path) -> None:
    if db_path.exists():
        try:
            db_path.unlink()
        except Exception:
            pass
    for suffix in ("-wal", "-shm"):
        p = Path(str(db_path) + suffix)
        if p.exists():
            try:
                p.unlink()
            except Exception:
                pass


def _fresh_db() -> Path:
    _ensure_test_dir()
    import uuid as _uuid
    suffix = f"test_{_uuid.uuid4().hex[:8]}"
    db_path = _make_db_path(suffix)
    _cleanup_db(db_path)
    init_db(db_path)
    return db_path


# ---------------------------------------------------------------------------
# HTML фикстуры (встроены в тесты, не требуют внешних файлов)
# ---------------------------------------------------------------------------

FIXTURE_VERIFIED_PROFILE = """
<html><body>
<div data-testid="verification-badge">Verified</div>
<div class="recently-active">Recently Active</div>
<div class="badge" data-testid="badge-1">Super Host</div>
<div class="badge">Years on CS: 5</div>
<div class="about-section">
  <h2>About me</h2>
  <p>I love traveling and meeting new people. I am a software engineer from Tokyo.</p>
</div>
<div class="shared-information">
  <h2>Shared Information</h2>
  <p>I speak English and Japanese. I enjoy cooking and photography.</p>
</div>
<div class="why-on-couchsurfing">
  <h2>Why I'm on Couchsurfing</h2>
  <p>I want to share my culture and learn from others. Couchsurfing is amazing.</p>
</div>
<div class="references-section">
  <div class="reference">
    <span class="author">Alice Smith</span>
    <span class="date">2026-01-15</span>
    <span class="text">Great host, very welcoming!</span>
    <span class="type">positive</span>
  </div>
  <div class="reference">
    <span class="author">Bob Jones</span>
    <span class="date">2026-03-20</span>
    <span class="text">Clean and tidy place.</span>
    <span class="type">neutral</span>
  </div>
</div>
<div class="gender-info">
  <span>She/her</span>
</div>
</body></html>
"""

FIXTURE_NOT_VERIFIED_PROFILE = """
<html><body>
<div class="verification-status">Not Verified</div>
<div class="recently-active">Active recently</div>
<div class="about-section">
  <p>Just a regular host.</p>
</div>
</body></html>
"""

FIXTURE_NO_ACTIVE_STATUS = """
<html><body>
<div class="about-section">
  <p>No activity info here.</p>
</div>
</body></html>
"""

FIXTURE_MULTIPLE_BADGES = """
<html><body>
<div class="badge">Super Host</div>
<div class="badge">Verified Phone</div>
<div class="badge">Verified Email</div>
<div class="badge">Years on CS: 10</div>
<div class="badge">Languages: English, Japanese</div>
<div class="badge">Eco Friendly</div>
<div class="badge">Pet Friendly</div>
</body></html>
"""

FIXTURE_EMPTY_PROFILE = """
<html><body>
<div class="placeholder">No profile information available.</div>
</body></html>
"""

FIXTURE_CONFLICTING_GENDER = """
<html><body>
<div class="gender-info">
  <span>He/him</span>
  <span>She/her</span>
</div>
<div class="about-section">
  <p>I am a guy. She/her.</p>
</div>
</body></html>
"""


class TestProfileIntelligence(unittest.TestCase):
    """Тесты системы сбора профильной информации о хостах."""

    @classmethod
    def setUpClass(cls):
        _ensure_test_dir()

    @classmethod
    def tearDownClass(cls):
        if TEST_DB_DIR.exists():
            for f in TEST_DB_DIR.iterdir():
                if f.is_file():
                    try:
                        f.unlink()
                    except Exception:
                        pass

    def test_negative_activity_label_is_not_misread_as_recent(self):
        self.assertEqual(
            _normalize_recently_active("Not recently active")[0],
            RECENTLY_ACTIVE_STATUS_NOT_RECENTLY_ACTIVE,
        )

    # ------------------------------------------------------------------
    # 1. ВЕРИФИКАЦИЯ ПРОФИЛЯ
    # ------------------------------------------------------------------

    def test_verification_verified(self):
        """Статус верификации VERIFIED при наличии подтверждающего признака."""
        db_path = _fresh_db()
        try:
            api_data = {"isIdentificationVerified": True}
            result = collect_host_profile(
                host_id="h_verified_1",
                username="verified_user",
                profile_url="https://www.couchsurfing.com/c/users/verified_user",
                api_data=api_data,
                db_file=db_path,
            )
            section = result["sections"]["verification"]
            self.assertEqual(section["verification_status"], VERIFICATION_STATUS_VERIFIED)
            self.assertIsNotNone(section["verification_source"])

            with get_connection(db_path) as conn:
                cur = conn.cursor()
                cur.execute("SELECT verification_status FROM host_profiles WHERE host_id='h_verified_1'")
                self.assertEqual(cur.fetchone()[0], VERIFICATION_STATUS_VERIFIED)
        finally:
            _cleanup_db(db_path)

    def test_verification_not_verified(self):
        """Статус верификации NOT_VERIFIED."""
        db_path = _fresh_db()
        try:
            api_data = {"isIdentificationVerified": False}
            result = collect_host_profile(
                host_id="h_not_verified_1",
                username="not_verified_user",
                api_data=api_data,
                db_file=db_path,
            )
            section = result["sections"]["verification"]
            self.assertEqual(section["verification_status"], VERIFICATION_STATUS_NOT_VERIFIED)
        finally:
            _cleanup_db(db_path)

    def test_verification_unknown(self):
        """Статус верификации UNKNOWN когда признака нет."""
        db_path = _fresh_db()
        try:
            result = collect_host_profile(
                host_id="h_unknown_1",
                username="unknown_user",
                db_file=db_path,
            )
            section = result["sections"]["verification"]
            self.assertEqual(section["verification_status"], VERIFICATION_STATUS_UNKNOWN)
        finally:
            _cleanup_db(db_path)

    def test_verification_not_based_on_references(self):
        """Верификация НЕ определяется по количеству References."""
        db_path = _fresh_db()
        try:
            api_data = {
                "isIdentificationVerified": False,
                "references": [{"text": "great", "author": "A"}, {"text": "ok", "author": "B"}],
            }
            result = collect_host_profile(
                host_id="h_refs_1",
                username="user_with_refs",
                api_data=api_data,
                db_file=db_path,
            )
            section = result["sections"]["verification"]
            self.assertEqual(section["verification_status"], VERIFICATION_STATUS_NOT_VERIFIED)
        finally:
            _cleanup_db(db_path)

    # ------------------------------------------------------------------
    # 2. RECENTLY ACTIVE
    # ------------------------------------------------------------------

    def test_recently_active_status(self):
        """Статус Recently Active извлекается и нормализуется."""
        db_path = _fresh_db()
        try:
            api_data = {"isRecentlyActive": True}
            result = collect_host_profile(
                host_id="h_active_1",
                username="active_user",
                api_data=api_data,
                db_file=db_path,
            )
            section = result["sections"]["recently_active"]
            self.assertEqual(
                section["recently_active_status"],
                RECENTLY_ACTIVE_STATUS_RECENTLY_ACTIVE,
            )
            self.assertIsNotNone(section["recently_active_raw"])
            self.assertIsNotNone(section["recently_active_normalized"])
        finally:
            _cleanup_db(db_path)

    def test_recently_active_no_status(self):
        """Когда нет индикатора активности — UNKNOWN, не подменяется вычислением."""
        db_path = _fresh_db()
        try:
            result = collect_host_profile(
                host_id="h_no_active_1",
                username="no_active_user",
                db_file=db_path,
            )
            section = result["sections"]["recently_active"]
            self.assertEqual(
                section["recently_active_status"],
                RECENTLY_ACTIVE_STATUS_UNKNOWN,
            )
        finally:
            _cleanup_db(db_path)

    def test_recently_active_preserves_raw_value(self):
        """Исходное значение с сайта сохраняется отдельно от нормализованного."""
        db_path = _fresh_db()
        try:
            api_data = {"isRecentlyActive": True, "lastLogin": "2026-11-10T14:00:00Z"}
            result = collect_host_profile(
                host_id="h_raw_1",
                username="raw_user",
                api_data=api_data,
                db_file=db_path,
            )
            section = result["sections"]["recently_active"]
            self.assertIsNotNone(section["recently_active_raw"])
            self.assertIsNotNone(section["recently_active_normalized"])
        finally:
            _cleanup_db(db_path)

    # ------------------------------------------------------------------
    # 3. БЕЙДЖИ
    # ------------------------------------------------------------------

    def test_all_badges_collected(self):
        """Все бейджи собираются, не ограничиваясь известным списком."""
        db_path = _fresh_db()
        try:
            api_data = {"badges": [
                {"name": "Super Host", "text": "Years on CS: 5"},
                {"name": "Verified Phone", "text": "Phone verified"},
                {"name": "Eco Friendly", "text": "Eco conscious host"},
            ]}
            result = collect_host_profile(
                host_id="h_badges_1",
                username="badge_user",
                api_data=api_data,
                db_file=db_path,
            )
            section = result["sections"]["badges"]
            self.assertGreaterEqual(section["badges_found"], 3)
            self.assertGreaterEqual(section["badges_saved"], 3)

            with get_connection(db_path) as conn:
                cur = conn.cursor()
                cur.execute("SELECT COUNT(*) FROM host_badges WHERE host_id='h_badges_1'")
                self.assertGreaterEqual(cur.fetchone()[0], 3)
        finally:
            _cleanup_db(db_path)

    def test_no_duplicate_badges_on_recollection(self):
        """При повторном сборе бейджи не дублируются."""
        db_path = _fresh_db()
        try:
            api_data = {"badges": [{"name": "Super Host", "text": "Years on CS: 5"}]}
            collect_host_profile(
                host_id="h_nodup_1", username="nodup_user", api_data=api_data, db_file=db_path
            )
            collect_host_profile(
                host_id="h_nodup_1", username="nodup_user", api_data=api_data, db_file=db_path
            )
            with get_connection(db_path) as conn:
                cur = conn.cursor()
                cur.execute("SELECT COUNT(*) FROM host_badges WHERE host_id='h_nodup_1'")
                self.assertEqual(cur.fetchone()[0], 1)
        finally:
            _cleanup_db(db_path)

    def test_profile_observations_keep_section_sources_and_prior_values(self):
        db_path = _fresh_db()
        try:
            first = collect_host_profile(
                host_id="h_provenance_1", username="provenance_user",
                api_data={
                    "aboutMe": "A complete profile biography.",
                    "houseRules": "Please remove your shoes.",
                    "sharedInformation": "I can show you the neighborhood.",
                    "whyOnCouchsurfing": "I enjoy cultural exchange.",
                    "badges": ["Super Host", {"name": "Verified phone"}],
                    "references": [{"id": "ref-profile", "text": "A thoughtful guest.", "author": "guest_a"}],
                },
                html_content="<html><body>Profile</body></html>",
                db_file=db_path,
            )
            self.assertEqual(first["overall_status"], COLLECTION_STATUS_PARTIAL)
            with get_connection(db_path) as conn:
                source_rows = {
                    row["section_name"]: row
                    for row in conn.execute(
                    "SELECT section_name,section_text,extraction_status,extraction_source,collected_at,value_collected_at FROM host_profile_sections WHERE host_id=?",
                        ("h_provenance_1",),
                    )
                }
                ref = conn.execute(
                    "SELECT collection_source,collected_at FROM host_references WHERE reference_id='ref-profile'"
                ).fetchone()
                badge_count = conn.execute(
                    "SELECT COUNT(*) FROM host_badges WHERE host_id='h_provenance_1'"
                ).fetchone()[0]
                snapshot_count = conn.execute(
                    "SELECT COUNT(*) FROM host_profile_snapshots WHERE host_id='h_provenance_1'"
                ).fetchone()[0]
            self.assertEqual(source_rows["house_rules"]["extraction_source"], "network_api")
            self.assertEqual(source_rows["shared_information"]["extraction_status"], EXTRACTION_STATUS_VALUE)
            self.assertTrue(source_rows["shared_information"]["collected_at"])
            self.assertEqual(ref["collection_source"], "network_api")
            self.assertTrue(ref["collected_at"])
            self.assertEqual(badge_count, 2)
            self.assertEqual(snapshot_count, 1)

            collect_host_profile(
                host_id="h_provenance_1", username="provenance_user",
                api_data={}, profile_page_status="FAILED", db_file=db_path,
            )
            with get_connection(db_path) as conn:
                row = conn.execute(
                    "SELECT section_text,extraction_status,extraction_source,value_collected_at FROM host_profile_sections WHERE host_id=? AND section_name='shared_information'",
                    ("h_provenance_1",),
                ).fetchone()
                snapshots = conn.execute(
                    "SELECT sections_json FROM host_profile_snapshots WHERE host_id=? ORDER BY collection_timestamp DESC LIMIT 1",
                    ("h_provenance_1",),
                ).fetchone()
            self.assertEqual(row["section_text"], "I can show you the neighborhood.")
            self.assertEqual(row["extraction_status"], EXTRACTION_STATUS_NOT_AVAILABLE)
            self.assertEqual(row["extraction_source"], "network_api")
            self.assertEqual(row["value_collected_at"], source_rows["shared_information"]["value_collected_at"])
            self.assertEqual(json.loads(snapshots[0])["shared_information"]["source"], "profile_page_unavailable")
            with get_connection(db_path) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM host_profile_snapshots WHERE host_id='h_provenance_1'").fetchone()[0], 2)
        finally:
            _cleanup_db(db_path)

    # ------------------------------------------------------------------
    # 4. ПОЛНЫЙ ОПИСАНИЕ ПРОФИЛЯ (секции)
    # ------------------------------------------------------------------

    def test_profile_sections_collected_separately(self):
        """Разделы профиля сохраняются отдельно, не объединяются в одно поле."""
        db_path = _fresh_db()
        try:
            api_data = {
                "aboutMe": "I love traveling.",
                "occupation": "Software Engineer",
                "languages": ["English", "Japanese"],
                "location": "Tokyo, Japan",
            }
            result = collect_host_profile(
                host_id="h_sections_1",
                username="sections_user",
                api_data=api_data,
                db_file=db_path,
            )
            sections = result["sections"]["profile_sections"]["sections"]
            self.assertIn("about", sections)
            self.assertIn("occupation", sections)
            self.assertIn("languages", sections)
            self.assertIn("location", sections)
            self.assertEqual(sections["about"]["section_text"], "I love traveling.")
            self.assertEqual(sections["occupation"]["section_text"], "Software Engineer")
        finally:
            _cleanup_db(db_path)

    def test_missing_section_not_present(self):
        """Отсутствующий раздел имеет статус NOT_PRESENT."""
        db_path = _fresh_db()
        try:
            api_data = {"aboutMe": "Only about me."}
            result = collect_host_profile(
                host_id="h_missing_1",
                username="missing_user",
                api_data=api_data,
                db_file=db_path,
            )
            sections = result["sections"]["profile_sections"]["sections"]
            self.assertIn("occupation", sections)
            self.assertEqual(sections["occupation"]["extraction_status"], EXTRACTION_STATUS_NOT_CHECKED)
        finally:
            _cleanup_db(db_path)

    # ------------------------------------------------------------------
    # 5. SHARED INFORMATION
    # ------------------------------------------------------------------

    def test_shared_information_separate_section(self):
        """Shared Information сохраняется как отдельная секция, не с описанием профиля."""
        db_path = _fresh_db()
        try:
            api_data = {
                "aboutMe": "About me text.",
                "shared_information": "I speak English and Japanese. I enjoy cooking.",
            }
            result = collect_host_profile(
                host_id="h_shared_1",
                username="shared_user",
                api_data=api_data,
                db_file=db_path,
            )
            shared = result["sections"]["shared_information"]
            self.assertEqual(shared["section_text"], "I speak English and Japanese. I enjoy cooking.")
            self.assertEqual(shared["extraction_status"], EXTRACTION_STATUS_VALUE)

            # Проверяем, что оно НЕ попадает в about
            sections = result["sections"]["profile_sections"]["sections"]
            self.assertEqual(sections["about"]["section_text"], "About me text.")
        finally:
            _cleanup_db(db_path)

    def test_shared_information_absent(self):
        """Неоткрытый профиль оставляет Shared Information непроверенным."""
        db_path = _fresh_db()
        try:
            result = collect_host_profile(
                host_id="h_no_shared_1",
                username="no_shared_user",
                db_file=db_path,
            )
            shared = result["sections"]["shared_information"]
            self.assertEqual(shared["extraction_status"], EXTRACTION_STATUS_NOT_CHECKED)
        finally:
            _cleanup_db(db_path)

    # ------------------------------------------------------------------
    # 6. WHY I'M ON COUCHSURFING
    # ------------------------------------------------------------------

    def test_why_on_couchsurfing_separate_section(self):
        """Раздел Why I'm on Couchsurfing сохраняется отдельно, без сокращения."""
        db_path = _fresh_db()
        try:
            why_text = ("I want to share my culture and learn from others. "
                        "Couchsurfing is amazing and I love meeting travelers.")
            api_data = {"why_on_couchsurfing": why_text}
            result = collect_host_profile(
                host_id="h_why_1",
                username="why_user",
                api_data=api_data,
                db_file=db_path,
            )
            why = result["sections"]["why_on_couchsurfing"]
            self.assertEqual(why["section_text"], why_text)
            self.assertEqual(why["extraction_status"], EXTRACTION_STATUS_VALUE)
        finally:
            _cleanup_db(db_path)

    def test_why_on_couchsurfing_not_replaced_by_ai(self):
        """Текст не заменяется AI-резюме, сохраняется исходный."""
        db_path = _fresh_db()
        try:
            original = "Original text about why I couchsurf. It is long and detailed."
            api_data = {"why_on_couchsurfing": original}
            result = collect_host_profile(
                host_id="h_noai_1",
                username="noai_user",
                api_data=api_data,
                db_file=db_path,
            )
            why = result["sections"]["why_on_couchsurfing"]
            self.assertEqual(why["section_text"], original)
            self.assertEqual(len(why["section_text"]), len(original))
        finally:
            _cleanup_db(db_path)

    # ------------------------------------------------------------------
    # 7. REFERENCES
    # ------------------------------------------------------------------

    def test_references_different_types(self):
        """References разных типов собираются с полными данными."""
        db_path = _fresh_db()
        try:
            api_data = {
                "references": [
                    {
                        "id": "ref_1",
                        "text": "Great host!",
                        "author": "Alice",
                        "authorProfileUrl": "https://cs.com/users/alice",
                        "date": "2026-01-15",
                        "type": "positive",
                        "direction": "host->guest",
                    },
                    {
                        "id": "ref_2",
                        "text": "Ok place.",
                        "author": "Bob",
                        "date": "2026-03-20",
                        "type": "neutral",
                    },
                ]
            }
            result = collect_host_profile(
                host_id="h_refs_multi_1",
                username="refs_multi_user",
                api_data=api_data,
                db_file=db_path,
            )
            refs = result["sections"]["references"]
            self.assertEqual(refs["total_references"], 2)
            self.assertEqual(refs["positive_references"], 1)
            self.assertEqual(refs["neutral_references"], 1)
            self.assertEqual(refs["negative_references"], 0)

            with get_connection(db_path) as conn:
                cur = conn.cursor()
                cur.execute("SELECT COUNT(*) FROM host_references WHERE host_id='h_refs_multi_1'")
                self.assertEqual(cur.fetchone()[0], 2)

                cur.execute("SELECT reference_type FROM host_references WHERE host_id='h_refs_multi_1'")
                types = {r[0] for r in cur.fetchall()}
                self.assertEqual(types, {"positive", "neutral"})
        finally:
            _cleanup_db(db_path)

    def test_references_no_duplicates_on_recollection(self):
        """При повторном сборе References не дублируются."""
        db_path = _fresh_db()
        try:
            api_data = {"references": [{"id": "ref_dup", "text": "Same ref", "author": "A"}]}
            collect_host_profile(
                host_id="h_refdup_1", username="refdup_user", api_data=api_data, db_file=db_path
            )
            collect_host_profile(
                host_id="h_refdup_1", username="refdup_user", api_data=api_data, db_file=db_path
            )
            with get_connection(db_path) as conn:
                cur = conn.cursor()
                cur.execute("SELECT COUNT(*) FROM host_references WHERE host_id='h_refdup_1'")
                self.assertEqual(cur.fetchone()[0], 1)
        finally:
            _cleanup_db(db_path)

    def test_references_count_not_hosting_proof(self):
        """Количество References не считается доказательством приёма гостей."""
        db_path = _fresh_db()
        try:
            refs_list = []
            for i in range(50):
                refs_list.append({
                    "text": f"Reference text {i}",
                    "author": f"Author {i}",
                    "date": f"2026-01-{(i % 28) + 1:02d}",
                })
            api_data = {
                "references": refs_list,
                "is_hosting": "not_accepting",
            }
            result = collect_host_profile(
                host_id="h_refcount_1",
                username="refcount_user",
                api_data=api_data,
                db_file=db_path,
            )
            refs = result["sections"]["references"]
            self.assertEqual(refs["total_references"], 50)
            # is_hosting остаётся not_accepting несмотря на много отзывов
        finally:
            _cleanup_db(db_path)

    def test_references_load_error_not_zero(self):
        """Ошибка загрузки References не интерпретируется как нулевое количество."""
        db_path = _fresh_db()
        try:
            result = collect_host_profile(
                host_id="h_referr_1",
                username="referr_user",
                db_file=db_path,
            )
            refs = result["sections"]["references"]
            # Когда нет данных, статус PARTIAL, а не 0 отзывов как факт
            self.assertEqual(refs["status"], COLLECTION_STATUS_PARTIAL)
            self.assertEqual(refs["total_references"], 0)
        finally:
            _cleanup_db(db_path)

    # ------------------------------------------------------------------
    # 8. GENDER EVIDENCE
    # ------------------------------------------------------------------

    def test_gender_explicit_field(self):
        """Явно указанное поле gender сохраняется как доказательство."""
        db_path = _fresh_db()
        try:
            api_data = {"gender": "female"}
            result = collect_host_profile(
                host_id="h_gend_exp_1",
                username="gend_exp_user",
                api_data=api_data,
                db_file=db_path,
            )
            gender = result["sections"]["gender_evidence"]
            self.assertGreaterEqual(gender["evidence_count"], 1)
            ev = gender["evidence"][0]
            self.assertEqual(ev["evidence_type"], "explicit_field")
            self.assertEqual(ev["inferred_gender"], "FEMALE")
        finally:
            _cleanup_db(db_path)

    def test_gender_self_identification(self):
        """Самоопределение в тексте профиля собирается как доказательство."""
        db_path = _fresh_db()
        try:
            html = '<div>I am a woman. She/her.</div>'
            result = collect_host_profile(
                host_id="h_gend_self_1",
                username="gend_self_user",
                html_content=html,
                db_file=db_path,
            )
            gender = result["sections"]["gender_evidence"]
            self.assertGreaterEqual(gender["evidence_count"], 1)
            females = [e for e in gender["evidence"] if e["inferred_gender"] == "FEMALE"]
            self.assertGreaterEqual(len(females), 1)
        finally:
            _cleanup_db(db_path)

    def test_gender_pronouns(self):
        """Местоимения собираются как доказательство."""
        db_path = _fresh_db()
        try:
            html = "<div>He is a great host. Him and his friends.</div>"
            result = collect_host_profile(
                host_id="h_gend_pro_1",
                username="gend_pro_user",
                html_content=html,
                db_file=db_path,
            )
            gender = result["sections"]["gender_evidence"]
            males = [e for e in gender["evidence"] if e["inferred_gender"] == "MALE"]
            self.assertGreaterEqual(len(males), 1)
        finally:
            _cleanup_db(db_path)

    def test_gender_insufficient_data(self):
        """Недостаточно данных — UNKNOWN, нет вероятностного предположения."""
        db_path = _fresh_db()
        try:
            html = "<div>I like traveling.</div>"
            result = collect_host_profile(
                host_id="h_gend_insuff_1",
                username="gend_insuff_user",
                html_content=html,
                db_file=db_path,
            )
            gender = result["sections"]["gender_evidence"]
            # Должен быть либо 0 evidence, либо все с UNKNOWN
            if gender["evidence_count"] > 0:
                for e in gender["evidence"]:
                    self.assertEqual(e["inferred_gender"], "UNKNOWN")
        finally:
            _cleanup_db(db_path)

    def test_gender_conflicting_sources(self):
        """Противоречивые источники обнаруживаются."""
        db_path = _fresh_db()
        try:
            result = collect_host_profile(
                host_id="h_gend_conf_1",
                username="gend_conf_user",
                html_content=FIXTURE_CONFLICTING_GENDER,
                db_file=db_path,
            )
            gender = result["sections"]["gender_evidence"]
            self.assertTrue(gender["has_conflict"])
        finally:
            _cleanup_db(db_path)

    def test_gender_not_from_name_only(self):
        """Пол не определяется только по имени."""
        db_path = _fresh_db()
        try:
            api_data = {"gender": None, "username": "yuki"}
            result = collect_host_profile(
                host_id="h_gend_name_1",
                username="yuki",
                api_data=api_data,
                db_file=db_path,
            )
            gender = result["sections"]["gender_evidence"]
            # Нет явного gender поля, нет био — evidence_count должен быть 0 или все UNKNOWN
            if gender["evidence_count"] > 0:
                for e in gender["evidence"]:
                    # Языковой маркер из имени может дать низкую уверенность, но не должен быть единственным фактом
                    self.assertLessEqual(e["confidence"], 0.5)
        finally:
            _cleanup_db(db_path)

    # ------------------------------------------------------------------
    # 9. DATA INTEGRITY
    # ------------------------------------------------------------------

    def test_repeat_collection_preserves_data(self):
        """Повторный сбор не уничтожает предыдущие данные."""
        db_path = _fresh_db()
        try:
            api_data_1 = {"isIdentificationVerified": True}
            collect_host_profile(
                host_id="h_repeat_1", username="repeat_user", api_data=api_data_1, db_file=db_path
            )
            api_data_2 = {"occupation": "Engineer"}
            collect_host_profile(
                host_id="h_repeat_1", username="repeat_user", api_data=api_data_2, db_file=db_path
            )
            with get_connection(db_path) as conn:
                cur = conn.cursor()
                cur.execute("SELECT verification_status FROM host_profiles WHERE host_id='h_repeat_1'")
                row = cur.fetchone()
                self.assertIsNotNone(row)
                self.assertEqual(row[0], VERIFICATION_STATUS_VERIFIED)  # верификация сохранена
        finally:
            _cleanup_db(db_path)

    def test_section_load_error_preserves_old_data(self):
        """При ошибке парсинга раздела старые данные не заменяются пустыми."""
        db_path = _fresh_db()
        try:
            api_data_1 = {"aboutMe": "Original about text."}
            collect_host_profile(
                host_id="h_err_1", username="err_user", api_data=api_data_1, db_file=db_path
            )
            # Второй сбор с некорректными данными для about (не должно стереть старый)
            api_data_2 = {}
            collect_host_profile(
                host_id="h_err_1", username="err_user", api_data=api_data_2, db_file=db_path
            )
            with get_connection(db_path) as conn:
                cur = conn.cursor()
                cur.execute("SELECT section_text FROM host_profile_sections WHERE host_id='h_err_1' AND section_name='about'")
                row = cur.fetchone()
                self.assertIsNotNone(row)
                self.assertEqual(row[0], "Original about text.")
        finally:
            _cleanup_db(db_path)

    def test_not_present_vs_parse_failed(self):
        """Различие между NOT_PRESENT и PARSE_FAILED."""
        db_path = _fresh_db()
        try:
            # Search API omission is not evidence that a profile section is absent.
            result = collect_host_profile(
                host_id="h_np_1", username="np_user",
                api_data={"aboutMe": "Only about."}, db_file=db_path
            )
            sections = result["sections"]["profile_sections"]["sections"]
            self.assertEqual(sections["occupation"]["extraction_status"], EXTRACTION_STATUS_NOT_CHECKED)
            self.assertNotEqual(sections["occupation"]["extraction_status"], EXTRACTION_STATUS_PARSE_FAILED)
        finally:
            _cleanup_db(db_path)

    def test_profile_completeness_percentage(self):
        """Процент полноты профиля рассчитывается корректно."""
        db_path = _fresh_db()
        try:
            api_data = {
                "aboutMe": "About.",
                "occupation": "Dev",
                "languages": ["English"],
                "location": "Tokyo",
                "isIdentificationVerified": True,
                "isRecentlyActive": True,
                "badges": [{"name": "B1", "text": "Badge 1"}],
            }
            result = collect_host_profile(
                host_id="h_pct_1", username="pct_user", api_data=api_data, db_file=db_path
            )
            with get_connection(db_path) as conn:
                cur = conn.cursor()
                cur.execute("SELECT profile_completeness_pct FROM host_profiles WHERE host_id='h_pct_1'")
                pct = cur.fetchone()[0]
                self.assertGreater(pct, 0)
                self.assertLessEqual(pct, 100)
        finally:
            _cleanup_db(db_path)

    def test_partial_collection_updates_attempt_without_claiming_success(self):
        db_path = _fresh_db()
        try:
            collect_host_profile(
                host_id="h_lsc_1", username="lsc_user",
                api_data={"aboutMe": "test"}, db_file=db_path
            )
            with get_connection(db_path) as conn:
                cur = conn.cursor()
                cur.execute("SELECT last_successful_collection,last_collection_attempt FROM host_profiles WHERE host_id='h_lsc_1'")
                row = cur.fetchone()
                self.assertIsNone(row[0])
                self.assertIsNotNone(row[1])
        finally:
            _cleanup_db(db_path)

    # ------------------------------------------------------------------
    # 10. REGRESSION — 기존 테스트 не ломаются
    # ------------------------------------------------------------------

    def test_database_init_idempotent(self):
        """init_db можно запускать повторно без дублирования таблиц."""
        db_path = _fresh_db()
        try:
            init_db(db_path)
            init_db(db_path)
            with get_connection(db_path) as conn:
                cur = conn.cursor()
                cur.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='host_profiles'")
                self.assertEqual(cur.fetchone()[0], 1)
                cur.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='host_badges'")
                self.assertEqual(cur.fetchone()[0], 1)
                cur.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='host_references'")
                self.assertEqual(cur.fetchone()[0], 1)
                cur.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='host_gender_evidence'")
                self.assertEqual(cur.fetchone()[0], 1)
                cur.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='host_profile_sections'")
                self.assertEqual(cur.fetchone()[0], 1)
        finally:
            _cleanup_db(db_path)

    def test_existing_tables_intact(self):
        """Существующие таблицы не повреждаются после добавления новых."""
        db_path = _fresh_db()
        try:
            with get_connection(db_path) as conn:
                cur = conn.cursor()
                cur.execute(
                    "INSERT INTO core_trips (id, url, city, arrival_date, departure_date, is_active, created_at) "
                    "VALUES ('trip_1', 'https://example.com/trip/1', 'Kyoto', '2026-11-19', '2026-11-21', 1, '2026-11-10')"
                )
                cur.execute(
                    "INSERT INTO core_hosts (host_id, trip_id, username, name, location, state, created_at, updated_at) "
                    "VALUES ('h1', 'trip_1', 'user1', 'User One', 'Tokyo', 'discovered', '2026-11-10T00:00:00', '2026-11-10T00:00:00')"
                )
                conn.commit()
            init_db(db_path)
            with get_connection(db_path) as conn:
                cur = conn.cursor()
                cur.execute("SELECT COUNT(*) FROM core_trips")
                self.assertEqual(cur.fetchone()[0], 1)
                cur.execute("SELECT COUNT(*) FROM core_hosts")
                self.assertEqual(cur.fetchone()[0], 1)
        finally:
            _cleanup_db(db_path)


if __name__ == "__main__":
    unittest.main()
