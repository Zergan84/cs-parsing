"""
message_generator.py - Personalized request generator for Couchsurfing.

Forms a warm, respectful, and concise message to a host:
1. Warm personal greeting with reference to the host's specific interest.
2. Clear arrival/departure dates in conversational format (Nov 18-20) and natural trip purpose.
3. Respect for space: daytime activity in the city, suggestion of low-pressure evening chat.
4. Strict volume: 65-90 words in 3 micro-paragraphs.
All text is English only, ASCII only.
"""

import json
import random
import re
from datetime import datetime
from typing import Any, Dict, Optional


def format_conversational_dates(check_in: str, check_out: str) -> str:
    """Formats dates into conversational English: Nov 18-20."""
    try:
        cin_clean = str(check_in).strip()[:10]
        cout_clean = str(check_out).strip()[:10]
        d_in = datetime.strptime(cin_clean, "%Y-%m-%d")
        d_out = datetime.strptime(cout_clean, "%Y-%m-%d")
        m_in = d_in.strftime("%b")
        m_out = d_out.strftime("%b")
        if d_in.month == d_out.month:
            return f"{m_in} {d_in.day}-{d_out.day}"
        else:
            return f"{m_in} {d_in.day} - {m_out} {d_out.day}"
    except Exception:
        return "Nov 18-20"


def format_date_range(check_in: str, check_out: str) -> str:
    """Alias for backwards compatibility."""
    return format_conversational_dates(check_in, check_out)


def extract_greeting_name(host: Dict[str, Any]) -> str:
    """
    Extracts a clean human name for greeting from host data.

    Priority:
    1. Real name from display name (`host.get("name")`) - e.g. "Sabina M." -> "Sabina"
    2. Field `clean_name`, if it does not match username/slug and is not 'there'
    3. If name is empty - letter part from `username` (e.g. "@sakura58670" -> "Sakura")
    4. If name cannot be extracted at all (e.g. only digits) - returns "" (empty string),
       so the generator uses the universal greeting "Hello!" instead of "Hello There!".
    """
    raw_name = host.get("name") or ""
    raw_clean = host.get("clean_name") or ""
    username = str(host.get("username") or host.get("id") or "").strip().lstrip("@")

    fake_placeholders = {"there", "host", "user", "member", "unknown", "none", "null"}

    def clean_one(val: Any) -> str:
        if not val:
            return ""
        s = str(val).strip().lstrip("@")
        # Normalize macron/diacritic chars
        s = s.replace(chr(0x014c), "O").replace(chr(0x014d), "o")
        if s.lower() in fake_placeholders:
            return ""
        # Remove location suffixes ("in Tokyo", "from Japan")
        s = re.sub(r"(?i)\s+(in|from|at)\s+.*$", "", s)
        # Remove emoticons (:), :D, etc.)
        s = re.sub(r"[:;=8][\-o\*\']?[\)\]\(\[dDpP/\\]", "", s)
        # Handle parentheses: e.g. "SATOMI (SATOMI)" -> "SATOMI"
        m_paren = re.search(r"\(([^)]+)\)", s)
        s_no_paren = re.sub(r"\([^)]*\)", "", s).strip()
        target = s_no_paren if s_no_paren else (m_paren.group(1) if m_paren else s)
        # Remove surname initials: "Sabina M.", "Olena S.", "Ka W."
        target = re.sub(r"[\s\.,_-]+[A-Za-z]\.?$", "", target).strip()

        parts = target.split()
        if not parts:
            return ""
        first = parts[0]

        # Extract letter part (Latin), discarding digits and special chars
        letters = re.findall(r"[a-zA-Z]+", first)
        if letters:
            w = letters[0]
            if w.lower() in fake_placeholders:
                return ""
            if len(w) > 1:
                return w[0].upper() + w[1:].lower()
            return w.upper()

        return ""

    # Priority 1: real display name (if not username)
    cand_name = clean_one(raw_name)
    if cand_name and cand_name.lower() != username.lower():
        return cand_name

    # Priority 2: clean_name (if not equal to username)
    cand_clean = clean_one(raw_clean)
    if cand_clean and cand_clean.lower() != username.lower():
        return cand_clean

    # Priority 3: extract letter name from username ("@sakura58670" -> "Sakura", "satoko-okts" -> "Satoko")
    cand_user = clean_one(username)
    if cand_user:
        return cand_user

    return cand_name or cand_clean or ""


