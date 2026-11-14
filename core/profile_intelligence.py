"""
core/profile_intelligence.py — Сбор, структурирование и сохранение информации о Couchsurfing-хостах.

Реализует полноценную систему сбора профильной информации:
1. Верификация профиля (VERIFIED / NOT_VERIFIED / UNKNOWN) с источником и доказательством.
2. Статус активности (Recently Active) с сырым и нормализованным значением.
3. Все бейджи профиля (без ограничения заранее известным списком).
4. Отдельные секции профиля: About, Personal description, Interests, Languages,
   Occupation, Education, Location, Living situation, Additional information.
5. Раздел Shared Information (самостоятельный).
6. Раздел Why I'm on Couchsurfing.
7. References с полной статистикой.
8. Доказательства для определения пола (несколько независимых методов).
9. Data Provenance для каждого важного факта.
10. Безопасное обновление с историей изменений (без потери старых данных).

Все операции с БД используют get_connection() из core.database.
Никаких прямых sqlite3.connect вне core/database.py.
"""

import hashlib
import json
import logging
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.database import get_connection, log_audit

logger = logging.getLogger("ProfileIntelligence")

# ---------------------------------------------------------------------------
# Константы статусов и секций
# ---------------------------------------------------------------------------

VERIFICATION_STATUS_VERIFIED = "VERIFIED"
VERIFICATION_STATUS_NOT_VERIFIED = "NOT_VERIFIED"
VERIFICATION_STATUS_UNKNOWN = "UNKNOWN"

RECENTLY_ACTIVE_STATUS_RECENTLY_ACTIVE = "RECENTLY_ACTIVE"
RECENTLY_ACTIVE_STATUS_ACTIVE = "ACTIVE"
RECENTLY_ACTIVE_STATUS_NOT_RECENTLY_ACTIVE = "NOT_RECENTLY_ACTIVE"
RECENTLY_ACTIVE_STATUS_UNKNOWN = "UNKNOWN"

EXTRACTION_STATUS_VALUE = "VALUE"
EXTRACTION_STATUS_NOT_PRESENT = "NOT_PRESENT"
EXTRACTION_STATUS_NOT_AVAILABLE = "NOT_AVAILABLE"
EXTRACTION_STATUS_PARSE_FAILED = "PARSE_FAILED"
EXTRACTION_STATUS_NOT_CHECKED = "NOT_CHECKED"

COLLECTION_STATUS_SUCCESS = "SUCCESS"
COLLECTION_STATUS_PARTIAL = "PARTIAL"
COLLECTION_STATUS_FAILED = "FAILED"

# Стандартные секции профиля, которые система пытается извлечь
PROFILE_SECTIONS = [
    "about",
    "house_rules",
    "personal_description",
    "interests",
    "languages",
    "occupation",
    "education",
    "location",
    "living_situation",
    "additional_information",
    "shared_information",
    "why_on_couchsurfing",
]

GENDER_EVIDENCE_TYPES = [
    "explicit_field",
    "self_identification",
    "pronoun",
    "language_marker",
    "reference",
]

GENDER_INFERRED_MALE = "MALE"
GENDER_INFERRED_FEMALE = "FEMALE"
GENDER_INFERRED_UNKNOWN = "UNKNOWN"

# CSS-селекторы для парсинга профилей Couchsurfing.
# Эти селекторы основаны на типичной структуре сайта и могут требовать
# обновления при изменении верстки Couchsurfing.
PROFILE_SELECTORS = {
    "verification_badge": [
        '[data-testid*="verification"]',
        '[class*="verification"]',
        '[class*="verified"]',
        'svg[aria-label*="Verified"]',
        'svg[aria-label*="verified"]',
        'text:contains("Verified")',
        'text:contains("Верифицирован")',
    ],
    "recently_active": [
        '[data-testid*="recently-active"]',
        '[class*="recently-active"]',
        '[class*="recently_active"]',
        '[class*="last-active"]',
        '[class*="last_active"]',
        'text:contains("Recently active")',
        'text:contains("Recently Active")',
        'text:contains("Active recently")',
        'text:contains("Последняя активность")',
    ],
    "badges": [
        '[data-testid*="badge"]',
        '[class*="badge"]',
        '[class*="achievement"]',
    ],
    "about_section": [
        '[data-testid*="about"]',
        '[class*="about-section"]',
        '[class*="about_text"]',
        '[class*="profile-about"]',
        '[class*="self-description"]',
    ],
    "house_rules": [
        '[data-testid*="house-rules"]',
        '[class*="house-rules"]',
        '[class*="house_rules"]',
    ],
    "shared_information": [
        '[data-testid*="shared-information"]',
        '[class*="shared-information"]',
        '[class*="shared_info"]',
        'h2:contains("Shared Information")',
        'h2:contains("Общая информация")',
    ],
    "why_on_couchsurfing": [
        '[data-testid*="why-on-couchsurfing"]',
        '[class*="why-on-couchsurfing"]',
        '[class*="why_couchsurfing"]',
        'h2:contains("Why I am on Couchsurfing")',
        'h2:contains("Почему я на Couchsurfing")',
        'h2:contains("Why I\'m on Couchsurfing")',
    ],
}

# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now().isoformat()

def _generate_uuid() -> str:
    return str(uuid.uuid4())

def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

def _safe_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    s = str(value).strip()
    return s if s else None

def _normalize_verification(raw: Optional[str]) -> Tuple[str, Optional[str], Optional[str]]:
    """
    Нормализует сырую строку верификации в статус VERIFIED/NOT_VERIFIED/UNKNOWN.
    Возвращает (status, source, evidence).
    """
    if not raw:
        return (VERIFICATION_STATUS_UNKNOWN, None, None)
    lower = raw.lower()
    # Сначала проверяем NOT_VERIFIED (включая варианты без пробела/с дефисом)
    if any(k in lower for k in ("not verified", "not_verified", "not-verified",
                                 "не верифицирован", "unverified", "non-verified")):
        return (VERIFICATION_STATUS_NOT_VERIFIED, "profile_badge", raw)
    if any(k in lower for k in ("verified", "верифицирован", "identification verified", "is_verified")):
        return (VERIFICATION_STATUS_VERIFIED, "profile_badge", raw)
    return (VERIFICATION_STATUS_UNKNOWN, None, raw)

def _normalize_recently_active(raw: Optional[str]) -> Tuple[str, Optional[str], Optional[str]]:
    """
    Нормализует сырую строку Recently Active.
    Возвращает (status, normalized_value, raw_value).
    """
    if not raw:
        return (RECENTLY_ACTIVE_STATUS_UNKNOWN, None, None)
    lower = raw.lower()
    if any(k in lower for k in ("not recently active", "not active recently", "inactive", "не активен", "недавно не активен", "давно не активен")):
        return (RECENTLY_ACTIVE_STATUS_NOT_RECENTLY_ACTIVE, "not_recently_active", raw)
    if any(k in lower for k in ("recently active", "recently_active", "недавно активен", "активен récemment")):
        return (RECENTLY_ACTIVE_STATUS_RECENTLY_ACTIVE, "recently_active", raw)
    if any(k in lower for k in ("active", "активен", "online", "в сети")):
        return (RECENTLY_ACTIVE_STATUS_ACTIVE, "active", raw)
    return (RECENTLY_ACTIVE_STATUS_UNKNOWN, None, raw)


