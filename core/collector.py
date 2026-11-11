"""
core/collector.py - Центральный модуль КОЛЛЕКТОР.
Отвечает за:
1. Сохранение сырых профилей в SQLite (с неизменяемым Snapshot).
2. Дедупликацию по (trip_id, host_id / username).
3. Скоринг (1-5★ Match) на основе реальных пересечений с CONTEXT гостя.
4. Управление переходами состояний (State Machine).
"""

import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from core.context import GUEST
from core.database import get_connection, log_audit
from core.models import HostCard, HostState

logger = logging.getLogger("Collector")


def calculate_match_score(host_data: Dict[str, Any]) -> Tuple[int, List[str]]:
    """
    Рассчитывает скоринг соответствия профиля хоста контексту гостя (1-5 звезд)
    и формирует список причин оценки.
    """
    score = 1
    reasons = []

    # 1. Текстовое пространство хоста (о себе, интересы, книги, фильмы, страны)
    about = str(host_data.get("about") or host_data.get("about_me") or "").lower()
    interests = [str(x).lower() for x in (host_data.get("interests") or [])]
    languages = [str(x).lower() for x in (host_data.get("languages") or [])]
    countries = [str(x).lower() for x in (host_data.get("countries_visited") or [])]
    
    full_text = " ".join([about] + interests + countries)

    # 2. Проверка пересечений по ключевым сферам гостя
    # Горы / Лыжи / Outdoor
    outdoor_matches = [k for k in GUEST.interests["outdoors"] if k in full_text]
    if outdoor_matches:
        score += 1
        reasons.append(f"Горы / Лыжи / Экстрим ({', '.join(outdoor_matches[:2])})")

    # Кино / Фотография / Искусство
    art_matches = [k for k in GUEST.interests["arts_media"] if k in full_text]
    if art_matches:
        score += 1
        reasons.append(f"Кино / Фото / Арт ({', '.join(art_matches[:2])})")

    # Музыка / Пианино
    music_matches = [k for k in GUEST.interests["music"] if k in full_text]
    if music_matches:
        score += 1
        reasons.append(f"Музыка / Классика / Фортепиано ({', '.join(music_matches[:2])})")

    # Языковая совместимость (Французский = особый плюс для Женевы)
    french_found = any("french" in l or "français" in l for l in languages) or "french" in full_text
    if french_found:
        if "Французский язык" not in reasons:
            score += 1
            reasons.append("Владеет французским языком")

    # Проверка наполненности анкеты
    has_meaningful_data = bool(interests or languages or (about and len(about.split()) > 10))
    if not has_meaningful_data:
        return 1, ["НЕТ ДАННЫХ (анкета не заполнена или отсутствует информация)"]

    # Ограничение максимума в 5 звезд
    score = min(5, max(1, score))
    if not reasons:
        reasons.append("Общий интерес к путешествиям (нет специфических совпадений)")

    return score, reasons


