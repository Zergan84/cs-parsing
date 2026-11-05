"""
data_cleaner.py - Строгая стандартизация и очистка сырых полей анкет хостов (Имя, Локация).
"""

import re
from typing import Any


def sanitize_host_name(raw_name: Any) -> str:
    """
    Имя хоста должно содержать ТОЛЬКО первое имя и инициал
    (строго до слов 'References', 'Friends', 'Lives in' или перевода строки).
    """
    if not raw_name:
        return "Host"
    name_part = re.split(
        r'(\d+\s*References|\d+\s*Friends|Lives in|\n)',
        str(raw_name),
        flags=re.I
    )[0].strip()
    name_part = re.sub(r'\s+', ' ', name_part)
    return name_part or "Host"


def sanitize_host_clean_name(raw_name: Any) -> str:
    """
    Имя для приветствия и компактного бейджа (без фамильного инициала и знаков препинания).
    """
    name = sanitize_host_name(raw_name)
    first_name = re.sub(r"[\s\.,_-]+[A-Za-z]\.?$", "", name).strip()
    return first_name or name


def sanitize_host_location(raw_loc: Any, default_city: str = "Tokyo") -> str:
    """
    Локация должна содержать ТОЛЬКО географическое название: "Tokyo", "Kita-ku, Tokyo", "Minato", "Hatsudai".
    Исключаются любые артефакты биографии: "CURRENT MISSION", "Hello there!", "Kind, sincere", etc.
    """
    if not raw_loc:
        return default_city
    if isinstance(raw_loc, dict):
        loc_val = (
            raw_loc.get("city")
            or (raw_loc.get("locationDescription", {}).get("description") if isinstance(raw_loc.get("locationDescription"), dict) else None)
            or raw_loc.get("slug")
            or default_city
        )
        return str(loc_val).strip()

    loc_str = str(raw_loc).strip()
    loc_str = re.sub(r"^Lives in\s*", "", loc_str, flags=re.I).strip()

    loc_str = re.split(
        r'(\d+\s*References|\d+\s*Friends|CURRENT MISSION|About\s*Me|Hello|Kind\s*,|Kind\s+sincere|Kind\b|sincere|\n)',
        loc_str,
        flags=re.I
    )[0].strip()

    loc_str = re.sub(r'(Kind|Hello|CURRENT.*)$', '', loc_str, flags=re.I).strip()

    loc_str = loc_str.strip(" ,.-")
    if len(loc_str) > 40 or len(loc_str) < 2:
        return default_city
    return loc_str
