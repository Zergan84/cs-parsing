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


def sanitize_host_location(raw_loc: Any, default_city: str = "Kyoto") -> str:
    """
    Локация должна содержать ТОЛЬКО географическое название: "Kyoto", "Sakyo-ku, Kyoto", "Tokyo", etc.
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

    # Normalize Kyoto wards and macrons
    if any(k in loc_str.lower() for k in ["kamigy", "sakyo", "shimogy", "nakagy", "higashiyama", "kita-ku", "minami-ku", "ukyo", "fushimi", "yamashina"]):
        if "kyoto" not in loc_str.lower():
            loc_str = f"{loc_str}, Kyoto"
    loc_str = loc_str.replace("ō", "o").replace("ū", "u")

    loc_str = loc_str.strip(" ,.-")
    if len(loc_str) > 40 or len(loc_str) < 2:
        return default_city
    return loc_str


def extract_members_from_json(data: Any, target_set: set) -> None:
    """Извлекает handle получателей из структуры GraphQL/tRPC ответов."""
    if isinstance(data, list):
        for item in data:
            extract_members_from_json(item, target_set)
    elif isinstance(data, dict):
        members = data.get("resolvedMembers")
        if isinstance(members, list):
            for m in members:
                if isinstance(m, dict):
                    handle = m.get("handle")
                    if handle and handle.lower() != "vanagaz":
                        target_set.add(handle.strip().lower())
        for v in data.values():
            if isinstance(v, (dict, list)):
                extract_members_from_json(v, target_set)
