"""Canonical deterministic request-message generation and quality rules."""

import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple, Union

from core.context import validate_text_purity
from core.identity import verify_host_name
from core.models import Trip


def extract_clean_first_name(raw_name: str, username: str) -> str:
    """Извлекает и очищает человеческое имя хоста."""
    clean = (raw_name or "").strip()
    if not clean or clean.lower() == username.lower() or "@" in clean or any(c.isdigit() for c in clean):
        parts = username.replace(".", " ").replace("_", " ").replace("-", " ").split()
        for p in parts:
            if p.isalpha() and len(p) >= 2:
                return p.capitalize()
        return "there"

    first = clean.split()[0].capitalize()
    return first if first.isalpha() else "there"


def build_personalized_message(host: Dict[str, Any], trip: Union[Dict[str, Any], Trip]) -> Tuple[str, bool, str]:
    """
    Формирует выверенное персонализированное сообщение на основе совпавших фактов.
    - Для минимального био (<25 слов): строго 30–70 слов.
    - Для обычного био (25+ слов): целевой диапазон 60–90 слов (допустимо 50–105).
    - Защита от роботизированных клише и запрещенных географических галлюцинаций.
    Возвращает (текст, qc_passed, qc_note).
    """
    first_name, trusted_name = verify_host_name(host)
    first_name = first_name or extract_clean_first_name(
        host.get("name") or host.get("display_name") or host.get("clean_name") or "",
        host.get("username", "")
    )
    reasons = host.get("score_reasons") or []
    about_text = str(host.get("about") or host.get("about_me") or host.get("about_me_clean") or "").strip()
    reasons_str = (
        " ".join(reasons) + " " +
        about_text + " " +
        str(host.get("interests_text") or "") + " " +
        str(host.get("occupation") or "")
    ).lower()

    if isinstance(trip, dict):
        city = trip.get("city") or "the city"
        arr = trip.get("arrival_date") or ""
        dep = trip.get("departure_date") or ""
    else:
        city = trip.city or "the city"
        arr = trip.arrival_date or ""
        dep = trip.departure_date or ""

    if arr and dep:
        try:
            arr_dt = datetime.strptime(str(arr)[:10], "%Y-%m-%d")
            dep_dt = datetime.strptime(str(dep)[:10], "%Y-%m-%d")
            if arr_dt.month == dep_dt.month:
                dates_str = f"({arr_dt.strftime('%b')} {arr_dt.day}–{dep_dt.day})"
            else:
                dates_str = f"({arr_dt.strftime('%b')} {arr_dt.day} – {dep_dt.strftime('%b')} {dep_dt.day})"
        except (TypeError, ValueError):
            dates_str = f"({arr} – {dep})"
    elif arr:
        dates_str = f"(around {arr})"
    else:
        dates_str = "for a couple of nights"

    is_minimal = len(about_text.split()) < 25 or about_text.lower().startswith("friendly local resident")

    if is_minimal:
        hook_sentence = "I appreciate your open hospitality on Couchsurfing."
        guest_fact = f"I am visiting {city} to explore its architecture and street photography."
        lines = [
            ("Hello," if host.get("_neutral_greeting") or not trusted_name else f"Hi {first_name},"),
            "",
            f"{hook_sentence} {guest_fact} Originally from Siberia, I work as an alpine ski instructor and have been on CS for years. I keep a quiet and tidy routine.",
            "",
            f"I will be visiting {city} {dates_str}. If your schedule allows hosting, I would be glad to connect and exchange stories!",
            "",
            "Warmly,",
            "Andrey",
        ]
    else:
        if any(k in reasons_str for k in ["горы", "лыжи", "ski", "mountain", "hiking", "trekking", "alps"]):
            hook_sentence = "Your shared love for the mountains and outdoor adventures really resonated with me."
            guest_fact = "I work as an alpine ski coach in Courchevel (French Alps) and grew up in Siberia, so mountain trails, snow, and outdoor life are second nature to me."
        elif any(k in reasons_str for k in ["кино", "фото", "арт", "photo", "cinema", "film", "art", "camera", "visual"]):
            hook_sentence = "Your creative interests in visual culture, film, and photography really caught my attention."
            guest_fact = "I studied film directing at a film school and actively practice street photography; visual storytelling and capturing genuine moments are central to how I travel."
        elif any(k in reasons_str for k in ["музыка", "фортепиано", "music", "piano", "jazz", "concert", "sound"]):
            hook_sentence = "I was delighted to see your appreciation for acoustic music and piano."
            guest_fact = "I play the piano and deeply appreciate musical traditions, quiet evening conversations, sound design, and genuine cultural exchange."
        elif any(k in reasons_str for k in ["французский", "french", "france"]):
            hook_sentence = "It was wonderful to notice your connection with the French language and European culture."
            guest_fact = "I work as a ski coach in the French Alps (Courchevel), so sharing perspectives in French or English over local traditions would be a pleasure."
        else:
            hook_sentence = "Your welcoming approach to hosting and cultural exchange immediately stood out to me."
            guest_fact = f"Originally from Siberia, I work as an alpine ski instructor and have been on Couchsurfing for many years. Visiting {city} for photography, architecture, and local culture."

        lines = [
            ("Hello," if host.get("_neutral_greeting") or not trusted_name else f"Hi {first_name},"),
            "",
            hook_sentence,
            "",
            f"{guest_fact} I'm a seasoned Couchsurfer, quiet, tidy, and respectful of house rules.",
            "",
            f"I will be visiting {city} {dates_str}. If your schedule allows hosting, I'd be glad to connect and exchange stories!",
            "",
            "Warmly,",
            "Andrey"
        ]

    message_text = "\n".join(lines)
    qc_ok, qc_issues = validate_message_qc(
        message_text, trip, is_minimal_bio=is_minimal, host_data=host,
    )
    qc_note = "Strict QC Passed" if qc_ok else "; ".join(qc_issues)
    return message_text, qc_ok, qc_note


