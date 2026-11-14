"""Canonical profile-name trust and identity normalization rules."""

import re
from typing import Any, Dict, Tuple


COMMON_JP_SURNAMES = {
    "sato", "suzuki", "takahashi", "tanaka", "watanabe", "ito", "yamamoto", "nakamura",
    "kobayashi", "kato", "yoshida", "yamada", "sasaki", "yamaguchi", "saito", "matsumoto",
    "inoue", "kimura", "hayashi", "shimizu", "yamazaki", "mori", "abe", "ikeda", "hashimoto",
    "yamashita", "ishikawa", "nakajima", "maeda", "fujita", "ogawa", "goto", "okada",
    "hasegawa", "murakami", "kondo", "ishii", "sakamoto", "endo", "aoki", "fujii",
    "nishimura", "fukuda", "miura", "fujiwara", "okamoto", "matsuda", "nakagawa", "harada",
    "ono", "tamura", "takeuchi", "kaneko", "wada", "nakano", "ishizuka", "moroi", "tokuda",
    "hagimori", "ogura", "yamauchi", "shiozaki", "higashi", "suda", "ueda", "kojima",
    "shibata", "sakai", "miyazaki", "kudo", "yokoyama", "miyake", "noguchi", "otsuka",
    "chiba", "iwasaki", "kikuchi", "kinoshita", "arai", "sugiyama", "matsuoka", "hirano",
    "uchida", "kubo", "nomura", "miyata", "kitamura", "igarashi", "hoshino", "sano",
    "oshima", "sugawara", "naito", "tani", "doi", "nogami", "tsukamoto", "hori",
    "nishida", "kawai", "shiraishi", "sekine", "kuroda", "kuwabara", "tsuji", "asano",
}


def verify_host_name(host: Dict[str, Any]) -> Tuple[str, bool]:
    """
    Извлекает и строго валидирует настоящее имя хоста (display name / first name).
    Возвращает (first_name, is_trusted).

    Требования:
    1. Использовать подтверждённое first name из профиля.
    2. Не принимать фамилию за имя (японские фамилии типа Ishizuka, Tanaka).
    3. Не превращать username в имя.
    4. Если имя неоднозначно — возвращать ("", False), чтобы выставить Name_Review_Required.
    """
    raw_name = host.get("name") or ""
    raw_clean = host.get("clean_name") or ""
    username = str(host.get("username") or host.get("id") or "").strip().lstrip("@")
    bio = host.get("about_me") or host.get("about_me_clean") or ""

    fake_placeholders = {"there", "host", "user", "member", "unknown", "none", "null", "profile", "guest"}

    # Проверка 1: прямое указание имени в биографии ("My name is Hidetaka", "Call me Alex")
    m_bio = re.search(r"(?i)\b(?:my name is|call me)\s+([A-Za-z]+)", bio)
    if m_bio:
        bio_name = m_bio.group(1).capitalize()
        if bio_name.lower() not in fake_placeholders and len(bio_name) >= 2:
            if bio_name.lower() not in COMMON_JP_SURNAMES:
                return bio_name, True

    def clean_candidate(val: Any) -> str:
        if not val:
            return ""
        s = str(val).strip().lstrip("@")
        s = s.replace(chr(0x014c), "O").replace(chr(0x014d), "o")
        if s.lower() in fake_placeholders:
            return ""
        # Удаляем локационные суффиксы ("in Tokyo", "from Japan")
        s = re.sub(r"(?i)\s+(in|from|at)\s+.*$", "", s)
        # Удаляем смайлики
        s = re.sub(r"[:;=8][\-o\*\']?[\)\]\(\[dDpP/\\]", "", s)
        # Обработка скобок: "SATOMI (SATOMI)" -> "SATOMI"
        m_paren = re.search(r"\(([^)]+)\)", s)
        s_no_paren = re.sub(r"\([^)]*\)", "", s).strip()
        target = s_no_paren if s_no_paren else (m_paren.group(1) if m_paren else s)
        # Удаляем инициалы фамилий: "Sabina M.", "Olena S."
        target = re.sub(r"[\s\.,_-]+[A-Za-z]\.?$", "", target).strip()
        parts = target.split()
        if not parts:
            return ""
        first = parts[0]
        # Извлекаем буквенную часть (латиница)
        letters = re.findall(r"[a-zA-Z]+", first)
        if letters:
            w = letters[0]
            if w.lower() in fake_placeholders or len(w) < 2:
                return ""
            return w[0].upper() + w[1:].lower()
        return ""

    # Приоритет 1: настоящее имя (name)
    name_cand = clean_candidate(raw_name)
    if name_cand and name_cand.lower() not in COMMON_JP_SURNAMES:
        # Не должно совпадать с техническим username или состоять из цифр
        if name_cand.lower() != username.lower() and not name_cand.isdigit():
            return name_cand, True

    # Приоритет 2: clean_name (если это не username с дефисами/цифрами и не фамилия)
    if "-" not in str(raw_clean) and "_" not in str(raw_clean) and not any(ch.isdigit() for ch in str(raw_clean)):
        clean_cand = clean_candidate(raw_clean)
        if clean_cand and clean_cand.lower() not in COMMON_JP_SURNAMES:
            if clean_cand.lower() != username.lower() and not clean_cand.isdigit():
                return clean_cand, True

    # Если имя совпадает с username, но username не числовой и не содержит дефисов
    if name_cand and name_cand.lower() not in COMMON_JP_SURNAMES:
        if not username.isdigit() and "-" not in username and "_" not in username and len(name_cand) >= 3 and not any(ch.isdigit() for ch in username):
            return name_cand, True

    # Имя неоднозначно (фамилия, числовой ID, пустая графа, username)
    return "", False