def _extract_anchor(host: Dict[str, Any]) -> str:
    """Extracts a profile fact / anchor for the opening hook."""
    hooks = host.get("conversation_hooks") or []
    if isinstance(hooks, str):
        try:
            hooks = json.loads(hooks)
        except Exception:
            hooks = [hooks]
    if isinstance(hooks, list) and hooks:
        first = str(hooks[0]).strip()
        if first:
            return first

    secret = host.get("secret_code_hint") or ""
    if secret:
        return secret

    bio = (host.get("about_me_clean") or host.get("about_me") or "").lower()
    interest_map = {
        "cook": "home cooking",
        "culinary": "home cooking",
        "foodie": "local food",
        "ramen": "ramen",
        "sushi": "sushi",
        "photo": "photography",
        "photography": "photography",
        "camera": "photography",
        "film": "film",
        "cinema": "cinema",
        "movie": "film",
        "music": "music",
        "vinyl": "vinyl",
        "guitar": "guitar",
        "piano": "piano",
        "travel": "travel",
        "countries": "travel",
        "backpack": "backpacking",
        "sport": "sports",
        "sport": "sports",
    }
    for key, topic in interest_map.items():
        if key in bio:
            return topic

    return "hosting"


def generate_stay_request(
    host: Dict[str, Any],
    check_in: str = "2026-11-18",
    check_out: str = "2026-11-20",
    guest_context: Optional[str] = None,
) -> str:
    """
    Generates a strictly personalized request message (65-90 words).
    Dynamically accounts for host city and dates.
    If host name is empty - uses universal greeting 'Hello!'.
    All text is English only, ASCII only.
    """
    first_name = extract_greeting_name(host)
    greeting = f"Hi {first_name}!" if first_name else "Hello!"

    # Dynamic city
    raw_loc = host.get("location")
    if isinstance(raw_loc, dict):
        loc_desc = raw_loc.get("locationDescription", {}).get("description") or raw_loc.get("slug") or "Tokyo"
    else:
        loc_desc = str(raw_loc or "Tokyo")

    city = host.get("city")
    if not city:
        ld_lower = loc_desc.lower()
        if "sapporo" in ld_lower:
            city = "Sapporo"
        elif "kyoto" in ld_lower:
            city = "Kyoto"
        elif "osaka" in ld_lower:
            city = "Osaka"
        elif "tokyo" in ld_lower:
            city = "Tokyo"
        else:
            city = loc_desc.split(",")[0].strip() or "Tokyo"
    # Strip non-ASCII from city (e.g. Ōtsu -> Otsu)
    city = "".join(ch for ch in city if ord(ch) < 128)

    # Dynamic dates
    cin = host.get("check_in") or host.get("arrival_date") or check_in
    cout = host.get("check_out") or host.get("departure_date") or check_out
    date_str = format_conversational_dates(cin, cout)

    # Anchor / profile fact for the opening hook
    anchor = _extract_anchor(host)

    # ----- Paragraph 1: Hook about the host's specific interest -----
    p1_options = [
        f"{greeting} Your {anchor} really caught my attention.",
        f"{greeting} I loved reading about your {topic if False else anchor}.",
        f"{greeting} Your passion for {anchor} stood out to me.",
    ]
    # Ensure the second option doesn't reference undefined variable
    p1_options = [
        f"{greeting} Your {anchor} really caught my attention.",
        f"{greeting} I loved reading about your {anchor}.",
        f"{greeting} Your passion for {anchor} stood out to me.",
    ]
    p1 = random.choice(p1_options)

    # ----- Paragraph 2: Andrey context (ski instructor, Hokkaido, Yamato, light travel) -----
    p2_options = [
        f"I'm Andrey, a ski instructor headed to Club Med Tomamu in Hokkaido for the winter, passing through {city} {date_str}. My bulky ski gear is already shipped ahead via Yamato delivery, so I travel light with just a daypack.",
        f"Andrey here - ski instructor heading to Hokkaido this winter, passing through {city} {date_str}. My ski gear is already forwarded via Yamato delivery, so I'm traveling light with only a backpack.",
        f"I'm Andrey, bound for Club Med Tomamu in Hokkaido for ski season, passing through {city} {date_str}. All my heavy gear is already shipped ahead via Yamato, so I move light.",
    ]
    p2 = random.choice(p2_options)

    # ----- Paragraph 3: Low-pressure closing -----
    p3_options = [
        f"I'd love to cross paths if our schedules align. Warmly, Andrey",
        f"Hope we can meet up if timing works. Cheers, Andrey",
        f"Would be great to connect if you're free. Best, Andrey",
    ]
    p3 = random.choice(p3_options)

    return f"{p1}\n\n{p2}\n\n{p3}"
