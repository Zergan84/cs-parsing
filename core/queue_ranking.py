"""Deterministic, explainable host ranking used by queue display and dispatch."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping


_TOKEN_RE = re.compile(r"[a-zа-яё]{3,}", re.IGNORECASE)
_STOP_WORDS = {
    "the", "and", "for", "with", "from", "that", "this", "you", "your", "host", "guest",
    "travel", "travels", "travelling", "traveler", "traveling", "love", "enjoy", "like",
    "мне", "мой", "моя", "мои", "это", "как", "для", "что", "его", "она", "они", "люблю",
}
_RUSSIAN_FIRST_NAMES = {
    "alexander", "aleksandr", "andrey", "andrei", "dmitry", "dmitri", "sergey", "sergei",
    "mikhail", "nikolay", "nikolai", "maria", "mariya", "elena", "anastasia", "ekaterina",
    "natalia", "nataliya", "olga", "irina", "ivan", "pavel", "anna", "yulia", "julia",
}
_RUSSIAN_WORDS = {
    "я", "мы", "мой", "моя", "мои", "привет", "русский", "русскийязык", "говорю", "говорим",
    "люблю", "живу", "работаю", "путешествую", "гостей", "принимаю", "добро", "пожалуйста",
}
_LATIN_SURNAME_ENDINGS = ("ov", "ova", "ev", "eva", "sky", "skaya", "in", "ina")
_RUSSIAN_LANG_MARKERS = ("russian", "русский", "русская", "русский язык", "language: ru", "lang: ru")


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
            if isinstance(decoded, list):
                return decoded
        except (TypeError, ValueError):
            pass
        return [part.strip() for part in re.split(r"[,;|]", value) if part.strip()]
    return []


def _snapshot(host: Mapping[str, Any]) -> dict[str, Any]:
    value = host.get("profile_snapshot_json") or host.get("profile_snapshot") or {}
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if not isinstance(parsed, dict):
                return {}
            nested = parsed.get("profile") or parsed.get("host")
            return {**parsed, **nested} if isinstance(nested, dict) else parsed
        except (TypeError, ValueError):
            return {}
    return {}


def _review_count(host: Mapping[str, Any], snapshot: Mapping[str, Any]) -> int | None:
    # Deliberately do not use references_count/totalReferencesCount: those fields
    # are not a reliable, distinct review-count signal in the current model.
    for source in (host, snapshot):
        for key in ("review_count", "reviews_count", "reviewsCount", "numberOfReviews", "totalReviewsCount"):
            value = source.get(key)
            if isinstance(value, bool):
                continue
            try:
                count = int(value)
                if count >= 0:
                    return count
            except (TypeError, ValueError):
                continue
        reviews = source.get("reviews")
        if isinstance(reviews, list):
            return len(reviews)
    return None


def _activity_bucket(host: Mapping[str, Any]) -> tuple[int, int | None, str]:
    status = str(host.get("recently_active_status") or "UNKNOWN").strip().upper()
    checked = host.get("activity_checked_at")
    if status in {"ACTIVE", "RECENTLY_ACTIVE"} and checked:
        try:
            value = datetime.fromisoformat(str(checked).replace("Z", "+00:00"))
            if value.tzinfo is None:
                value = value.replace(tzinfo=datetime.now().astimezone().tzinfo)
            age_days = (datetime.now(value.tzinfo) - value).total_seconds() / 86400
            if age_days > 30:
                status = "UNKNOWN"
        except (TypeError, ValueError):
            status = "UNKNOWN"
    if status == "RECENTLY_ACTIVE":
        return 3, _int_or_none(host.get("last_login_days")), "RECENTLY_ACTIVE"
    if status == "ACTIVE":
        return 2, _int_or_none(host.get("last_login_days")), "ACTIVE"
    if status in {"NOT_RECENTLY_ACTIVE", "INACTIVE"}:
        return 1, _int_or_none(host.get("last_login_days")), "INACTIVE"
    return 0, None, "UNKNOWN"


def _int_or_none(value: Any) -> int | None:
    try:
        return max(0, int(value)) if value is not None else None
    except (TypeError, ValueError):
        return None


def _russian_name_signal(host: Mapping[str, Any]) -> tuple[bool, int, str | None]:
    name = str(host.get("clean_name") or host.get("name") or "").strip()
    if not name:
        return False, 0, None
    if re.search(r"[А-Яа-яЁё]", name):
        return True, 2, "RUSSIAN NAME — CYRILLIC"
    tokens = [token.lower() for token in re.findall(r"[A-Za-z]+", name)]
    if any(token in _RUSSIAN_FIRST_NAMES for token in tokens):
        return True, 2, "RUSSIAN NAME — PROBABLE TRANSLITERATION"
    if any(token.endswith(_LATIN_SURNAME_ENDINGS) and len(token) >= 4 for token in tokens):
        return True, 1, "RUSSIAN NAME — WEAK SURNAME SIGNAL"
    return False, 0, None


def _language_match(host: Mapping[str, Any], snapshot: Mapping[str, Any]) -> bool:
    languages = _as_list(host.get("language_list") or host.get("languages") or snapshot.get("languages"))
    if any(str(language).strip().casefold() in {"ru", "russian", "русский", "русский язык"} for language in languages):
        return True
    for field in ("about_me", "about_me_clean", "about", "description", "bio", "shared_information", "why_couchsurfing"):
        value = str(host.get(field) or snapshot.get(field) or "").casefold()
        if any(marker in value for marker in _RUSSIAN_LANG_MARKERS):
            return True
        words = set(_TOKEN_RE.findall(value))
        cyrillic_count = len(re.findall(r"[А-Яа-яЁё]", value))
        if cyrillic_count >= 24 and len(words.intersection(_RUSSIAN_WORDS)) >= 2:
            return True
    return False


def _interest_match(host: Mapping[str, Any], snapshot: Mapping[str, Any], traveler_interests: Iterable[str]) -> bool:
    host_interests = _as_list(host.get("interests") or snapshot.get("interests"))
    if not host_interests:
        return False
    host_terms = {
        token.casefold() for value in host_interests for token in _TOKEN_RE.findall(str(value))
        if token.casefold() not in _STOP_WORDS
    }
    traveler_terms = {
        token.casefold() for value in traveler_interests for token in _TOKEN_RE.findall(str(value))
        if token.casefold() not in _STOP_WORDS
    }
    return bool(host_terms & traveler_terms)


def rank_host(host: Mapping[str, Any], traveler_interests: Iterable[str] = ()) -> dict[str, Any]:
    """Return public ranking details plus a deterministic lexicographic sort key."""
    snapshot = _snapshot(host)
    verification = str(host.get("verification_status") or "UNKNOWN").strip().upper()
    verified = verification == "VERIFIED"
    activity_rank, age_days, activity_status = _activity_bucket(host)
    review_count = _review_count(host, snapshot)
    review_tier = 2 if review_count is not None and review_count > 100 else 1 if review_count is not None and review_count > 10 else 0
    interest_match = _interest_match(host, snapshot, traveler_interests)
    language_match = _language_match(host, snapshot)
    name_match, name_strength, name_reason = _russian_name_signal(host)

    reasons: list[str] = []
    if verified:
        reasons.append("VERIFIED ACCOUNT")
    if activity_status == "RECENTLY_ACTIVE":
        reasons.append("RECENTLY ACTIVE")
    elif activity_status == "ACTIVE":
        reasons.append("ACTIVE")
    elif activity_status == "INACTIVE":
        reasons.append("INACTIVE")
    if review_count is not None and review_count > 100:
        reasons.append(f"{review_count} REVIEWS")
    elif review_count is not None and review_count > 10:
        reasons.append(f"{review_count} REVIEWS")
    if interest_match:
        reasons.append("MATCHING INTERESTS")
    if language_match:
        reasons.append("RUSSIAN LANGUAGE")
    if name_reason:
        reasons.append(name_reason)
    if not reasons:
        reasons.append("NO STRONG RANKING SIGNALS")

    present = 0
    present += int(bool(str(host.get("verification_status") or "").strip()))
    present += int(bool(str(host.get("recently_active_status") or "").strip()))
    present += int(review_count is not None)
    present += int(bool(_as_list(host.get("interests") or snapshot.get("interests"))))
    present += int(bool(_as_list(host.get("language_list") or host.get("languages") or snapshot.get("languages"))) or bool(host.get("about_me") or snapshot.get("about")))
    present += int(bool(host.get("name") or host.get("clean_name")))
    confidence = round(present / 6, 2)

    score = (
        int(verified) * 1_000_000
        + activity_rank * 100_000
        + review_tier * 10_000
        + int(interest_match) * 1_000
        + int(language_match) * 100
        + name_strength * 10
    )
    return {
        "ranking_score": score,
        "ranking_reasons": reasons,
        "verification_status": verification,
        "activity_status": activity_status,
        "review_count": review_count,
        "interest_match": interest_match,
        "language_match": language_match,
        "russian_name_match": name_match,
        "ranking_confidence": confidence,
        "_ranking_age_days": age_days if age_days is not None else 10**9,
        "_ranking_tiebreak": str(host.get("username") or host.get("id") or "").casefold(),
    }


def rank_hosts(hosts: Iterable[Mapping[str, Any]], traveler_interests: Iterable[str] = ()) -> list[dict[str, Any]]:
    ranked = []
    for source in hosts:
        item = dict(source)
        item.update(rank_host(item, traveler_interests))
        ranked.append(item)
    ranked.sort(key=lambda item: (-item["ranking_score"], item["_ranking_age_days"], item["_ranking_tiebreak"]))
    for position, item in enumerate(ranked, start=1):
        item["ranking_position"] = position
        item.pop("_ranking_age_days", None)
        item.pop("_ranking_tiebreak", None)
    return ranked