class CollectorEngine:
    """Движок коллектора данных."""

    def __init__(self, trip_id: str, db_file: Optional[Path] = None):
        self.trip_id = trip_id
        self.db_file = db_file

    def ingest_raw_host(self, raw_host: Dict[str, Any]) -> Optional[HostCard]:
        """
        Принимает сырой профиль из поиска:
        1. Проверяет дубликаты.
        2. Считает скоринг (1-5★).
        3. Сохраняет снимок и карточку в SQLite.
        """
        host_id = str(raw_host.get("id") or raw_host.get("public_address") or raw_host.get("username") or "").strip()
        username = str(raw_host.get("username") or raw_host.get("public_address") or host_id).strip()
        if not host_id or not username:
            return None

        now_iso = datetime.now().isoformat()

        # Проверка на существование в базе
        with get_connection(self.db_file) as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT state, score_stars FROM core_hosts WHERE trip_id = ? AND (host_id = ? OR username = ?)",
                (self.trip_id, host_id, username)
            )
            existing = cur.fetchone()
            if existing:
                # Уже сохранен — не перезаписываем состояние
                return None

        # Расчет рейтинга 1-5 звезд
        score_stars, score_reasons = calculate_match_score(raw_host)

        name = (raw_host.get("name") or raw_host.get("first_name") or username).strip()
        location = (raw_host.get("location") or raw_host.get("city") or "").strip()
        
        init_state = HostState.DISCOVERED
        if raw_host.get("is_hosting") == "not_accepting":
            init_state = HostState.SKIPPED
            score_reasons.append("Не принимает гостей / в поездке")

        card = HostCard(
            host_id=host_id,
            username=username,
            trip_id=self.trip_id,
            name=name,
            location=location,
            gender=raw_host.get("gender"),
            age=raw_host.get("age"),
            occupation=raw_host.get("occupation"),
            about=raw_host.get("about") or raw_host.get("about_me"),
            interests=raw_host.get("interests") or [],
            languages=raw_host.get("languages") or [],
            countries_visited=raw_host.get("countries_visited") or [],
            countries_lived=raw_host.get("countries_lived") or [],
            profile_url=raw_host.get("profile_url") or f"https://www.couchsurfing.com/c/users/{username}",
            avatar_url=raw_host.get("avatar_url") or raw_host.get("profile_photo_url"),
            is_verified=bool(raw_host.get("is_verified") or raw_host.get("isIdentificationVerified")),
            birth_country=raw_host.get("birth_country"),
            residence_country=raw_host.get("residence_country"),
            is_hosting=raw_host.get("is_hosting", "unknown"),
            household=raw_host.get("household"),
            special_notes=raw_host.get("special_notes"),
            score_stars=score_stars,
            score_reasons=score_reasons,
            state=init_state,
            created_at=now_iso,
            updated_at=now_iso,
        )

        with get_connection(self.db_file) as conn:
            # 1. Сохранение сырого снимка
            conn.execute(
                """
                INSERT OR REPLACE INTO core_host_snapshots (host_id, trip_id, raw_data_json, captured_at)
                VALUES (?, ?, ?, ?);
                """,
                (host_id, self.trip_id, json.dumps(raw_host, ensure_ascii=False), now_iso)
            )
            # 2. Сохранение карточки
            conn.execute(
                """
                INSERT INTO core_hosts (
                    host_id, trip_id, username, name, location, gender, age, occupation,
                    about, interests_json, languages_json, countries_json, profile_url,
                    avatar_url, is_verified, birth_country, residence_country, is_hosting,
                    household, special_notes, countries_lived_json,
                    score_stars, score_reasons_json, state, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    card.host_id, card.trip_id, card.username, card.name, card.location,
                    card.gender, card.age, card.occupation, card.about,
                    json.dumps(card.interests, ensure_ascii=False),
                    json.dumps(card.languages, ensure_ascii=False),
                    json.dumps(card.countries_visited, ensure_ascii=False),
                    card.profile_url, card.avatar_url, 1 if card.is_verified else 0,
                    card.birth_country, card.residence_country, card.is_hosting,
                    card.household, card.special_notes,
                    json.dumps(card.countries_lived, ensure_ascii=False),
                    card.score_stars, json.dumps(card.score_reasons, ensure_ascii=False),
                    card.state.value, card.created_at, card.updated_at
                )
            )

        log_audit(
            trip_id=self.trip_id,
            module="COLLECTOR",
            event="HOST_INGESTED",
            host_id=host_id,
            details={"username": username, "score_stars": score_stars, "reasons": score_reasons},
            db_file=self.db_file
        )
        return card

    def ingest_batch(self, raw_hosts: List[Dict[str, Any]]) -> Dict[str, int]:
        """Пакетный прием списка хостов."""
        added = 0
        skipped = 0
        for h in raw_hosts:
            card = self.ingest_raw_host(h)
            if card:
                added += 1
            else:
                skipped += 1
        return {"added": added, "skipped": skipped, "total": len(raw_hosts)}