def validate_message_qc(
    text: str,
    trip: Optional[Union[Dict[str, Any], Trip]] = None,
    min_words: Optional[int] = None,
    max_words: Optional[int] = None,
    is_minimal_bio: bool = False,
    host_data: Optional[Dict[str, Any]] = None,
    trip_city: Optional[str] = None,
) -> Tuple[bool, List[str]]:
    """
    Единый фильтр качества (QC):
    1. Для минимального био (<25 слов): строго 30–70 слов.
    2. Для обычного био: 50–105 слов (целевой диапазон 60–90).
    3. Присутствие города поездки.
    4. Отсутствие выдуманных фактов и роботизированных клише (validate_text_purity).
    """
    issues: List[str] = []
    if not text or not text.strip():
        return False, ["Empty message draft"]

    if min_words is None or max_words is None:
        if is_minimal_bio:
            min_w = 30 if min_words is None else min_words
            max_w = 70 if max_words is None else max_words
        else:
            min_w = 50 if min_words is None else min_words
            max_w = 105 if max_words is None else max_words
    else:
        min_w = min_words
        max_w = max_words

    words = text.strip().split()
    wc = len(words)
    if wc < min_w:
        issues.append(f"Too short ({wc} words, min {min_w})")
    if wc > max_w:
        issues.append(f"Too long ({wc} words, max {max_w})")

    city = trip_city or (trip.get("city") if isinstance(trip, dict) else (trip.city if trip else ""))
    if city and city.lower() not in text.lower():
        issues.append(f"Missing target city '{city}' in message")

    pure, purity_issues = validate_text_purity(text)
    if not pure:
        issues.extend(purity_issues)

    strict_ok, strict_issues = _validate_dispatch_qc(
        text, is_minimal_bio=is_minimal_bio, host_data=host_data, trip_city=city
    )
    issues.extend(issue for issue in strict_issues if issue not in issues)
    return (len(issues) == 0 and strict_ok, issues)



