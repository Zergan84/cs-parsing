"""Pure host eligibility policies shared by application use cases."""

import re
from datetime import date
from typing import Optional, Set

BLACKLIST_PHRASES = (
    "not open to hosting",
    "can't host",
    "cannot host",
    "no hosting",
    "currently not hosting",
    "stopped hosting",
    "not hosting",
)


def is_blacklisted(about_me: Optional[str]) -> Optional[str]:
    """Return the first hosting opt-out phrase found in a profile, if any."""
    if not about_me:
        return None
    text = str(about_me).strip().lower()
    return next((phrase for phrase in BLACKLIST_PHRASES if phrase in text), None)


MONTHS = {
    1: ("january", "jan", "январ", "января"),
    2: ("february", "feb", "феврал", "февраля"),
    3: ("march", "mar", "март", "марта"),
    4: ("april", "apr", "апрел", "апреля"),
    5: ("may", "май", "мая"),
    6: ("june", "jun", "июн", "июня"),
    7: ("july", "jul", "июл", "июля"),
    8: ("august", "aug", "август", "августа"),
    9: ("september", "sep", "sept", "сентябр", "сентября"),
    10: ("october", "oct", "октябр", "октября"),
    11: ("november", "nov", "ноябр", "ноября"),
    12: ("december", "dec", "декабр", "декабря"),
}

REFUSAL_WORDS = r"(?:not|no|without|don't|do not|cannot|can't|won't|unavailable|away|traveling|travelling|не принимаю|не приму|не могу принять|не буду дома|меня не будет|не хост|no hosting)"

PROFILE_FIELDS = {
    "about": ("about", "about_me", "aboutMe", "about_me_clean"),
    "house_rules": ("house_rules", "houseRules", "my_home", "myHome"),
    "shared_information": ("shared_information", "sharedInformation"),
    "why_on_couchsurfing": ("why_on_couchsurfing", "whyOnCouchsurfing", "why_i_am_on_couchsurfing"),
    "personal_description": ("personal_description", "personalDescription"),
    "additional_information": ("additional_information", "additionalInformation"),
    "references": ("references", "reviews", "reference_text"),
    "profile_card": ("profile_card_text", "profileCardText"),
    "hosting_preferences": ("hosting_preferences", "hostingPreferences", "guestRequirements", "guest_requirements"),
}


def profile_evidence(host: dict) -> list[dict]:
    """Extract only actually received profile text, preserving where it came from."""
    nested = host.get("profile_api_data")
    if isinstance(nested, dict):
        host = {**nested, **host}
    evidence = []
    for source, keys in PROFILE_FIELDS.items():
        value = next((host.get(key) for key in keys if host.get(key) not in (None, "", [], {})), None)
        if isinstance(value, dict):
            value = value.get("text") or value.get("body") or value.get("content") or ""
        if isinstance(value, list):
            value = " ".join(
                str(item.get("text") or item.get("body") or item.get("content") or "") if isinstance(item, dict) else str(item)
                for item in value
            )
        text = " ".join(str(value or "").split())
        if text:
            evidence.append({"source": source, "text": text})
    return evidence


def evaluate_host_eligibility(host: dict, *, arrival_date=None, departure_date=None, guest_gender="male", target_city=None):
    """Return explicit exclusion evidence; missing profile fields remain unverified."""
    sections = profile_evidence(host)
    for section in sections:
        reason = evaluate_hosting_exclusion(
            section["text"], arrival_date=arrival_date, departure_date=departure_date,
            guest_gender=guest_gender, target_city=target_city,
        )
        if reason:
            reason_lower = reason.casefold()
            terms = [x.strip(".,:()") for x in reason.split() if len(x.strip(".,:()")) > 3]
            excerpt = next((section["text"][max(0, section["text"].casefold().find(term.casefold())-80):][:220]
                            for term in terms if term.casefold() in section["text"].casefold()), section["text"][:220])
            return {"eligible": False, "reason": reason, "source": section["source"], "excerpt": excerpt}
    return {"eligible": True, "status": "NOT_EXCLUDED_FROM_AVAILABLE_EVIDENCE", "reason": None, "source": None, "excerpt": None,
            "checked_sections": [section["source"] for section in sections],
            "unavailable_sections": [name for name in PROFILE_FIELDS if name not in {item["source"] for item in sections}]}


