"""
message_generator.py - Генератор персонализированных запросов на Couchsurfing.

Формирует живое, уважительное и лаконичное сообщение для хоста:
1. Теплое личное приветствие с отсылкой к конкретному интересу хоста (настолки, кино, наука, кулинария, фото, музыка, спорт).
2. Четкие даты заезда/выезда в разговорном формате (Nov 18–20) и естественная цель поездки.
3. Соблюдение пространства: дневная занятость в городе, предложение ненавязчивого вечернего общения (кофе, совместный перекус, истории).
4. Строгий объем: 65-85 слов в 3 микро-параграфах.
"""

import re
from datetime import datetime
from typing import Any, Dict, Optional


def format_conversational_dates(check_in: str, check_out: str) -> str:
    """Форматирует даты в разговорный английский: Nov 18–20."""
    try:
        cin_clean = str(check_in).strip()[:10]
        cout_clean = str(check_out).strip()[:10]
        d_in = datetime.strptime(cin_clean, "%Y-%m-%d")
        d_out = datetime.strptime(cout_clean, "%Y-%m-%d")
        m_in = d_in.strftime("%b")
        m_out = d_out.strftime("%b")
        if d_in.month == d_out.month:
            return f"{m_in} {d_in.day}–{d_out.day}"
        else:
            return f"{m_in} {d_in.day} – {m_out} {d_out.day}"
    except Exception:
        return "Nov 18–20"


def format_date_range(check_in: str, check_out: str) -> str:
    """Алиас для обратной совместимости."""
    return format_conversational_dates(check_in, check_out)


def extract_greeting_name(host: Dict[str, Any]) -> str:
    """
    Извлекает чистое человеческое имя для приветствия из данных хоста.
    
    Приоритет: clean_name > name > username
    Очищает от технических приставок, цифр, дефисов, подчёркиваний.
    Например:
      - "satoko-okts" -> "Satoko"
      - "lin-lin-84" -> "Lin"
      - "Bobur_ns" -> "Bobur"
      - "Alice J." -> "Alice"
    """
    raw = host.get("clean_name") or host.get("name") or host.get("username") or "there"
    if not raw:
        return "there"

    # Базовая очистка через data_cleaner (убирает фамильные инициалы, References и т.д.)
    try:
        from data_cleaner import sanitize_host_clean_name
        name = sanitize_host_clean_name(raw)
    except Exception:
        name = str(raw)

    name = str(name).strip()
    if not name:
        return "there"

    # Берем только первое слово (до пробела)
    parts = name.split()
    first_part = parts[0] if parts else name

    # Разбиваем по всем не-буквенным символам (дефис, подчёркивание, цифра, точка и т.д.)
    # и берем первый буквенный фрагмент
    letter_parts = re.split(r'[^a-zA-Zа-яА-ЯёЁ]+', first_part)
    letter_parts = [p for p in letter_parts if p]

    if letter_parts:
        first_name = letter_parts[0]
    elif len(parts) > 1:
        # Пробуем следующее слово
        return extract_greeting_name({**host, "clean_name": parts[1]})
    else:
        return "there"

    # Капитализируем: первая буква заглавная, остальные строчные
    if len(first_name) > 1:
        first_name = first_name[0].upper() + first_name[1:].lower()
    else:
        first_name = first_name.upper()

    return first_name


def generate_stay_request(
    host: Dict[str, Any],
    check_in: str = "2026-11-18",
    check_out: str = "2026-11-20",
    guest_context: Optional[str] = None,
) -> str:
    """
    Генерирует строго персонализированный текст запроса (45-65 слов).
    Динамически учитывает город и даты хоста.
    """
    first_name = extract_greeting_name(host)

    # Динамический город
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

    # Динамические даты
    cin = host.get("check_in") or host.get("arrival_date") or check_in
    cout = host.get("check_out") or host.get("departure_date") or check_out
    date_str = format_conversational_dates(cin, cout)
    bio = (host.get("about_me_clean") or host.get("about_me") or "").lower()

    # Специфический хук под реальные увлечения хоста
    if any(k in bio for k in ("board game", "boardgame", "catan", "chess")):
        hook = f"Hi {first_name}!\n\nYour passion for board games and tabletop strategy is wonderful! There’s such a great community spirit in playing games together — what’s been a favorite one you’ve been playing lately around {city}?"
    elif any(k in bio for k in ("cook", "culinary", "foodie", "ramen", "sushi", "baking", "wine", "sake", "chef")):
        hook = f"Hi {first_name}!\n\nYour philosophy around authentic home cooking and discovering local food markets completely resonates with me. There’s something genuinely special about food connecting people across cultures."
    elif any(k in bio for k in ("film", "cinema", "director", "cinematography", "movie")):
        hook = f"Hi {first_name}!\n\nReally enjoyed reading about your creative focus in film and storytelling. It's always inspiring to see people actively nurturing local arts and staying curious about different visual perspectives."
    elif any(k in bio for k in ("photo", "photography", "camera", "analog", "35mm")):
        hook = f"Hi {first_name}!\n\nYour eye for photography and visual storytelling really stood out to me. Are there any favorite neighborhood corners in {city} you particularly love capturing on film?"
    elif any(k in bio for k in ("music", "vinyl", "record", "piano", "guitar", "jazz")):
        hook = f"Hi {first_name}!\n\nYour love for good music and vinyl records is fantastic. Finding cozy listening spaces and sharing favorite tunes is one of my favorite things when exploring new cities."
    elif any(k in bio for k in ("teacher", "education", "science")):
        hook = f"Hi {first_name}!\n\nYour dedication to teaching and education is wonderful. Exchanging perspectives on learning and cultural adaptation is always fascinating to chat about."
    elif any(k in bio for k in ("travel", "countries", "backpack")):
        hook = f"Hi {first_name}!\n\nYour extensive travel journey is such a wonderful milestone! Exploring that deeply really shifts how one sees the world — what’s been one place that surprised you the most so far?"
    else:
        hook = f"Hi {first_name}!\n\nYour thoughtful perspective on hosting and meaningful cultural exchange really stood out to me. It is always inspiring to connect with hosts who appreciate genuine human connection."

    p2 = f"I’m Andrey, a ski instructor passing through {city} {date_str} on my way to coach skiing at Club Med Tomamu in Hokkaido for the winter season. Having traveled through 55+ countries — from vibrant hubs like Hong Kong to mountain life in Nepal — I always value how Couchsurfing connects kindred spirits. My bulky ski gear is already forwarded ahead via Yamato Takkyubin, so I travel light with just a daypack and easily adapt to your daily rhythm."

    endings = [
        f"I’d love to cross paths, check out your favorite neighborhood spot, and share travel stories over tea or a drink if our schedules align!\n\nWarmly,\nAndrey",
        f"Would be wonderful to connect over a casual dinner or tea and swap travel memories if you have some free time!\n\nCheers,\nAndrey",
        f"Would love to buy you a coffee or tea and chat about your favorite local corners if you happen to be free!\n\nBest,\nAndrey"
    ]
    u_hash = sum(ord(c) for c in (host.get("username") or first_name))
    p3 = endings[u_hash % len(endings)]

    return f"{hook}\n\n{p2}\n\n{p3}"