def _validate_dispatch_qc(
    text: str,
    is_minimal_bio: bool = False,
    host_data: Optional[Dict[str, Any]] = None,
    trip_city: Optional[str] = None,
) -> Tuple[bool, List[str]]:
    """
    Контроль качества (QC Validator):
    1. Длина в диапазоне 55-130 слов (целевой диапазон: 60-90 слов; для пустых био: 30-70 слов).
    2. Отсутствие шаблонных копипаст.
    3. Отсутствие строго запрещенных фраз (BANNED):
       - 'won't crowd your space', 'crowd your space', 'crowd your routine'
       - 'love your passion for', 'excited about your', 'hope you have a great season'
       - 'traveling light with a daypack', 'only a daypack', 'with just a daypack'
       - 'fully independent during the day'
       - 'historic neighborhoods'
       - '55+ countries', 'over 55 countries'
       - '10+ years on couchsurfing'
       - 'tonight'
       - em-dashes ('—')
       - робо-дат в формате ISO (2026-11-18)
    4. Фактическая заземленность (Factual Grounding):
       - Запрет выдуманных travel routes ('on my way north to Hokkaido' при поездке в Киото/Токио).
       - Запрет не подтвержденных общих интересов ('We both enjoy language learning' без пруфа в профиле хоста).
       - Строгое разделение фактов хоста и личного бэкграунда Андрея.
    """
    if not text:
        return False, ["Empty text"]

    words = len(text.strip().split())
    issues = []

    if is_minimal_bio:
        if words < 30:
            issues.append(f"Too short ({words} words, minimum 30)")
        elif words > 70:
            issues.append(f"Too long for minimal bio ({words} words, maximum 70)")
    else:
        if words < 55:
            issues.append(f"Too short ({words} words, minimum 55)")
        elif words > 130:
            issues.append(f"Too long ({words} words, maximum 130)")

    lower = text.lower()
    if "would love to connect!" in lower or "andrey here from the french alps" in lower:
        issues.append("Contains generic template stub")

    banned_phrases = [
        "won't crowd your space",
        "crowd your space",
        "crowd your routine",
        "love your passion for",
        "love the passion for",
        "excited about your",
        "excited about the",
        "hope you have a great season",
        "grabbing a quick coffee and sharing travel stories",
        "sharing travel stories in the evening",
        "i noticed",
        "i saw on your profile",
        "according to your bio",
        "caught my eye",
        "tonight",
        "hidden gems",
        "what an adventure",
        "love that vibe",
        "traveling light with a daypack",
        "travel light with a daypack",
        "with just a daypack",
        "with only a daypack",
        "with just my daypack",
        "fully independent during the day",
        "historic neighborhoods",
        "55+ countries",
        "over 55 countries",
        "10+ years on couchsurfing",
        "a decade of travels",
        "yamato takkyubin",
        "yamato delivery",
        "takkyubin",
        "your passion for",
        "outside of sports coaching, i studied film and play piano",
        "would love to cross paths if your timing aligns",
        "thanks for considering hosting a quiet, respectful guest",
        "on my way north to hokkaido",
        "on my way up north",
        "heading to hokkaido",
        "heading north to hokkaido",
        "for skiing in hokkaido",
        "club med tomamu",
        "we both enjoy language learning",
        "we both love language learning",
        "we both enjoy languages",
        "moscow",
        "russia",
        "russian",
        "москва",
        "россия",
        "vgik",
        "вгик",
    ]
    for b in banned_phrases:
        if b in lower:
            issues.append(f"Contains forbidden cliche or origin: '{b}'")

    if re.search(r"\b(moscow|russia|russian|москв[аеыу]|росси[яеию]|вгик)\b", lower):
        issues.append("BANNED: Mentions Moscow, Russia, or VGIK (user is flying from Geneva, Switzerland)")

    if "—" in text:
        issues.append("Contains forbidden em-dash ('—')")

    if re.search(r"\b\d{4}-\d{2}-\d{2}\b", text):
        issues.append("Contains robotic ISO date format")

    # 4. Factual Grounding: Route Inferences
    # The Trip Card establishes destination and dates, not onward routes.
    effective_city = (trip_city or "").lower()
    if "hokkaido" not in effective_city and "sapporo" not in effective_city:
        if re.search(r"\b(on my way (?:up )?north|heading (?:up )?north)\b", lower):
            issues.append("Contains ungrounded travel route inference ('on my way north')")
        if re.search(r"\b(hokkaido|tomamu)\b", lower):
            issues.append("Contains ungrounded travel destination ('Hokkaido') outside Trip Card destination")

    # 5. Factual Grounding: Shared Interest Claims
    # "We both enjoy/love/share/like X" requires explicit evidence from the host
    shared_match = re.search(r"\bwe both (enjoy|love|share|like)\s+([a-z\s]+?)(?:[,.]|\bI\b|\band\b|$)", lower)
    if shared_match:
        claimed_interest = shared_match.group(2).strip()
        if host_data:
            bio_text = str(host_data.get("about_me_clean") or host_data.get("about_me") or "").lower()
            interests_text = str(host_data.get("interests") or "").lower()
            host_all = f"{bio_text} {interests_text}"
            words_to_check = [w for w in claimed_interest.split() if len(w) > 3 and w not in ("both", "also", "really", "learning")]
            if words_to_check and not any(w in host_all for w in words_to_check):
                issues.append(f"Ungrounded shared interest claim: 'we both {shared_match.group(1)} {claimed_interest}' without evidence in host profile")
        else:
            # General strict check against presumptuous shared language learning
            if "language" in claimed_interest:
                issues.append(f"Ungrounded shared interest claim: 'we both {shared_match.group(1)} {claimed_interest}'")

    return len(issues) == 0, issues