# ---------------------------------------------------------------------------
# Основной движок
# ---------------------------------------------------------------------------

class ProfileIntelligenceEngine:
    """
    Движок сбора профильной информации о хосте Couchsurfing.

    Собирает максимально полную и достоверную информацию о профиле хоста,
    сохраняя её в нормализованных таблицах SQLite с отслеживанием происхождения
    каждого факта (Data Provenance).
    """

    def __init__(self, db_file: Optional[Path | str] = None):
        self.db_file = Path(db_file) if db_file else None

    # ------------------------------------------------------------------
    # Точка входа: полный сбор профиля
    # ------------------------------------------------------------------

    def collect_host_profile(
        self,
        host_id: str,
        username: str,
        profile_url: Optional[str] = None,
        html_content: Optional[str] = None,
        page: Any = None,
        api_data: Optional[Dict[str, Any]] = None,
        profile_page_status: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Полный сбор профиля хоста из всех доступных источников.

        Args:
            host_id: Идентификатор хоста в приложении.
            username: Имя пользователя Couchsurfing.
            profile_url: URL профиля хоста.
            html_content: HTML контент страницы профиля (для тестирования/fallback).
            page: Объект Playwright page (для реального сбора).
            api_data: Данные из сетевого API (если доступны).

        Возвращает словарь с результатами сбора по каждой секции.
        """
        now = _now_iso()
        page_status = profile_page_status or ("SUCCESS" if html_content is not None or page is not None else "NOT_ATTEMPTED")
        results: Dict[str, Any] = {
            "host_id": host_id,
            "username": username,
            "profile_url": profile_url,
            "collected_at": now,
            "sections": {},
            "overall_status": COLLECTION_STATUS_PARTIAL,
        }

        # Гарантируем наличие основной записи профиля
        self._upsert_host_profile(host_id, username, profile_url, now)

        # 1. Верификация профиля
        verif_result = self._collect_verification(host_id, username, profile_url, html_content, page, api_data, now)
        results["sections"]["verification"] = verif_result

        # 2. Recently Active
        active_result = self._collect_recently_active(host_id, username, profile_url, html_content, page, api_data, now)
        results["sections"]["recently_active"] = active_result

        # 3. Все бейджи
        badges_result = self._collect_badges(host_id, username, profile_url, html_content, page, api_data, now)
        results["sections"]["badges"] = badges_result

        # 4. Отдельные секции профиля
        sections_result = self._collect_profile_sections(host_id, username, profile_url, html_content, page, api_data, now)
        results["sections"]["profile_sections"] = sections_result

        # 5. Shared Information (отдельная секция)
        shared_result = self._collect_shared_information(host_id, username, profile_url, html_content, page, api_data, now)
        results["sections"]["shared_information"] = shared_result

        # 6. Why I'm on Couchsurfing (отдельная секция)
        why_result = self._collect_why_on_couchsurfing(host_id, username, profile_url, html_content, page, api_data, now)
        results["sections"]["why_on_couchsurfing"] = why_result

        # 7. References
        refs_result = self._collect_references(host_id, username, profile_url, html_content, page, api_data, now)
        results["sections"]["references"] = refs_result

        # 8. Gender evidence
        gender_result = self._collect_gender_evidence(host_id, username, html_content, page, api_data, now)
        results["sections"]["gender_evidence"] = gender_result

        page_was_checked = html_content is not None or page is not None
        for key, payload in results["sections"].items():
            if isinstance(payload, dict) and "extraction_status" not in payload:
                found = bool(
                    payload.get("badges_found") or payload.get("references_found")
                    or payload.get("verification_status") not in (None, VERIFICATION_STATUS_UNKNOWN)
                    or payload.get("recently_active_status") not in (None, RECENTLY_ACTIVE_STATUS_UNKNOWN)
                )
                payload["extraction_status"] = EXTRACTION_STATUS_VALUE if found else (
                    EXTRACTION_STATUS_NOT_PRESENT if page_was_checked else EXTRACTION_STATUS_NOT_CHECKED
                )
                payload.setdefault("source", "profile_page" if page_was_checked else "not_checked")

        nested_sections = results["sections"].get("profile_sections", {}).get("sections", {})
        for section_name, section_result in nested_sections.items():
            section_result.setdefault(
                "extraction_status",
                EXTRACTION_STATUS_NOT_PRESENT if page_was_checked else EXTRACTION_STATUS_NOT_CHECKED,
            )

        if page_status == "FAILED":
            for key, value in results["sections"].items():
                if value.get("extraction_status") in (None, EXTRACTION_STATUS_NOT_CHECKED, EXTRACTION_STATUS_NOT_PRESENT):
                    value["extraction_status"] = EXTRACTION_STATUS_NOT_AVAILABLE
                    value["source"] = "profile_page_unavailable"
                    if key in PROFILE_SECTIONS:
                        self._upsert_profile_section(
                            host_id, key, None, None, EXTRACTION_STATUS_NOT_AVAILABLE,
                            profile_url, now, extraction_source="profile_page_unavailable",
                        )
            for section_name, section_result in nested_sections.items():
                if section_result.get("extraction_status") in (EXTRACTION_STATUS_NOT_CHECKED, EXTRACTION_STATUS_NOT_PRESENT):
                    section_result["extraction_status"] = EXTRACTION_STATUS_NOT_AVAILABLE
                    section_result["source"] = "profile_page_unavailable"
                    self._upsert_profile_section(
                        host_id, section_name, None, None, EXTRACTION_STATUS_NOT_AVAILABLE,
                        profile_url, now, extraction_source="profile_page_unavailable",
                    )
            results["sections"]["profile_page"] = {
                "status": COLLECTION_STATUS_FAILED,
                "extraction_status": EXTRACTION_STATUS_NOT_AVAILABLE,
                "source": "playwright_navigation",
            }

        # Определяем общий статус
        section_statuses = [
            s.get("status", COLLECTION_STATUS_FAILED)
            for s in results["sections"].values()
        ]
        if all(s == COLLECTION_STATUS_SUCCESS for s in section_statuses):
            results["overall_status"] = COLLECTION_STATUS_SUCCESS
        elif any(s == COLLECTION_STATUS_FAILED for s in section_statuses):
            results["overall_status"] = COLLECTION_STATUS_PARTIAL
        else:
            results["overall_status"] = COLLECTION_STATUS_PARTIAL

        # Обновляем основную запись профиля
        self._finalize_host_profile(host_id, username, results, now)

        with get_connection(self.db_file) as conn:
            conn.execute(
                """INSERT INTO host_profile_snapshots
                   (snapshot_id,host_id,collection_timestamp,raw_profile_json,sections_json)
                   VALUES(?,?,?,?,?)""",
                (_sha256(f"{host_id}|{now}"), host_id, now,
                 json.dumps(api_data or {}, ensure_ascii=False),
                 json.dumps(results["sections"], ensure_ascii=False, default=str)),
            )

        # Записываем в лог сбора
        self._log_collection(host_id, "full_profile", results["overall_status"], None, now)

        # Аудит
        try:
            log_audit(
                trip_id="global",
                module="PROFILE_INTELLIGENCE",
                event="HOST_PROFILE_COLLECTED",
                host_id=host_id,
                details={
                    "username": username,
                    "overall_status": results["overall_status"],
                    "sections": {k: v.get("status") for k, v in results["sections"].items()},
                },
                db_file=self.db_file,
            )
        except Exception:
            pass

        return results

    # ------------------------------------------------------------------
    # 1. Верификация профиля
    # ------------------------------------------------------------------

    def _collect_verification(
        self,
        host_id: str,
        username: str,
        profile_url: Optional[str],
        html_content: Optional[str],
        page: Any,
        api_data: Optional[Dict[str, Any]],
        now: str,
    ) -> Dict[str, Any]:
        """
        Сбор статуса верификации профиля.
        Допустимые значения: VERIFIED / NOT_VERIFIED / UNKNOWN.
        Статус не определяется по количеству References.
        """
        raw_value: Optional[str] = None
        source: Optional[str] = None
        evidence: Optional[str] = None

        # Пытаемся из api_data
        if api_data:
            is_verified = api_data.get("isIdentificationVerified")
            if is_verified is None:
                is_verified = api_data.get("is_verified")
            if is_verified is not None:
                raw_value = "verified" if bool(is_verified) else "not_verified"
                source = "network_api"
                evidence = str(is_verified)

        # Пытаемся из HTML
        if raw_value is None and html_content:
            for sel in PROFILE_SELECTORS["verification_badge"]:
                m = re.search(r'class="[^"]*' + re.escape(sel.replace('[class*="', '').replace('"]', '')) + r'[^"]*"', html_content)
                if m:
                    raw_value = m.group(0)
                    source = "dom_selector"
                    evidence = sel
                    break
            # Поиск текста "Verified"
            if raw_value is None:
                if re.search(r"not[\s_-]+verified|unverified|не верифицирован", html_content, re.IGNORECASE):
                    raw_value = "not_verified_text_found"
                    source = "dom_text"
                    evidence = "text_match:not_verified"
                elif re.search(r"verified", html_content, re.IGNORECASE):
                    raw_value = "verified_text_found"
                    source = "dom_text"
                    evidence = "text_match:verified"

        # Пытаемся из Playwright page
        if raw_value is None and page is not None:
            try:
                for sel in PROFILE_SELECTORS["verification_badge"]:
                    try:
                        els = page.locator(sel)
                        if els.count() > 0:
                            raw_value = els.first.inner_text() or sel
                            source = "playwright_selector"
                            evidence = sel
                            break
                    except Exception:
                        continue
            except Exception:
                pass

        status, norm_source, norm_evidence = _normalize_verification(raw_value)
        if norm_source:
            source = norm_source
        if norm_evidence:
            evidence = norm_evidence

        self._upsert_verification(host_id, status, source, evidence, now)

        return {
            "status": COLLECTION_STATUS_SUCCESS if raw_value is not None else COLLECTION_STATUS_PARTIAL,
            "verification_status": status,
            "verification_source": source,
            "verification_evidence": evidence,
            "raw_value": raw_value,
        }

    # ------------------------------------------------------------------
    # 2. Recently Active
    # ------------------------------------------------------------------

    def _collect_recently_active(
        self,
        host_id: str,
        username: str,
        profile_url: Optional[str],
        html_content: Optional[str],
        page: Any,
        api_data: Optional[Dict[str, Any]],
        now: str,
    ) -> Dict[str, Any]:
        """
        Сбор статуса Recently Active.
        Допустимые значения: RECENTLY_ACTIVE / ACTIVE / NOT_RECENTLY_ACTIVE / UNKNOWN.
        Сохраняет исходное значение, нормализованное значение, дату сбора, URL, источник.
        """
        raw_value: Optional[str] = None
        source: Optional[str] = None
        evidence: Optional[str] = None
        last_active_url: Optional[str] = profile_url

        # Из api_data
        if api_data:
            is_active = api_data.get("isRecentlyActive")
            if is_active is not None:
                raw_value = "recently_active" if bool(is_active) else "not_recently_active"
                source = "network_api"
                evidence = f"isRecentlyActive={is_active}"

            last_login = api_data.get("lastLogin") or api_data.get("last_login")
            if last_login:
                # last_login сохраняется отдельно, не перезаписывает raw_value
                if evidence:
                    evidence = f"{evidence}; last_login={last_login}"
                else:
                    evidence = f"last_login={last_login}"

        # Из HTML
        if raw_value is None and html_content:
            for pat in [r"Recently Active", r"Recently active", r"Активен недавно", r"Последняя активность"]:
                m = re.search(pat, html_content)
                if m:
                    raw_value = m.group(0)
                    source = "dom_text"
                    evidence = f"text_match:{pat}"
                    break

        # Из Playwright
        if raw_value is None and page is not None:
            try:
                for sel in PROFILE_SELECTORS["recently_active"]:
                    try:
                        els = page.locator(sel)
                        if els.count() > 0:
                            txt = els.first.inner_text() or ""
                            if txt:
                                raw_value = txt
                                source = "playwright_selector"
                                evidence = sel
                                break
                    except Exception:
                        continue
            except Exception:
                pass

        status, norm_value, norm_raw = _normalize_recently_active(raw_value)
        if norm_value:
            raw_value = norm_raw or raw_value

        self._upsert_recently_active(host_id, status, raw_value, norm_value, last_active_url, now)

        return {
            "status": COLLECTION_STATUS_SUCCESS if raw_value is not None else COLLECTION_STATUS_PARTIAL,
            "recently_active_status": status,
            "recently_active_raw": raw_value,
            "recently_active_normalized": norm_value,
            "last_active_url": last_active_url,
            "source": source,
        }

    # ------------------------------------------------------------------
    # 3. Все бейджи
    # ------------------------------------------------------------------

    def _collect_badges(
        self,
        host_id: str,
        username: str,
        profile_url: Optional[str],
        html_content: Optional[str],
        page: Any,
        api_data: Optional[Dict[str, Any]],
        now: str,
    ) -> Dict[str, Any]:
        """
        Сбор ВСЕХ доступных бейджов с карточки хоста и страницы профиля.
        Не ограничивается заранее известным списком.
        """
        badges: List[Dict[str, Any]] = []
        seen_keys: set = set()

        # Из api_data (badges могут быть в JSON ответе)
        if api_data:
            raw_badges = api_data.get("badges") or api_data.get("achievements") or []
            if isinstance(raw_badges, dict):
                raw_badges = raw_badges.get("items") or raw_badges.get("edges") or raw_badges.get("nodes") or []
                raw_badges = [item.get("node", item) if isinstance(item, dict) else item for item in raw_badges]
            if isinstance(raw_badges, list):
                for b in raw_badges:
                    if isinstance(b, str):
                        name, text, category, image_url = b.strip(), b.strip(), None, None
                    elif isinstance(b, dict):
                        name = b.get("name") or b.get("title") or b.get("label") or "Unknown Badge"
                        text = b.get("text") or b.get("description") or ""
                        category = b.get("category") or b.get("type")
                        image_url = b.get("image_url") or b.get("icon") or b.get("badge_image_url")
                    else:
                        continue
                    badge_key = f"{host_id}|{name}|{text}"
                    if badge_key in seen_keys:
                        continue
                    seen_keys.add(badge_key)
                    badges.append({
                        "badge_id": _sha256(badge_key),
                        "badge_name": name,
                        "badge_text": text,
                        "badge_category": category,
                        "badge_image_url": image_url,
                        "source_html_attribute": "network_api",
                        "source_url": profile_url,
                        "collected_at": now,
                    })

        # Из HTML (поиск элементов бейджей)
        if html_content:
            # Ищем паттерны бейджей в HTML
            badge_patterns = [
                r'<[^>]+class="[^"]*badge[^"]*"[^>]*>(.*?)</[^>]+>',
                r'<[^>]+data-testid="[^"]*badge[^"]*"[^>]*>(.*?)</[^>]+>',
            ]
            for pat in badge_patterns:
                for m in re.finditer(pat, html_content, re.IGNORECASE | re.DOTALL):
                    inner = re.sub(r"<[^>]+>", " ", m.group(1))
                    inner = re.sub(r"\s+", " ", inner).strip()
                    if not inner or len(inner) > 200:
                        continue
                    badge_key = f"{host_id}|{inner}|html"
                    if badge_key in seen_keys:
                        continue
                    seen_keys.add(badge_key)
                    badges.append({
                        "badge_id": _sha256(badge_key),
                        "badge_name": inner,
                        "badge_text": inner,
                        "badge_category": None,
                        "badge_image_url": None,
                        "source_html_attribute": m.group(0)[:500],
                        "source_url": profile_url,
                        "collected_at": now,
                    })

        # Из Playwright
        if page is not None:
            try:
                for sel in PROFILE_SELECTORS["badges"]:
                    try:
                        els = page.locator(sel).all()
                        for el in els:
                            try:
                                txt = el.inner_text() or ""
                                if not txt or len(txt) > 200:
                                    continue
                                img = el.get_attribute("src") or el.get_attribute("data-src")
                                badge_key = f"{host_id}|{txt}|playwright"
                                if badge_key in seen_keys:
                                    continue
                                seen_keys.add(badge_key)
                                badges.append({
                                    "badge_id": _sha256(badge_key),
                                    "badge_name": txt,
                                    "badge_text": txt,
                                    "badge_category": None,
                                    "badge_image_url": img,
                                    "source_html_attribute": sel,
                                    "source_url": profile_url,
                                    "collected_at": now,
                                })
                            except Exception:
                                continue
                    except Exception:
                        continue
            except Exception:
                pass

        # Сохраняем бейджи в БД (INSERT OR IGNORE для предотвращения дублей)
        saved_count = 0
        with get_connection(self.db_file) as conn:
            cur = conn.cursor()
            for b in badges:
                try:
                    cur.execute(
                        """
                        INSERT INTO host_badges
                        (badge_id, host_id, badge_name, badge_text, badge_category,
                         badge_image_url, source_html_attribute, source_url, collected_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(host_id,badge_name,badge_text) DO UPDATE SET
                            badge_category=COALESCE(excluded.badge_category,host_badges.badge_category),
                            badge_image_url=COALESCE(excluded.badge_image_url,host_badges.badge_image_url),
                            source_html_attribute=COALESCE(excluded.source_html_attribute,host_badges.source_html_attribute),
                            source_url=COALESCE(excluded.source_url,host_badges.source_url),
                            collected_at=excluded.collected_at
                        """,
                        (
                            b["badge_id"], host_id, b["badge_name"], b["badge_text"],
                            b["badge_category"], b["badge_image_url"],
                            b["source_html_attribute"], b["source_url"], b["collected_at"],
                        ),
                    )
                    if cur.rowcount > 0:
                        saved_count += 1
                except Exception:
                    continue
            conn.commit()

        return {
            "status": COLLECTION_STATUS_SUCCESS if badges else COLLECTION_STATUS_PARTIAL,
            "badges_found": len(badges),
            "badges_saved": saved_count,
            "badges": badges,
        }

    # ------------------------------------------------------------------
    # 4. Отдельные секции профиля
    # ------------------------------------------------------------------

    def _collect_profile_sections(
        self,
        host_id: str,
        username: str,
        profile_url: Optional[str],
        html_content: Optional[str],
        page: Any,
        api_data: Optional[Dict[str, Any]],
        now: str,
    ) -> Dict[str, Any]:
        """
        Сбор отдельных секций профиля: About, Personal description, Interests,
        Languages, Occupation, Education, Location, Living situation, Additional info.
        Каждая секция сохраняется отдельно с статусом извлечения.
        """
        section_results: Dict[str, Any] = {}

        # Данные из api_data (уже структурированы)
        api_sections = {}
        if api_data:
            api_sections = {
                "about": api_data.get("aboutMe") or api_data.get("about_me") or api_data.get("about"),
                "house_rules": api_data.get("houseRules") or api_data.get("house_rules") or api_data.get("myHome") or api_data.get("my_home"),
                "personal_description": api_data.get("personalDescription") or api_data.get("personal_description"),
                "interests": json.dumps(api_data.get("interests") or [], ensure_ascii=False) if api_data.get("interests") else None,
                "languages": json.dumps(api_data.get("languages") or [], ensure_ascii=False) if api_data.get("languages") else None,
                "occupation": api_data.get("occupation") or api_data.get("job"),
                "education": api_data.get("education"),
                "location": api_data.get("locationSummary") or api_data.get("location"),
                "living_situation": api_data.get("livingSituation") or api_data.get("living_situation") or api_data.get("household"),
                "additional_information": api_data.get("additionalInformation") or api_data.get("additional_information"),
            }

        for section_name in PROFILE_SECTIONS:
            if section_name in ("shared_information", "why_on_couchsurfing"):
                continue  # Эти секции собираются отдельно

            section_text: Optional[str] = None
            extraction_status = EXTRACTION_STATUS_NOT_CHECKED
            source_url = profile_url
            language = None
            source = "not_checked"

            # Пытаемся из api_data
            if section_name in api_sections and api_sections[section_name]:
                val = api_sections[section_name]
                if isinstance(val, list):
                    section_text = ", ".join(str(v) for v in val)
                else:
                    section_text = str(val)
                extraction_status = EXTRACTION_STATUS_VALUE
                source = "network_api"

            # Пытаемся из HTML
            if section_text is None and html_content:
                try:
                    extracted = self._extract_section_from_html(html_content, section_name)
                    if extracted is not None:
                        section_text = extracted["text"]
                        extraction_status = extracted["status"]
                        source = "dom_parser"
                        language = extracted.get("language")
                except Exception as e:
                    extraction_status = EXTRACTION_STATUS_PARSE_FAILED
                    source = f"dom_parser_error:{e}"

            # Пытаемся из Playwright
            if section_text is None and page is not None:
                try:
                    extracted = self._extract_section_from_page(page, section_name)
                    if extracted is not None:
                        section_text = extracted["text"]
                        extraction_status = extracted["status"]
                        source = "playwright"
                except Exception:
                    extraction_status = EXTRACTION_STATUS_PARSE_FAILED
                    source = "playwright_error"

            if section_text is None and extraction_status == EXTRACTION_STATUS_NOT_CHECKED:
                extraction_status = EXTRACTION_STATUS_NOT_PRESENT if html_content is not None or page is not None else EXTRACTION_STATUS_NOT_CHECKED

            self._upsert_profile_section(
                host_id, section_name, section_text, language,
                extraction_status, source_url, now, extraction_source=source
            )

            section_results[section_name] = {
                "status": COLLECTION_STATUS_SUCCESS if section_text else COLLECTION_STATUS_PARTIAL,
                "extraction_status": extraction_status,
                "section_text": section_text,
                "source": source,
                "language": language,
            }

        return {
            "status": COLLECTION_STATUS_SUCCESS,
            "sections": section_results,
        }

    def _extract_section_from_html(self, html: str, section_name: str) -> Optional[Dict[str, Any]]:
        """Извлекает секцию профиля из HTML (эвристический парсинг)."""
        # Упрощённый извлекатель: ищет по ключевым словам в тексте
        text = re.sub(r"<[^>]+>", "\n", html)
        text = re.sub(r"\n\s*\n", "\n", text)
        text = re.sub(r"[ \t]+", " ", text)

        section_keywords = {
            "about": ["about", "about me", "о себе"],
            "house_rules": ["house rules", "my home", "правила дома"],
            "personal_description": ["personal description", "личное описание"],
            "interests": ["interests", "интересы", "hobbies", "хобби"],
            "languages": ["languages", "языки"],
            "occupation": ["occupation", "occupation", "profession", "profession"],
            "education": ["education", "образование"],
            "location": ["location", "местоположение"],
            "living_situation": ["living situation", "жилье", "жизненная ситуация"],
            "additional_information": ["additional information", "дополнительная информация"],
        }

        keywords = section_keywords.get(section_name, [section_name])
        for kw in keywords:
            idx = text.lower().find(kw.lower())
            if idx >= 0:
                # Извлекаем текст после ключевого слова (до следующего заголовка или 2000 символов)
                snippet = text[idx:idx + 2000]
                lines = [l.strip() for l in snippet.split("\n") if l.strip()]
                if len(lines) > 1:
                    return {
                        "text": "\n".join(lines[1:]),
                        "status": EXTRACTION_STATUS_VALUE,
                    }
                return {
                    "text": lines[0] if lines else "",
                    "status": EXTRACTION_STATUS_VALUE,
                }

        return None

    def _extract_section_from_page(self, page: Any, section_name: str) -> Optional[Dict[str, Any]]:
        """Извлекает секцию профиля через Playwright page."""
        try:
            sel_map = {
                "about": '[data-testid*="about"], [class*="about-section"]',
                "house_rules": '[data-testid*="house-rules"], [class*="house-rules"], [class*="house_rules"]',
                "interests": '[data-testid*="interest"], [class*="interest"]',
                "languages": '[data-testid*="language"], [class*="language"]',
                "occupation": '[data-testid*="occupation"], [class*="occupation"]',
                "education": '[data-testid*="education"], [class*="education"]',
                "location": '[data-testid*="location"], [class*="location"]',
                "living_situation": '[data-testid*="living"], [class*="living"]',
                "additional_information": '[data-testid*="additional"], [class*="additional"]',
                "shared_information": '[data-testid*="shared-information"], [class*="shared-information"], [class*="shared_info"]',
                "why_on_couchsurfing": '[data-testid*="why-on-couchsurfing"], [class*="why-on-couchsurfing"], [class*="why_couchsurfing"]',
            }
            sel = sel_map.get(section_name)
            if not sel:
                return None
            els = page.locator(sel).all()
            if not els:
                return None
            texts = []
            for el in els:
                try:
                    t = el.inner_text() or ""
                    if t.strip():
                        texts.append(t.strip())
                except Exception:
                    continue
            if texts:
                return {"text": "\n".join(texts), "status": EXTRACTION_STATUS_VALUE}
        except Exception:
            pass
        return None

    # ------------------------------------------------------------------
    # 5. Shared Information
    # ------------------------------------------------------------------

    def _collect_shared_information(
        self,
        host_id: str,
        username: str,
        profile_url: Optional[str],
        html_content: Optional[str],
        page: Any,
        api_data: Optional[Dict[str, Any]],
        now: str,
    ) -> Dict[str, Any]:
        """
        Сбор раздела Shared Information как самостоятельной секции.
        Отличает отсутствие раздела от ошибки парсинга.
        """
        section_text: Optional[str] = None
        extraction_status = EXTRACTION_STATUS_NOT_CHECKED
        source = "not_checked"
        error_details: Optional[str] = None

        # Из api_data
        if api_data:
            val = api_data.get("sharedInformation") or api_data.get("shared_information")
            if val:
                section_text = str(val) if not isinstance(val, str) else val
                extraction_status = EXTRACTION_STATUS_VALUE
                source = "network_api"

        # Из HTML
        if section_text is None and html_content:
            try:
                extracted = self._extract_section_from_html(html_content, "shared_information")
                if extracted:
                    section_text = extracted["text"]
                    extraction_status = extracted["status"]
                    source = "dom_parser"
            except Exception as e:
                extraction_status = EXTRACTION_STATUS_PARSE_FAILED
                error_details = str(e)
                source = "dom_parser_error"

        # Из Playwright
        if section_text is None and page is not None:
            try:
                extracted = self._extract_section_from_page(page, "shared_information")
                if extracted:
                    section_text = extracted["text"]
                    extraction_status = extracted["status"]
                    source = "playwright"
            except Exception as e:
                if extraction_status == EXTRACTION_STATUS_NOT_CHECKED:
                    extraction_status = EXTRACTION_STATUS_PARSE_FAILED
                    error_details = str(e)

        if section_text is None and extraction_status == EXTRACTION_STATUS_NOT_CHECKED:
            extraction_status = EXTRACTION_STATUS_NOT_PRESENT if html_content is not None or page is not None else EXTRACTION_STATUS_NOT_CHECKED

        self._upsert_profile_section(
            host_id, "shared_information", section_text, None,
            extraction_status, profile_url, now, extraction_source=source
        )

        return {
            "status": COLLECTION_STATUS_SUCCESS if section_text else COLLECTION_STATUS_PARTIAL,
            "extraction_status": extraction_status,
            "section_text": section_text,
            "source": source,
            "error_details": error_details,
        }

    # ------------------------------------------------------------------
    # 6. Why I'm on Couchsurfing
    # ------------------------------------------------------------------

    def _collect_why_on_couchsurfing(
        self,
        host_id: str,
        username: str,
        profile_url: Optional[str],
        html_content: Optional[str],
        page: Any,
        api_data: Optional[Dict[str, Any]],
        now: str,
    ) -> Dict[str, Any]:
        """
        Сбор раздела Why I'm on Couchsurfing как отдельной секции.
        Сохраняет исходный текст, язык, дату сбора, источник, статус извлечения.
        Не сокращает текст и не заменяет AI-резюме.
        """
        section_text: Optional[str] = None
        extraction_status = EXTRACTION_STATUS_NOT_CHECKED
        source = "not_checked"
        language = None
        error_details: Optional[str] = None

        # Из api_data
        if api_data:
            val = api_data.get("whyOnCouchsurfing") or api_data.get("why_on_couchsurfing") or api_data.get("why_i_am_on_couchsurfing")
            if val:
                section_text = str(val) if not isinstance(val, str) else val
                extraction_status = EXTRACTION_STATUS_VALUE
                source = "network_api"

        # Из HTML
        if section_text is None and html_content:
            try:
                extracted = self._extract_section_from_html(html_content, "why_on_couchsurfing")
                if extracted:
                    section_text = extracted["text"]
                    extraction_status = extracted["status"]
                    source = "dom_parser"
            except Exception as e:
                extraction_status = EXTRACTION_STATUS_PARSE_FAILED
                error_details = str(e)

        # Из Playwright
        if section_text is None and page is not None:
            try:
                extracted = self._extract_section_from_page(page, "why_on_couchsurfing")
                if extracted:
                    section_text = extracted["text"]
                    extraction_status = extracted["status"]
                    source = "playwright"
            except Exception as e:
                if extraction_status == EXTRACTION_STATUS_NOT_CHECKED:
                    extraction_status = EXTRACTION_STATUS_PARSE_FAILED
                    error_details = str(e)

        if section_text is None and extraction_status == EXTRACTION_STATUS_NOT_CHECKED:
            extraction_status = EXTRACTION_STATUS_NOT_PRESENT if html_content is not None or page is not None else EXTRACTION_STATUS_NOT_CHECKED

        self._upsert_profile_section(
            host_id, "why_on_couchsurfing", section_text, language,
            extraction_status, profile_url, now, extraction_source=source
        )

        return {
            "status": COLLECTION_STATUS_SUCCESS if section_text else COLLECTION_STATUS_PARTIAL,
            "extraction_status": extraction_status,
            "section_text": section_text,
            "language": language,
            "source": source,
            "error_details": error_details,
        }

    # ------------------------------------------------------------------
    # 7. References
    # ------------------------------------------------------------------

    def _collect_references(
        self,
        host_id: str,
        username: str,
        profile_url: Optional[str],
        html_content: Optional[str],
        page: Any,
        api_data: Optional[Dict[str, Any]],
        now: str,
    ) -> Dict[str, Any]:
        """
        Полноценный сбор доступных References.
        Для каждого Reference сохраняет: ID, текст, автора, ссылку на профиль автора,
        дату, тип, направление отношения, источник, дату сбора.
        Отдельно сохраняет статистику профиля.
        Не считает количество References доказательством приёма гостей.
        """
        references: List[Dict[str, Any]] = []
        seen_ref_ids: set = set()

        # Из api_data
        if api_data:
            raw_refs = api_data.get("references") or api_data.get("reviews") or []
            if isinstance(raw_refs, list):
                for r in raw_refs:
                    if not isinstance(r, dict):
                        continue
                    ref_id = r.get("id") or r.get("referenceId") or _sha256(
                        f"{host_id}|{r.get('text','')}|{r.get('author','')}|{r.get('date','')}"
                    )
                    if ref_id in seen_ref_ids:
                        continue
                    seen_ref_ids.add(ref_id)
                    references.append({
                        "reference_id": ref_id,
                        "reference_text": r.get("text") or r.get("body") or r.get("content") or "",
                        "author_username": r.get("author") or r.get("authorUsername") or r.get("reviewer"),
                        "author_profile_url": r.get("authorProfileUrl") or r.get("author_url"),
                        "reference_date": r.get("date") or r.get("createdAt") or r.get("reference_date"),
                        "reference_type": r.get("type") or r.get("reference_type"),
                        "relationship_direction": r.get("direction") or r.get("relationship_direction"),
                        "source_url": profile_url,
                        "collection_source": r.get("_collection_source") or "network_api",
                        "collected_at": now,
                    })

        # Из HTML (эвристический поиск блоков отзывов)
        if html_content:
            # Ищем паттерны отзывов
            ref_blocks = re.finditer(
                r'<[^>]+(?:class|data-testid)="[^"]*(?:reference|review|testimonial)[^"]*"[^>]*>.*?</[^>]+>',
                html_content, re.IGNORECASE | re.DOTALL
            )
            for m in ref_blocks:
                block = m.group(0)
                inner = re.sub(r"<[^>]+>", " ", block)
                inner = re.sub(r"\s+", " ", inner).strip()
                if len(inner) < 10:
                    continue
                ref_id = _sha256(f"{host_id}|{inner}|html")
                if ref_id in seen_ref_ids:
                    continue
                seen_ref_ids.add(ref_id)
                references.append({
                    "reference_id": ref_id,
                    "reference_text": inner[:2000],
                    "author_username": None,
                    "author_profile_url": None,
                    "reference_date": None,
                    "reference_type": None,
                    "relationship_direction": None,
                    "source_url": profile_url,
                    "collection_source": "profile_html",
                    "collected_at": now,
                })

        # Сохраняем references в БД
        saved_count = 0
        with get_connection(self.db_file) as conn:
            cur = conn.cursor()
            for r in references:
                try:
                    cur.execute(
                        """
                        INSERT INTO host_references
                        (reference_id, host_id, reference_text, author_username,
                         author_profile_url, reference_date, reference_type,
                         relationship_direction, source_url, collected_at, collection_source)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(reference_id) DO UPDATE SET
                            reference_text=COALESCE(excluded.reference_text,host_references.reference_text),
                            author_username=COALESCE(excluded.author_username,host_references.author_username),
                            author_profile_url=COALESCE(excluded.author_profile_url,host_references.author_profile_url),
                            reference_date=COALESCE(excluded.reference_date,host_references.reference_date),
                            reference_type=COALESCE(excluded.reference_type,host_references.reference_type),
                            relationship_direction=COALESCE(excluded.relationship_direction,host_references.relationship_direction),
                            source_url=COALESCE(excluded.source_url,host_references.source_url),
                            collection_source=COALESCE(excluded.collection_source,host_references.collection_source),
                            collected_at=excluded.collected_at
                        """,
                        (
                            r["reference_id"], host_id, r["reference_text"],
                            r["author_username"], r["author_profile_url"],
                            r["reference_date"], r["reference_type"],
                            r["relationship_direction"], r["source_url"], r["collected_at"],
                            r.get("collection_source"),
                        ),
                    )
                    if cur.rowcount > 0:
                        saved_count += 1
                except Exception:
                    continue

            # Обновляем статистику
            reported_total = 0
            if api_data:
                try:
                    reported_total = int(
                        api_data.get("totalReferencesCount")
                        or api_data.get("references_count")
                        or api_data.get("referencesCount")
                        or 0
                    )
                except (TypeError, ValueError):
                    reported_total = 0
            total = max(len(references), reported_total)
            positive = sum(1 for r in references if r.get("reference_type") and "positive" in str(r["reference_type"]).lower())
            negative = sum(1 for r in references if r.get("reference_type") and "negative" in str(r["reference_type"]).lower())
            neutral = max(0, len(references) - positive - negative)
            stored_positive = positive if references else None
            stored_negative = negative if references else None
            stored_neutral = neutral if references else None

            cur.execute(
                """
                INSERT INTO host_reference_stats
                (host_id, total_references, positive_references, neutral_references,
                 negative_references, other_categories, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(host_id) DO UPDATE SET
                    total_references=CASE WHEN excluded.total_references > 0 THEN excluded.total_references ELSE host_reference_stats.total_references END,
                    positive_references=COALESCE(excluded.positive_references,host_reference_stats.positive_references),
                    neutral_references=COALESCE(excluded.neutral_references,host_reference_stats.neutral_references),
                    negative_references=COALESCE(excluded.negative_references,host_reference_stats.negative_references),
                    other_categories=COALESCE(excluded.other_categories,host_reference_stats.other_categories),
                    updated_at=excluded.updated_at
                """,
                (
                    host_id, total, stored_positive, stored_neutral, stored_negative,
                    json.dumps({}, ensure_ascii=False), now,
                ),
            )
            conn.commit()

        return {
            "status": COLLECTION_STATUS_PARTIAL if reported_total > len(references) else (COLLECTION_STATUS_SUCCESS if references or reported_total else COLLECTION_STATUS_PARTIAL),
            "references_found": len(references),
            "references_saved": saved_count,
            "total_references": total,
            "positive_references": stored_positive,
            "neutral_references": stored_neutral,
            "negative_references": stored_negative,
            "references": references,
        }

    # ------------------------------------------------------------------
    # 8. Gender evidence
    # ------------------------------------------------------------------

    def _collect_gender_evidence(
        self,
        host_id: str,
        username: str,
        html_content: Optional[str],
        page: Any,
        api_data: Optional[Dict[str, Any]],
        now: str,
    ) -> Dict[str, Any]:
        """
        Сбор доказательств для определения пола несколькими независимыми методами.
        1. Явно указанное поле gender.
        2. Самоопределение в тексте профиля.
        3. Местоимения.
        4. Языковые признаки.
        5. Данные из References.
        Сохраняет источник, исходный фрагмент, тип признака, уверенность, противоречия.
        Не определяет пол только по фотографии или имени.
        """
        evidence_items: List[Dict[str, Any]] = []

        # Явное поле gender (из api_data)
        if api_data:
            explicit_gender = api_data.get("gender") or api_data.get("explicit_gender")
            if explicit_gender:
                g = str(explicit_gender).strip().lower()
                inferred = GENDER_INFERRED_MALE if g in ("male", "m") else (GENDER_INFERRED_FEMALE if g in ("female", "f") else GENDER_INFERRED_UNKNOWN)
                if inferred != GENDER_INFERRED_UNKNOWN:
                    evidence_items.append({
                        "evidence_type": "explicit_field",
                        "evidence_source": "network_api",
                        "evidence_fragment": str(explicit_gender),
                        "inferred_gender": inferred,
                        "confidence": 0.9,
                    })

        # Самоопределение в тексте профиля (из HTML или api_data)
        bio_text = None
        if api_data:
            bio_text = api_data.get("aboutMe") or api_data.get("about_me") or api_data.get("about")
        if bio_text is None and html_content:
            bio_text = re.sub(r"<[^>]+>", " ", html_content)
            bio_text = re.sub(r"\s+", " ", bio_text).strip()

        if bio_text:
            # Маркеры自我идентификации
            male_patterns = [
                r"\b(as\s+a\s+(?:guy|man|male|dude))\b",
                r"\b(i['’]?m\s+(?:a\s+)?(?:guy|man|male|dude))\b",
                r"\b(he/him|him/he)\b",
                r"\b(my\s+girlfriend|my\s+wife)\b",
            ]
            female_patterns = [
                r"\b(as\s+a\s+(?:girl|woman|female|lady))\b",
                r"\b(i['’]?m\s+(?:a\s+)?(?:woman|girl|female|lady))\b",
                r"\b(she/her|her/she)\b",
                r"\b(my\s+boyfriend|my\s+husband)\b",
            ]

            for pat in male_patterns:
                m = re.search(pat, bio_text, re.IGNORECASE)
                if m:
                    evidence_items.append({
                        "evidence_type": "self_identification",
                        "evidence_source": "profile_bio",
                        "evidence_fragment": m.group(0)[:200],
                        "inferred_gender": GENDER_INFERRED_MALE,
                        "confidence": 0.8,
                    })
                    break

            for pat in female_patterns:
                m = re.search(pat, bio_text, re.IGNORECASE)
                if m:
                    evidence_items.append({
                        "evidence_type": "self_identification",
                        "evidence_source": "profile_bio",
                        "evidence_fragment": m.group(0)[:200],
                        "inferred_gender": GENDER_INFERRED_FEMALE,
                        "confidence": 0.8,
                    })
                    break

            # Местоимения в био
            male_pronouns = len(re.findall(r"\b(he|him|his)\b", bio_text, re.IGNORECASE))
            female_pronouns = len(re.findall(r"\b(she|her|hers)\b", bio_text, re.IGNORECASE))
            if male_pronouns > 0 and female_pronouns == 0:
                evidence_items.append({
                    "evidence_type": "pronoun",
                    "evidence_source": "profile_bio",
                    "evidence_fragment": f"male pronouns: {male_pronouns}",
                    "inferred_gender": GENDER_INFERRED_MALE,
                    "confidence": min(0.6, 0.3 + male_pronouns * 0.1),
                })
            elif female_pronouns > 0 and male_pronouns == 0:
                evidence_items.append({
                    "evidence_type": "pronoun",
                    "evidence_source": "profile_bio",
                    "evidence_fragment": f"female pronouns: {female_pronouns}",
                    "inferred_gender": GENDER_INFERRED_FEMALE,
                    "confidence": min(0.6, 0.3 + female_pronouns * 0.1),
                })

        # Языковые признаки (японские суффиксы имени)
        if username:
            first = username.lower().split()[0] if username.split() else username.lower()
            if first.endswith("ko") and not first.endswith("hiko"):
                evidence_items.append({
                    "evidence_type": "language_marker",
                    "evidence_source": "username_linguistics",
                    "evidence_fragment": f"username suffix -ko: {first}",
                    "inferred_gender": GENDER_INFERRED_FEMALE,
                    "confidence": 0.4,
                })
            elif first.endswith(("suke", "taro", "kazu", "to", "ro", "ji", "ichi")):
                evidence_items.append({
                    "evidence_type": "language_marker",
                    "evidence_source": "username_linguistics",
                    "evidence_fragment": f"username suffix male: {first}",
                    "inferred_gender": GENDER_INFERRED_MALE,
                    "confidence": 0.4,
                })

        # Сохраняем 증거 в БД
        saved_count = 0
        with get_connection(self.db_file) as conn:
            cur = conn.cursor()
            for e in evidence_items:
                evidence_id = _sha256(f"{host_id}|{e['evidence_type']}|{e['evidence_fragment']}")
                try:
                    cur.execute(
                        """
                        INSERT OR IGNORE INTO host_gender_evidence
                        (evidence_id, host_id, evidence_type, evidence_source,
                         evidence_fragment, inferred_gender, confidence, collected_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            evidence_id, host_id, e["evidence_type"], e["evidence_source"],
                            e["evidence_fragment"], e["inferred_gender"], e["confidence"], now,
                        ),
                    )
                    if cur.rowcount > 0:
                        saved_count += 1
                except Exception:
                    continue
            conn.commit()

        # Определяем противоречия
        genders = [e["inferred_gender"] for e in evidence_items if e["inferred_gender"] != GENDER_INFERRED_UNKNOWN]
        has_conflict = GENDER_INFERRED_MALE in genders and GENDER_INFERRED_FEMALE in genders

        return {
            "status": COLLECTION_STATUS_SUCCESS if evidence_items else COLLECTION_STATUS_PARTIAL,
            "evidence_count": len(evidence_items),
            "evidence_saved": saved_count,
            "has_conflict": has_conflict,
            "evidence": evidence_items,
        }

    # ------------------------------------------------------------------
    # Вспомогательные методты upsert
    # ------------------------------------------------------------------

    def _upsert_host_profile(
        self,
        host_id: str,
        username: str,
        profile_url: Optional[str],
        now: str,
    ) -> None:
        """Создаёт или обновляет основную запись профиля (без перезаписи существующих данных)."""
        with get_connection(self.db_file) as conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO host_profiles
                (host_id, username, profile_url, first_collected_at,
                 last_collection_attempt, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(host_id) DO UPDATE SET
                    username = COALESCE(excluded.username, host_profiles.username),
                    profile_url = COALESCE(excluded.profile_url, host_profiles.profile_url),
                    last_collection_attempt = excluded.last_collection_attempt,
                    updated_at = excluded.updated_at
                """,
                (host_id, username, profile_url, now, now, now, now),
            )
            conn.commit()

    def _upsert_verification(
        self,
        host_id: str,
        status: str,
        source: Optional[str],
        evidence: Optional[str],
        now: str,
    ) -> None:
        # Не перезаписываем существующий статус, если нет новых данных
        if status == VERIFICATION_STATUS_UNKNOWN:
            with get_connection(self.db_file) as conn:
                cur = conn.cursor()
                cur.execute(
                    "SELECT verification_status FROM host_profiles WHERE host_id = ?",
                    (host_id,),
                )
                row = cur.fetchone()
                if row and str(row[0]) != VERIFICATION_STATUS_UNKNOWN:
                    return  # Сохраняем существующий статус
        with get_connection(self.db_file) as conn:
            cur = conn.cursor()
            cur.execute(
                """
                UPDATE host_profiles
                SET verification_status = ?,
                    verification_source = COALESCE(?, verification_source),
                    verification_evidence = COALESCE(?, verification_evidence),
                    verification_checked_at = ?,
                    updated_at = ?
                WHERE host_id = ?
                """,
                (status, source, evidence, now, now, host_id),
            )
            conn.commit()

    def _upsert_recently_active(
        self,
        host_id: str,
        status: str,
        raw_value: Optional[str],
        normalized: Optional[str],
        last_active_url: Optional[str],
        now: str,
    ) -> None:
        with get_connection(self.db_file) as conn:
            cur = conn.cursor()
            cur.execute(
                """
                UPDATE host_profiles
                SET recently_active_status = CASE WHEN ? = 'UNKNOWN' THEN recently_active_status ELSE ? END,
                    recently_active_raw = COALESCE(?, recently_active_raw),
                    recently_active_normalized = COALESCE(?, recently_active_normalized),
                    last_active_url = COALESCE(?, last_active_url),
                    recently_active_checked_at = ?,
                    last_seen_update = ?,
                    updated_at = ?
                WHERE host_id = ?
                """,
                (status, status, raw_value, normalized, last_active_url, now, now, now, host_id),
            )
            conn.commit()

    def _upsert_profile_section(
        self,
        host_id: str,
        section_name: str,
        section_text: Optional[str],
        language: Optional[str],
        extraction_status: str,
        source_url: Optional[str],
        now: str,
        extraction_source: Optional[str] = None,
    ) -> None:
        section_id = _sha256(f"{host_id}|{section_name}")
        with get_connection(self.db_file) as conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO host_profile_sections
                (section_id, host_id, section_name, section_text, language,
                 extraction_status, source_url, collected_at, extraction_source, value_collected_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(host_id, section_name) DO UPDATE SET
                    section_text = COALESCE(excluded.section_text, host_profile_sections.section_text),
                    language = COALESCE(excluded.language, host_profile_sections.language),
                    extraction_status = excluded.extraction_status,
                    source_url = COALESCE(excluded.source_url, host_profile_sections.source_url),
                    collected_at = excluded.collected_at,
                    extraction_source = CASE WHEN excluded.section_text IS NOT NULL OR host_profile_sections.extraction_source IS NULL
                                             THEN excluded.extraction_source ELSE host_profile_sections.extraction_source END,
                    value_collected_at = CASE WHEN excluded.section_text IS NOT NULL
                                              THEN excluded.value_collected_at ELSE host_profile_sections.value_collected_at END
                """,
                (section_id, host_id, section_name, section_text, language,
                 extraction_status, source_url, now, extraction_source, now if section_text is not None else None),
            )
            conn.commit()

    def _finalize_host_profile(
        self,
        host_id: str,
        username: str,
        results: Dict[str, Any],
        now: str,
    ) -> None:
        """Обновляет поля полноты и времени последнего успешного сбора."""
        sections = results.get("sections", {})
        collected = 0
        total = len(PROFILE_SECTIONS) + 3  # + verification, recently_active, badges
        for s in sections.values():
            if s.get("status") == COLLECTION_STATUS_SUCCESS:
                collected += 1

        pct = round((collected / total) * 100.0, 1) if total > 0 else 0.0

        section_names = []
        for s in ("about", "personal_description", "interests", "languages", "occupation",
                  "education", "location", "living_situation", "additional_information"):
            sec = sections.get("profile_sections", {}).get("sections", {}).get(s)
            if sec and sec.get("extraction_status") == EXTRACTION_STATUS_VALUE:
                section_names.append(s)
        if sections.get("shared_information", {}).get("extraction_status") == EXTRACTION_STATUS_VALUE:
            section_names.append("shared_information")
        if sections.get("why_on_couchsurfing", {}).get("extraction_status") == EXTRACTION_STATUS_VALUE:
            section_names.append("why_on_couchsurfing")

        with get_connection(self.db_file) as conn:
            cur = conn.cursor()
            cur.execute(
                """
                UPDATE host_profiles
                SET profile_completeness_pct = ?,
                    sections_collected = ?,
                    last_successful_collection = CASE WHEN ? = 'SUCCESS' THEN ? ELSE last_successful_collection END,
                    last_collection_attempt = ?,
                    updated_at = ?
                WHERE host_id = ?
                """,
                (pct, json.dumps(section_names, ensure_ascii=False), results.get("overall_status"), now, now, now, host_id),
            )
            conn.commit()

    def _log_collection(
        self,
        host_id: str,
        section_name: Optional[str],
        status: str,
        error_details: Optional[str],
        now: str,
    ) -> None:
        log_id = _sha256(f"{host_id}|{section_name}|{now}")
        with get_connection(self.db_file) as conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO host_profile_collection_log
                (log_id, host_id, section_name, status, error_details, collected_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (log_id, host_id, section_name, status, error_details, now),
            )
            conn.commit()


# ---------------------------------------------------------------------------
# Фасад-функции для обратной совместимости
# ---------------------------------------------------------------------------

def get_profile_intelligence_engine(
    db_file: Optional[Path | str] = None,
) -> ProfileIntelligenceEngine:
    """Создаёт новый экземпляр ProfileIntelligenceEngine (без кэширования db_file)."""
    return ProfileIntelligenceEngine(db_file=db_file)


def collect_host_profile(
    host_id: str,
    username: str,
    profile_url: Optional[str] = None,
    html_content: Optional[str] = None,
    page: Any = None,
    api_data: Optional[Dict[str, Any]] = None,
    profile_page_status: Optional[str] = None,
    db_file: Optional[Path | str] = None,
) -> Dict[str, Any]:
    """Удобная функция для сбора профиля хоста."""
    engine = ProfileIntelligenceEngine(db_file=db_file)
    return engine.collect_host_profile(
        host_id=host_id,
        username=username,
        profile_url=profile_url,
        html_content=html_content,
        page=page,
        api_data=api_data,
        profile_page_status=profile_page_status,
    )