def _months_in_trip(arrival_date: Optional[str], departure_date: Optional[str]) -> Set[int]:
    try:
        start = date.fromisoformat(str(arrival_date)[:10])
        end = date.fromisoformat(str(departure_date)[:10])
    except (TypeError, ValueError):
        return set()
    if end < start:
        return set()
    months: Set[int] = set()
    cursor = date(start.year, start.month, 1)
    while cursor <= end:
        months.add(cursor.month)
        cursor = date(cursor.year + (cursor.month == 12), cursor.month % 12 + 1, 1)
    return months


def evaluate_hosting_exclusion(
    profile_text: Optional[str],
    *,
    arrival_date: Optional[str] = None,
    departure_date: Optional[str] = None,
    guest_gender: str = "male",
    target_city: Optional[str] = None,
) -> Optional[str]:
    """Return a concrete reason to exclude a host for this guest and trip.

    Unknown or vague profile data does not count as acceptance evidence. This
    function only excludes on explicit refusal, a matching gender restriction,
    a trip-month conflict, or a clear relocation statement.
    """
    if not profile_text:
        return None
    text = " ".join(str(profile_text).split())
    lower = text.casefold()

    refusal = is_blacklisted(lower)
    if refusal:
        return f"Explicit hosting refusal: {refusal}"
    explicit_refusal_patterns = (
        r"\b(?:i'?m|i am)\s+not\s+(?:currently\s+)?accepting\s+(?:any\s+)?guests?\b",
        r"\bno\s+longer\s+(?:host|hosting|accept(?:ing)?\s+guests?)\b",
        r"\b(?:не принимаю|не принимаем|не могу принять|не буду принимать)\s+(?:у себя\s+)?(?:гостей|гостя)\b",
        r"\b(?:гостей не принимаю|гостей не принимаем|не принимаю гостей)\b",
        r"\b(?:moved away|no longer live here|no longer living here|relocated away)\b",
    )
    for pattern in explicit_refusal_patterns:
        match = re.search(pattern, lower)
        if match:
            return f"Profile says host is unavailable: {match.group(0)}"

    moved = re.search(r"\b(?:i\s+)?(?:recently\s+)?moved\s+(?:to|from)\s+([a-z][a-z\s-]{1,40})|\b(?:переехал|переехала|переехали)(?:\s+(?:в|из)\s+([а-яёa-z][а-яёa-z\s-]{1,40}))?", lower)
    if moved:
        destination = (moved.group(1) or moved.group(2) or "").strip(" .,!?")
        target = str(target_city or "").strip().lower()
        if not target or not destination or target not in destination:
            return f"Profile indicates relocation: {moved.group(0).strip()}"

    male_guest = str(guest_gender or "").strip().lower() in {"male", "man", "мужчина", "мужской"}
    female_guest = str(guest_gender or "").strip().lower() in {"female", "woman", "женщина", "женский"}
    if male_guest:
        patterns = (
            r"\b(?:no|not accepting|don't accept|do not accept|won't host|don't host|do not host)\s+(?:male|men|guys)(?:\s+(?:guests?|travelers?|couchsurfers?))?\b",
            r"\b(?:male|men)\s+(?:guests?|travelers?|couchsurfers?)\s+(?:are\s+)?not\s+(?:accepted|welcome|allowed)\b",
            r"\b(?:only\s+(?:female|women|girls)|female guests? only|women only|girls only)\b",
            r"\b(?:only|female only|women only|girls only)\s+(?:female\s+)?(?:guests?|travelers?|women|girls)\b",
            r"\b(?:только|принимаю только)\s+(?:женщин|девушек)\b",
            r"\b(?:не принимаю|не принимаем|не принимают)\s+(?:в гости\s+)?(?:мужчин|парней)\b",
            r"\b(?:мужчин|мужчин-гостей)\s+(?:не принимаю|не принимаем|не принимаются)\b",
        )
        for pattern in patterns:
            match = re.search(pattern, lower)
            if match:
                return f"Guest gender restriction: {match.group(0)}"
    if female_guest:
        for pattern in (r"\b(?:no|don't accept|do not accept|won't host)\s+(?:female|women|girls)\b", r"\bтолько мужчин\b"):
            match = re.search(pattern, lower)
            if match:
                return f"Guest gender restriction: {match.group(0)}"

    trip_months = _months_in_trip(arrival_date, departure_date)
    for month in trip_months:
        for month_name in MONTHS[month]:
            for match in re.finditer(re.escape(month_name), lower):
                context = lower[max(0, match.start() - 55): min(len(lower), match.end() + 55)]
                if re.search(REFUSAL_WORDS, context):
                    return f"Hosting unavailable during trip month ({month_name.title()})"

    return None
