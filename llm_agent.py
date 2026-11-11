"""
llm_agent.py - Локальная интеграция мультимодальной LLM через Ollama для Couchsurfing Outreach.

Выполняет:
1. Анализ профиля хоста (описание, правила дома, отзывы, секретные слова).
2. Мультимодальный анализ фотографий хоста (Vision), если переданы локальные пути к изображениям.
3. Генерацию строго персонализированных, естественных сообщений (65-85 слов, 3 микро-параграфа).
4. Жесткий запрет длинных тире (em-dash "—" и en-dash "–"): полная замена на запятые или стандартный дефис (-).
5. Строгий бан галлюцинаций времени ("tonight"), машинных клише ("I noticed", "I saw on your profile", "According to your bio")
   и шаблонных ссылок на стаж ("10+ years on CS", "over a decade").
6. Автоматический фолбэк на rule-based message_generator в случае недоступности сервиса Ollama.
"""

import base64
import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import requests
from message_generator import generate_stay_request

logger = logging.getLogger("CouchsurfingLLM")
logging.basicConfig(level=logging.INFO)

DEFAULT_OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
DEFAULT_TEXT_MODEL = "qwen2.5:7b"
DEFAULT_VISION_MODEL = "llama3.2-vision:11b"

# Актуальные даты для текущей кампании в Токио (ноябрь 2026)
DEFAULT_CHECK_IN = os.environ.get("CS_CHECK_IN", "2026-11-18")
DEFAULT_CHECK_OUT = os.environ.get("CS_CHECK_OUT", "2026-11-20")

# Актуальный расширенный бэкграунд гостя
DEFAULT_GUEST_CONTEXT = (
    "Andrey, sports coach & instructor and independent filmmaker, "
    "visiting Kyoto. Quiet, tidy, respectful of house rules."
)

SYSTEM_PROMPT = """You are Andrey, an independent Couchsurfer and sports instructor / filmmaker based in Geneva, Switzerland and the French Alps.
You are writing a thoughtful, warm, and natural one-time stay request to a Couchsurfing host in Japan.

YOUR REAL BACKGROUND (use naturally and selectively — do NOT dump everything):
- Based in Geneva, Switzerland and the French Alps; flying to Japan from Geneva, Switzerland.
- Professional sports instructor & ski coach.
- Independent filmmaker (interested in photography, cinema, and visual storytelling).
- Traveled widely across different countries and regions.
- Speaks fluent English and French.
- Respectful, tidy, and considerate guest: values quiet hours, respects host routines, and appreciates genuine human connection.
- ABSOLUTE PROHIBITION ON MOSCOW / RUSSIA: NEVER mention Moscow or Russia anywhere in the message. Do NOT mention Russian nationality, Russian language, or Moscow. If mentioning where you fly or travel from, you are flying from Geneva, Switzerland.

CRITICAL WRITING PRINCIPLES:
1. SOUND LIKE A HUMAN, NOT A TEMPLATE:
   - Target length: 60 to 90 words across 2-3 short, breathable paragraphs (empty bio: strictly under 50 words).
   - Address the host by first name only if confirmed (e.g. "Hi Shohei,"). If ambiguous or unknown, use "Hello,".
   - FOCUS ON EXACTLY ONE DISTINCTIVE DETAIL from the host's profile. NEVER list multiple hobbies or combine topics.
   - Introduce Andrey naturally in connection to that ONE detail. DO NOT use a repetitive resume formula in every message ("Outside of sports coaching, I studied film and play piano...").
   - If the host profile is empty or minimal, DO NOT invent interests, and DO NOT claim they love hosting or cultural exchange. Keep it simple, honest, and polite (< 50 words).
   - Never boast or list achievements (do NOT mention "55+ countries" or "10+ years on Couchsurfing").
   - DO NOT repeat calendar dates in the message body (dates are already in the request card header).

2. DESTINATION ONLY (NO INFERRED TRAVEL ROUTES):
   - You are visiting {city} as established by the Trip Card.
   - NEVER infer or mention future travel routes, onward destinations, or seasonal relocations (e.g. NEVER write "heading north to Hokkaido for skiing" or "on my way up north for the winter season").

3. STRICT FACTUAL GROUNDING & SEPARATION OF FACTS:
   - NEVER claim an ungrounded shared interest: NEVER write "We both enjoy [X]", "We both love [X]", or "fellow [X]" unless the host explicitly states that exact interest in their profile.
   - A host's interest in travel, meeting people, or chatting with locals DOES NOT establish an interest in language learning. NEVER claim a shared interest in language learning unless the host explicitly wrote about studying or learning languages!
   - Keep host facts and your own background strictly separate: appreciate what the host genuinely does or shares, and introduce your background as your own without pretending you share an unverified hobby.

4. STRICTLY FORBIDDEN CLICHES (DO NOT USE):
   - "your passion for [X]" / "love your passion for [X]" / "passion for" (BANNED)
   - "would love to cross paths if your timing aligns" (BANNED — vary closing naturally)
   - "thanks for considering hosting a quiet, respectful guest" (BANNED — vary naturally)
   - "outside of sports coaching, i studied film and play piano" (BANNED as a repetitive formula)
   - "all bulky gear is shipped ahead" / "yamato takkyubin" (BANNED)
   - "traveling light with a daypack" / "with only a daypack" (BANNED)
   - "fully independent during the day" (BANNED)
   - "historic neighborhoods" (BANNED — use natural language instead)
   - "tea or coffee" as an obligatory offer in every message (BANNED — vary naturally)
   - "cultural exchange" as a generic crutch (BANNED)
   - "won't crowd your space or routine" (BANNED)
   - "on my way north to hokkaido" / "heading to hokkaido" (BANNED)
   - "we both enjoy language learning" / "we both love language learning" (BANNED)

FEW-SHOT EXAMPLES OF THE TARGET QUALITY & TONE:

Example 1 (Host studies mathematics or planning travel):
"Hi Hidetaka,

Planning a round-the-world journey during a gap year sounds like a wonderful adventure. Having spent considerable time traveling independently, I know how exciting that preparation stage is.

I’m Andrey, a sports coach visiting Kyoto. Outside of coaching, I work in film and always enjoy chatting with people embarking on big journeys.

Let me know if you might have space to host a considerate guest. Either way, best of luck with your travels and studies!

Warmly,
Andrey"

Example 2 (Host interested in local culture or chatting with travelers):
"Hi Masafumi,

Love that you take time to chat with locals and learn about different cultures while traveling. That genuine curiosity is what makes Couchsurfing so special.

I’m Andrey, visiting Kyoto. Alongside sports coaching, I studied film directing, and observing local life and human stories has always been at the heart of my travels.

Would be glad to share travel stories or grab a bite if your schedule permits. Thanks for considering!

Cheers,
Andrey"

Example 3 (Host with empty or minimal bio, or unverified name):
"Hello!

I’m Andrey, visiting Kyoto for a few days to explore the city.

I’m reaching out to see if you might have room to host a quiet, considerate guest. I keep my space very clean and am deeply respectful of house rules and quiet hours.

No worries at all if the timing doesn't work out. Appreciate your time!

Best,
Andrey"
"""


def format_conversational_dates(check_in: str, check_out: str) -> str:
    """
    Преобразует машинные ISO даты (2026-11-18, 2026-11-20) в естественный разговорный английский:
    'Nov 18–20'.
    """
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


def validate_message_qc(
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


def clean_generated_message(text: str, date_str: str = "Nov 18–20", city: str = "Tokyo", include_dates: bool = True) -> str:
    """
    Детерминированная постобработка (Regex Sanitizer) и комплексная проверка стоп-листа:
    1. Жесткая замена em-dash (—) и en-dash (–) на запятые или стандартный дефис (-).
    2. Устранение галлюцинаций времени ('tonight' -> 'in the evening').
    3. Исключение машинных штампов и запрещенных шаблонов.
    4. Замена машинных ISO дат (2026-11-18) на разговорные ({date_str}) или удаление.
    5. Нормализация подписи 'Cheers, Andrey'.
    """
    if not text:
        return ""

    # 1. Удаление markdown code blocks
    text = re.sub(r"^```(?:markdown)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)

    # 1.1. Очистка непечатаемых символов и артефактов кодировки (\ufffd кракозябр)
    text = text.replace("\ufffd", "").replace("\x00", "")

    # 2. Удаление мета-вставок модели ("Here is a draft:", "Sure! Here's...")
    text = re.sub(
        r"^(?:Here(?:'s| is) (?:a |the )?(?:draft|message|stay request|response|concise|personalized|short)[^\n]*:?\s*)+",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"^(?:Sure!|Certainly!|Of course!|Here you go!)[^\n]*\n+",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"^Subject:[^\n]*\n+", "", text, flags=re.IGNORECASE)

    # 3. Убираем внешние кавычки
    text = text.strip().strip('"').strip("'").strip()

    # 4. ДЕТЕРМИНИРОВАННЫЙ САНИТАЙЗЕР ТИРЕ (EM-DASH "—" И EN-DASH "–")
    text = re.sub(r'(\d+)\s*[—–]\s*(\d+)', r'\1–\2', text)
    text = re.sub(r'\s*—\s*', ', ', text)
    text = text.replace("—", ", ")

    # 5. Замена ISO дат на разговорный формат (или удаление, если include_dates=False)
    if include_dates and date_str:
        text = re.sub(r"\b\d{4}-\d{2}-\d{2}(?:\s*(?:to|-|–)\s*\d{4}-\d{2}-\d{2})?\b", date_str, text)
    else:
        pat_dates = r"(?i)\b(?:from\s+|between\s+)?(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+\d{1,2}(?:\s*(?:to|[-–—]|and)\s*(?:[A-Za-z]+\s+)?\d{1,2})?(?:,\s*\d{4})?\b"
        text = re.sub(pat_dates, "", text)
        text = re.sub(r"\b\d{4}-\d{2}-\d{2}(?:\s*(?:to|-|–)\s*\d{4}-\d{2}-\d{2})?\b", "", text)
        text = re.sub(r"\b(?:from|between)\s+(?:,\s*|\.\s*|\s+)", " ", text, flags=re.IGNORECASE)
        text = re.sub(r"[ \t]+([,.?!])", r"\1", text)
        text = re.sub(r"[ \t]{2,}", " ", text)

    # 6. Жесткий Regex Safeguard для 'tonight' -> 'in the evening'
    text = re.sub(r"\btonight\b", "in the evening", text, flags=re.IGNORECASE)

    # 7. Санитизация строго запрещенных фраз
    text = re.sub(r"(?i)\bi'll be out in town exploring during the daytime,?\s*(?:so )?i won't crowd your space or routine\.?\s*", "", text)
    text = re.sub(r"(?i)\bi won't crowd your space or routine\.?\s*", "", text)
    text = re.sub(r"(?i)\blove (?:the|your) passion for\b", "Love that you're into", text)
    text = re.sub(r"(?i)\bexcited about (?:the|your)\b", "Intrigued by your", text)

    # Удаление и автозамена шаблонных открытий и машинных штампов
    text = re.sub(r"(?i)\bi noticed (?:that )?you (?:love|enjoy|like|are)\b", "Love that you're", text)
    text = re.sub(r"(?i)\bi noticed (?:that )?your\b", "Loved your", text)
    text = re.sub(r"(?i)\bi noticed (?:that )?", "", text)
    text = re.sub(r"(?i)\bi saw on your profile (?:that )?", "", text)
    text = re.sub(r"(?i)\baccording to your bio,?\s*", "", text)
    text = re.sub(r"(?i)\bi can see (?:that )?", "", text)
    text = re.sub(r"(?i)\bi see that (?:you )?", "", text)

    # "caught my eye" -> "stood out"
    text = re.sub(r"(?i)\b(?:caught my eye|your profile caught my eye)\b", "stood out to me", text)

    # Запрещенные упоминания стажа
    text = re.sub(r"(?i)\b(?:for )?(?:over |more than )?10\+?\s*years on (?:cs|couchsurfing)[,.]?\s*", "", text)
    text = re.sub(r"(?i)\b(?:over |more than )?a decade on (?:cs|couchsurfing)[,.]?\s*", "", text)
    text = re.sub(r"(?i)\bi know the drill[,.]?\s*", "", text)
    text = re.sub(r"(?i)\ba seasoned couchsurfer[,.]?\s*", "a considerate guest ", text)
    text = re.sub(r"(?i)\b55\+?\s*countries[,.]?\s*", "many countries ", text)

    # Запрещенные шаблонные фразы про рюкзак, доставку и независимость
    text = re.sub(r"(?i)\b(?:all )?bulky gear is shipped ahead (?:via yamato takkyubin(?: delivery)?|via takkyubin)?[,.]?\s*", "", text)
    text = re.sub(r"(?i)\bgear is shipped ahead (?:via yamato takkyubin(?: delivery)?|via takkyubin)?[,.]?\s*", "", text)
    text = re.sub(r"(?i)\b(?:yamato takkyubin|takkyubin)(?: delivery)?[,.]?\s*", "", text)
    text = re.sub(r"(?i)\b(?:traveling|travel)\s+(?:ultra-)?light\s+(?:with|carrying)\s+(?:just|only)?\s*(?:a|my)?\s*(?:small|lightweight)?\s*daypack[,.]?\s*", "", text)
    text = re.sub(r"(?i)\bwith (?:just|only) (?:a|my) (?:small|lightweight )?daypack[,.]?\s*", "", text)
    text = re.sub(r"(?i)\band am fully independent during the day[,.]?\s*", "", text)
    text = re.sub(r"(?i)\bfully independent during the day[,.]?\s*", "", text)
    text = re.sub(r"(?i)\bexplore historic neighborhoods and\b", "explore and", text)
    text = re.sub(r"(?i)\bhistoric neighborhoods\b", "local neighborhoods", text)

    # Заглушка
    text = re.sub(r"(?i)\bwould love to connect!?\s*", "", text)

    # Запрет и замена упоминаний Москвы и России (пользователь летит из Женевы, Швейцария)
    text = re.sub(r"(?i)\b(?:visiting|traveling|flying)\s+from\s+moscow\b", "flying from Geneva, Switzerland", text)
    text = re.sub(r"(?i)\b(?:visiting|traveling|flying)\s+from\s+russia\b", "flying from Geneva, Switzerland", text)
    text = re.sub(r"(?i)\bfrom\s+moscow\b", "from Geneva, Switzerland", text)
    text = re.sub(r"(?i)\bfrom\s+russia\b", "from Geneva, Switzerland", text)
    text = re.sub(r"(?i)\bin\s+moscow\b", "in Geneva", text)
    text = re.sub(r"(?i)\bin\s+russia\b", "in Switzerland", text)
    text = re.sub(r"(?i)\benglish,\s*french,?\s*and\s*russian\b", "English and French", text)
    text = re.sub(r"(?i)\band\s*native\s*russian\b", "", text)
    text = re.sub(r"(?i)\bnative\s*russian\b", "", text)
    text = re.sub(r"(?i)\brussian\s+cinema\b", "European cinema", text)
    text = re.sub(r"(?i)\brussian\b", "European", text)
    text = re.sub(r"(?i)\bmoscow\b", "Geneva", text)
    text = re.sub(r"(?i)\brussia\b", "Switzerland", text)
    text = re.sub(r"(?i)\bvgik\b", "film school", text)

    # "excited to" -> "looking forward to"
    text = re.sub(r"\bexcited to\b", "looking forward to", text, flags=re.IGNORECASE)

    # "fascinating" -> "inspiring"
    text = re.sub(r"\bfascinating\b", "inspiring", text, flags=re.IGNORECASE)

    # Замена шаблонных штампов
    text = re.sub(r"(?i)\byour passion for\b", "your interest in", text)
    text = re.sub(r"(?i)\bpassion for\b", "interest in", text)
    text = re.sub(r"(?i)\bwould love to cross paths if your timing aligns\.?\s*", "Would love to cross paths if you have space. ", text)
    text = re.sub(r"(?i)\bthanks for considering hosting a quiet, respectful guest!?\.?\s*", "Thanks for considering! ", text)

    # 8. Нормализация подписи (сохраняем Warmly, Cheers, Best)
    text = re.sub(
        r"(?i)\b(?:Sincerely|Regards),?\s*\n*\s*Andrey\b",
        "Warmly,\nAndrey",
        text,
    )

    # 9. Исправление заглавной буквы после удаления вводной фразы
    text = re.sub(r"(?:^|[.!?]\s+)([a-z])", lambda m: m.group(0).upper(), text)

    # 10. Очистка пунктуации и горизонтальных пробелов (сохраняя параграфы \n\n)
    text = re.sub(r',\s*,', ',', text)
    text = re.sub(r',\s*\.', '.', text)
    text = re.sub(r'[ \t]+([,?.!])', r'\1', text)
    text = re.sub(r'[ \t]{2,}', ' ', text)
    text = re.sub(r'\n[ \t]+', '\n', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = text.replace("—", ", ")

    # Если отсутствует подпись с именем Andrey, добавляем
    if not re.search(r"(?i)\b(?:Cheers|Warmly|Best),?\s*\n*\s*Andrey[.]?\b", text):
        if not text.endswith("Andrey") and not text.endswith("Andrey."):
            text += "\n\nWarmly,\nAndrey"

    return text.strip()


VISUAL_OBJECT_MAP = {
    "cat": ("animal", 0.95),
    "dog": ("animal", 0.95),
    "pet": ("animal", 0.90),
    "camera": ("photography", 0.95),
    "lens": ("photography", 0.90),
    "tripod": ("photography", 0.90),
    "guitar": ("music", 0.95),
    "piano": ("music", 0.95),
    "vinyl": ("music", 0.95),
    "instrument": ("music", 0.90),
    "koto": ("music", 0.95),
    "stadium": ("sports", 0.90),
    "soccer": ("sports", 0.95),
    "football": ("sports", 0.95),
    "futsal": ("sports", 0.95),
    "bicycle": ("sports", 0.90),
    "bike": ("sports", 0.90),
    "temple": ("architecture", 0.90),
    "shrine": ("architecture", 0.90),
    "restaurant": ("food", 0.85),
    "cafe": ("food", 0.85),
    "food": ("food", 0.85),
    "ramen": ("food", 0.90),
    "coffee": ("food", 0.85),
    "mountain": ("nature", 0.90),
    "hiking": ("nature", 0.90),
    "sea": ("nature", 0.90),
    "ocean": ("nature", 0.90),
    "beach": ("nature", 0.90),
}


def ensure_host_visual_observations(host_id: str, username: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Проверяет и обеспечивает наличие визуальных наблюдений LLaVA в базе данных:
    1. Если в visual_observations уже есть записи для данного host_id -> сразу возвращает их из SQLite (0 вызовов Ollama).
    2. Если наблюдений нет, ищет локальные фото в output/llava_photos/<username>_photo_*.jpg или <host_id>_photo_*.jpg.
    3. Если фото найдены и Ollama доступна, анализирует фото через LLaVA (llava:latest / llama3.2-vision),
       извлекает объекты и сохраняет в SQLite (кэширует навсегда).
    4. При отсутствии фото или недоступности модели -> безопасный fallback (возвращает пустой список).
    """
    if not host_id and not username:
        return []
    u = (username or host_id or "").strip()
    hid = (host_id or username or "").strip()

    from database import db_session
    try:
        with db_session() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT vo.observation, vo.category, vo.evidence_confidence, pa.local_path
                FROM visual_observations vo
                LEFT JOIN photo_assets pa ON vo.photo_asset_id = pa.id
                WHERE LOWER(vo.host_id) = LOWER(?) OR LOWER(vo.host_id) = LOWER(?)
                ORDER BY vo.evidence_confidence DESC;
            """, (hid, u))
            rows = cur.fetchall()
            if rows:
                return [
                    {
                        "observation": r["observation"],
                        "category": r["category"],
                        "confidence": float(r["evidence_confidence"] or 0.9),
                        "photo_path": r["local_path"],
                    }
                    for r in rows
                ]
    except Exception as e:
        logger.warning(f"Error querying visual_observations cache: {e}")

    # Поиск локальных фотографий на диске
    base_dir = Path(__file__).parent
    out_dir = Path(os.environ.get("CS_OUTPUT_DIR", str(base_dir / "output"))).resolve()
    photos_dir = out_dir / "llava_photos"
    if not photos_dir.is_dir():
        return []

    safe_u = re.sub(r"[^\w\-_]", "_", u)
    safe_hid = re.sub(r"[^\w\-_]", "_", hid)

    photo_files = []
    for pattern in [f"{safe_u}_photo_*.jpg", f"{safe_u}_photo_*.png", f"{safe_hid}_photo_*.jpg", f"{safe_hid}_photo_*.png"]:
        photo_files.extend(list(photos_dir.glob(pattern)))

    photo_files = list(dict.fromkeys(photo_files))
    if not photo_files:
        return []

    agent = get_llm_agent()
    if not agent.is_available():
        logger.info("Ollama is offline; visual analysis deferred.")
        return []

    vision_model = agent.select_best_model(has_images=True)
    new_observations: List[Dict[str, Any]] = []

    for p in photo_files[:3]:
        try:
            b64_img = agent._encode_image(p)
            if not b64_img:
                continue
            payload = {
                "model": vision_model,
                "prompt": (
                    "Analyze this image from a Couchsurfing host's profile. "
                    "Describe concisely: activities, hobbies, instruments, places, food, and sports."
                ),
                "stream": False,
                "images": [b64_img],
            }
            resp = requests.post(f"{agent.host}/api/generate", json=payload, timeout=45.0)
            if resp.status_code == 200:
                raw_text = resp.json().get("response", "")
                lower_text = raw_text.lower()

                found_for_photo = []
                for kw, (cat, conf) in VISUAL_OBJECT_MAP.items():
                    if kw in lower_text:
                        found_for_photo.append({"observation": kw, "category": cat, "confidence": conf})

                if found_for_photo:
                    import hashlib
                    with open(p, "rb") as f:
                        f_hash = hashlib.sha256(f.read()).hexdigest()
                    asset_id = f"pa_{f_hash[:16]}"
                    now_iso = datetime.now().isoformat()

                    with db_session() as conn:
                        cur = conn.cursor()
                        cur.execute("""
                            INSERT OR REPLACE INTO photo_assets (
                                id, image_hash, local_path, file_size, mime_type,
                                llava_raw_response, detected_objects_json, model, analysis_status,
                                analyzed_at, created_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'completed', ?, ?);
                        """, (
                            asset_id, f_hash, str(p), os.path.getsize(p),
                            "image/jpeg" if str(p).lower().endswith((".jpg", ".jpeg")) else "image/png",
                            raw_text[:800], json.dumps([x["observation"] for x in found_for_photo]),
                            vision_model, now_iso, now_iso
                        ))

                        for vo in found_for_photo:
                            obs_id = f"vo_{hid}_{asset_id[:8]}_{vo['observation']}"
                            cur.execute("""
                                INSERT OR REPLACE INTO visual_observations (
                                    id, host_id, photo_asset_id, observation, category,
                                    evidence_confidence, raw_description, created_at
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
                            """, (
                                obs_id, hid, asset_id, vo["observation"], vo["category"],
                                vo["confidence"], raw_text[:300], now_iso
                            ))
                            new_observations.append({
                                "observation": vo["observation"],
                                "category": vo["category"],
                                "confidence": vo["confidence"],
                                "photo_path": str(p),
                            })
        except Exception as err:
            logger.warning(f"Error during LLaVA analysis of {p}: {err}")

    return new_observations


class CouchsurfingLLM:
    """
    Класс интеграции с Gemini Flash и локальной Ollama для мультимодального анализа профилей
    и генерации персонализированных Couchsurfing сообщений с автоматическим fallback на rule-based.
    """

    def __init__(
        self,
        model: str = DEFAULT_TEXT_MODEL,
        host: str = DEFAULT_OLLAMA_HOST,
        timeout: float = 60.0,
    ):
        self.host = host.rstrip("/")
        self.model = model
        self.timeout = timeout
        self._available: Optional[bool] = None

    def get_gemini_api_key(self) -> Optional[str]:
        """Возвращает ключ API Gemini из окружения или настроек базы данных."""
        key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if key:
            return key.strip()
        try:
            from database import db_session
            with db_session() as conn:
                cur = conn.cursor()
                cur.execute("SELECT value FROM app_settings WHERE key = 'gemini_api_key';")
                row = cur.fetchone()
                if row and row[0]:
                    return row[0].strip()
        except Exception:
            pass
        return None

    def generate_via_gemini(
        self,
        prompt: str,
        system_prompt: str,
        images: Optional[List[str]] = None,
        model: Optional[str] = None,
    ) -> Optional[str]:
        """
        Генерация персонализированного обращения через Gemini Flash API (gemini-2.5-flash).
        Поддерживает:
        - Экспоненциальный бэкофф при HTTP 429 / 503
        - Маскирование ключей в логах
        - Защиту от бесконечных циклов
        - 30-секундный таймаут
        """
        api_key = self.get_gemini_api_key()
        if not api_key:
            return None

        active_model = model or os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
        endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{active_model}:generateContent"

        parts: List[Dict[str, Any]] = [{"text": prompt}]
        if images:
            for img_b64 in images[:2]:
                parts.append({
                    "inlineData": {
                        "mimeType": "image/jpeg",
                        "data": img_b64,
                    }
                })

        payload = {
            "systemInstruction": {
                "parts": [{"text": system_prompt.strip()}]
            },
            "contents": [
                {
                    "role": "user",
                    "parts": parts,
                }
            ],
            "generationConfig": {
                "temperature": 0.72,
                "topP": 0.9,
                "maxOutputTokens": 350,
            },
        }

        masked_key = f"{api_key[:4]}...{api_key[-4:]}" if len(api_key) > 8 else "***"
        logger.info(f"Запрос к Gemini Flash ({active_model}, key={masked_key})")

        backoff = 2.0
        for attempt in range(1, 4):
            try:
                resp = requests.post(
                    endpoint,
                    params={"key": api_key},
                    json=payload,
                    timeout=30.0,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    candidates = data.get("candidates", [])
                    if candidates:
                        parts_resp = candidates[0].get("content", {}).get("parts", [])
                        if parts_resp:
                            text_out = parts_resp[0].get("text", "").strip()
                            if text_out:
                                logger.info(f"Gemini Flash ({active_model}) успешно вернул ответ (попытка {attempt})")
                                return text_out
                elif resp.status_code in (429, 500, 503, 504):
                    logger.warning(
                        f"Gemini Flash вернул статус {resp.status_code} (попытка {attempt}/3). Ожидание {backoff}с перед ретраем..."
                    )
                    time.sleep(backoff)
                    backoff *= 2.0
                else:
                    logger.warning(
                        f"Gemini Flash вернул неожиданный статус {resp.status_code}: {resp.text[:200]}"
                    )
                    break
            except Exception as e:
                logger.warning(f"Ошибка вызова Gemini Flash (попытка {attempt}/3): {e}")
                time.sleep(backoff)
                backoff *= 2.0

        return None

    def is_available(self) -> bool:
        """Проверяет доступность сервера Ollama."""
        try:
            resp = requests.get(f"{self.host}/api/tags", timeout=3.0)
            return resp.status_code == 200
        except Exception:
            return False

    def get_available_models(self) -> List[str]:
        """Возвращает список установленных локально моделей."""
        try:
            resp = requests.get(f"{self.host}/api/tags", timeout=3.0)
            if resp.status_code == 200:
                data = resp.json()
                return [m["name"] for m in data.get("models", [])]
        except Exception as e:
            logger.warning(f"Failed to fetch models from Ollama: {e}")
        return []

    def select_best_model(self, has_images: bool = False) -> str:
        """Подбирает лучшую доступную модель (Vision при наличии изображений, иначе текстовую)."""
        installed = self.get_available_models()
        if not installed:
            return self.model

        # Если есть фото хоста, ищем Vision модель
        if has_images:
            for v_name in [
                "llama3.2-vision:11b",
                "llama3.2-vision:latest",
                "llava:latest",
                "llava:7b",
                "llava:13b",
                "minicpm-v:latest",
                "qwen2-vl:7b",
            ]:
                if any(v_name in m for m in installed):
                    return v_name

        # Для текстовой генерации отдаем предпочтение qwen2.5:7b или указанной модели
        for t_name in [self.model, "qwen2.5:7b", "llama3.2:3b", "llama3.1:8b"]:
            if any(t_name in m for m in installed):
                return t_name

        return installed[0]

    def _encode_image(self, image_path: Union[str, Path]) -> Optional[str]:
        """Кодирует файл изображения в base64 для отправки в Ollama."""
        p = Path(image_path)
        if not p.is_file():
            return None
        try:
            with open(p, "rb") as f:
                return base64.b64encode(f.read()).decode("utf-8")
        except Exception as e:
            logger.warning(f"Error encoding image {p}: {e}")
            return None

    def _prepare_prompt(
        self,
        host: Dict[str, Any],
        check_in: str,
        check_out: str,
        guest_context: Optional[str] = None,
        image_insights: Optional[str] = None,
    ) -> Tuple[str, str, str]:
        from message_generator import verify_host_name
        first_name, is_trusted = verify_host_name(host)
        greeting_target = first_name if is_trusted and first_name else "there"

        raw_loc = host.get("location")
        if isinstance(raw_loc, dict):
            location = raw_loc.get("locationDescription", {}).get("description") or raw_loc.get("slug") or "Tokyo"
        else:
            location = raw_loc or "Tokyo"

        city = host.get("city")
        if not city:
            loc_str = str(location).lower()
            if "sapporo" in loc_str:
                city = "Sapporo"
            elif "tokyo" in loc_str:
                city = "Tokyo"
            else:
                city = str(location).split(",")[0].strip() or "Tokyo"

        # Извлечение структурированных профильных данных и Host Dossier
        from profile_analyzer import get_profile_analyzer
        from database import get_host_dossier
        analyzer = get_profile_analyzer()
        details = analyzer.extract_host_profile_details(host)

        # Host Dossier integration (provenance & confidence)
        host_id = host.get("id") or host.get("username") or ""
        ensure_host_visual_observations(host_id, host.get("username"))
        dossier = get_host_dossier(host_id) if host_id else None
        
        dossier_interests = []
        dossier_visuals = []
        dossier_places = []
        used_facts = []

        if dossier:
            # 1. Interests (confirmed / high confidence only)
            for item in dossier.get("interests", []):
                conf = item.get("confidence", 0.5)
                status = item.get("status", "uncertain")
                name_i = item.get("name", "")
                if (status == "confirmed" or conf >= 0.8) and name_i:
                    dossier_interests.append(f"{name_i} (provenance: {item.get('source', 'text')})")
                    used_facts.append(f"interest:{name_i}")

            # 2. Visual observations (evidence confidence >= 0.85, limit 3)
            for vo in dossier.get("visual_observations", [])[:3]:
                obs = vo.get("observation", "")
                cat = vo.get("category", "")
                if obs:
                    dossier_visuals.append(f"{obs} ({cat})")
                    used_facts.append(f"visual:{obs}")

            # 3. Places
            for pl in dossier.get("places", [])[:2]:
                pl_name = pl.get("name", "")
                pl_type = pl.get("type", "mentioned")
                if pl_name:
                    dossier_places.append(f"{pl_name} ({pl_type})")
                    used_facts.append(f"place:{pl_name}")

        # Интеллектуальная сборка персонального контекста через Context Assembler
        from context_assembler import get_context_assembler
        assembler = get_context_assembler()
        trip_ctx = {
            "city": city,
            "arrival_date": check_in,
            "departure_date": check_out,
        }
        assembled = assembler.assemble_context(host, trip_context=trip_ctx)
        travel_info = assembled["travel_context"]
        city = travel_info.get("city") or city
        date_str = travel_info.get("dates_text") or format_conversational_dates(check_in, check_out)
        context_instructions = assembled["llm_instructions"]

        has_secret = host.get("secret_code_detected", False)
        secret_code = host.get("secret_code_hint", "")

        interests_list = details.get("host_interests", [])
        if dossier_interests:
            interests_list = list(dict.fromkeys(interests_list + [i.split(" (")[0] for i in dossier_interests]))
        interests_str = ", ".join(interests_list)
        bio_snippet = details["clean_bio"][:1200]
        if not bio_snippet.strip():
            bio_snippet = "Friendly local resident interested in cultural exchange and hosting world travelers."

        dossier_section = ""
        if dossier_visuals or dossier_places:
            dossier_section = f"""
HOST DOSSIER (CONFIRMED OBSERVATIONS & PLACES):
- Visual Observations from Photos: {', '.join(dossier_visuals) if dossier_visuals else 'None'}
- Associated Places: {', '.join(dossier_places) if dossier_places else 'None'}
(Note: Treat visual observations as interesting context, not absolute assertions. E.g. a camera indicates photography interest, not that they are a pro photographer.)
"""

        greeting_line = f"Address {first_name} warmly by first name (e.g. 'Hi {first_name},')." if (is_trusted and first_name) else "Use a polite universal greeting ('Hello!' or 'Hi there!'). DO NOT guess a name and DO NOT use a surname."

        bio_instruction = ""
        if len(bio_snippet.strip()) < 15 or bio_snippet.startswith("Friendly local resident"):
            bio_instruction = "\nSPECIAL NOTE: The host's bio is empty or minimal. DO NOT invent facts about the host. DO NOT claim they love hosting, traveling, or cultural exchange. Keep the message straightforward, polite, and grounded: introduce Andrey, state the reason for visiting Kyoto, and respectfully ask if they have space to host."

        prompt = f"""Write a Couchsurfing stay request to {first_name if is_trusted and first_name else 'the host'}.

HOST PROFILE:
- Verified First Name: {first_name if is_trusted and first_name else 'NOT VERIFIED (use neutral greeting)'}
- Location: {city}
- Focus & Specific Interests: {interests_str if interests_str else 'Not specified'}
- Occupation: {details["host_occupation"]}
- Languages: {", ".join(details["host_languages"])}
- Profile Bio:
\"\"\"
{bio_snippet}
\"\"\"{dossier_section}{bio_instruction}"""

        if has_secret and secret_code:
            prompt += f"\n- Secret Keyword / Rule: '{secret_code}' (incorporate this word naturally into the message)"

        if image_insights:
            prompt += f"\n- Photo Insights: {image_insights}"

        prompt += f"""

WRITE A CONCISE, NATURAL STAY REQUEST (TARGET LENGTH: 60 TO 90 WORDS IN 2-3 SHORT BREATHABLE PARAGRAPHS):
{context_instructions}

PARAGRAPH REQUIREMENTS:
1. Paragraph 1 (Greeting & Connection): {greeting_line}
   Connect naturally with a specific detail from the host's profile or Context Assembler. If the bio is empty, open politely and naturally without inventing passions for the host. Never use robotic openers like "I noticed" or "I saw on your profile" or "Your passion for X".
2. Paragraph 2 (Trip Context & Grounded Connection): Introduce Andrey naturally in connection to {city} and the relevant background from Context Assembler. State simply that you are visiting {city}. NEVER invent or mention travel routes beyond {city} (never say 'on my way to Hokkaido', 'heading north for skiing', etc.). Keep host facts and Andrey's background strictly separate — NEVER say 'We both enjoy [X]' or 'We both love [X]' unless the host explicitly stated that exact hobby. DO NOT list achievements (never say "55+ countries" or "10+ years on CS"). DO NOT repeat explicit calendar dates in the message body. DO NOT use boilerplate like "traveling light with a daypack" or "historic neighborhoods" or "fully independent during the day".
3. Paragraph 3 (Closing): Warm, natural, low-pressure closing. DO NOT make "tea or coffee" an obligatory offer. Vary the sign-off naturally (e.g. asking if they might have space to host, offering to share impressions or cooking, or wishing them well).
4. Sign-off: "Warmly, Andrey", "Cheers, Andrey", or "Best, Andrey".

STRICT NEGATIVE CONSTRAINTS (NEVER USE THESE PHRASES):
- NEVER repeat calendar dates in the text (BANNED — already in the card header!).
- NEVER write: "traveling light with a daypack" or "only a daypack" (BANNED).
- NEVER write: "fully independent during the day" (BANNED).
- NEVER write: "historic neighborhoods" (BANNED).
- NEVER write: "tea or coffee" as a standard closing formula (BANNED — vary naturally).
- NEVER write: "cultural exchange" as a generic crutch (BANNED).
- NEVER write: "I won't crowd your space or routine" (BANNED).
- NEVER write: "Hope you have a great season!" or "Love your passion for [X]!" or "Excited about your [X]!".
- NEVER boast or list achievements ("55+ countries", "10+ years CS").
- NEVER invent host facts or assume things not in their bio.
- NEVER invent or mention travel routes, onward destinations, or relocations not in the Trip Card (e.g. BANNED: 'on my way to Hokkaido', 'heading north for skiing', 'on my way up north for the winter season').
- NEVER claim ungrounded shared interests: NEVER write 'we both enjoy', 'we both love', 'we both share', 'we both like', 'fellow [X]' unless the host explicitly mentioned that exact activity/interest.
- NEVER conflate culture/travel with language learning. Keep host facts and your facts distinct.

Output ONLY the message text in 2-3 short, breathable paragraphs. Total length must be strictly between 60 and 90 words.
"""
        return prompt, city, date_str

    def generate_request(
        self,
        host: Dict[str, Any],
        check_in: str = DEFAULT_CHECK_IN,
        check_out: str = DEFAULT_CHECK_OUT,
        guest_context: Optional[str] = None,
        photo_paths: Optional[List[Union[str, Path]]] = None,
        image_insights: Optional[str] = None,
        include_dates: bool = False,
    ) -> str:
        """
        Генерирует развернутый, теплый персонализированный текст запроса через Ollama (85-120 слов).
        Параметры генерации: temperature 0.72, repeat_penalty 1.15, num_predict 260.
        Динамически считывает город и даты из метаданных хоста.
        """
        # Динамически берем даты из объекта хоста, если они там заданы
        if check_in == DEFAULT_CHECK_IN:
            check_in = host.get("check_in") or host.get("arrival_date") or DEFAULT_CHECK_IN
        if check_out == DEFAULT_CHECK_OUT:
            check_out = host.get("check_out") or host.get("departure_date") or DEFAULT_CHECK_OUT

        if not self.is_available():
            logger.info("Ollama is not responding, using rule-based fallback generator.")
            raw_fallback = generate_stay_request(
                host=host,
                check_in=check_in,
                check_out=check_out,
                guest_context=guest_context,
                include_dates=include_dates,
            )
            return clean_generated_message(raw_fallback, date_str="", city=host.get("city", "Tokyo"), include_dates=include_dates)

        images: List[str] = []
        if photo_paths:
            for p in photo_paths:
                b64 = self._encode_image(p)
                if b64:
                    images.append(b64)

        active_model = self.select_best_model(has_images=bool(images))

        user_prompt, city, date_str = self._prepare_prompt(
            host=host,
            check_in=check_in,
            check_out=check_out,
            guest_context=guest_context,
            image_insights=image_insights,
        )

        # 1. Попытка генерации через Gemini Flash при наличии API-ключа
        gemini_text = self.generate_via_gemini(
            prompt=user_prompt,
            system_prompt=SYSTEM_PROMPT,
            images=images,
        )
        if gemini_text:
            cleaned = clean_generated_message(gemini_text, date_str=date_str, city=city, include_dates=include_dates)
            words = len(cleaned.split())
            bio_words_count = len((host.get("about_me_clean") or host.get("about_me") or "").split())
            is_minimal_bio = bio_words_count < 25
            qc_ok, issues = validate_message_qc(cleaned, is_minimal_bio=is_minimal_bio, host_data=host, trip_city=city)
            min_limit = 30 if is_minimal_bio else 55
            max_limit = 70 if is_minimal_bio else 130
            if qc_ok and min_limit <= words <= max_limit:
                logger.info(f"Gemini Flash генерация успешно прошла QC ({words} слов)")
                return cleaned
            else:
                logger.warning(f"Текст Gemini Flash не прошел QC ({words} слов: {issues}), переход к Ollama/fallback.")

        # 2. Попытка генерации через Ollama (если доступна)
        payload: Dict[str, Any] = {
            "model": active_model,
            "prompt": user_prompt,
            "system": SYSTEM_PROMPT.strip(),
            "stream": False,
            "options": {
                "temperature": 0.72,
                "top_p": 0.9,
                "repeat_penalty": 1.15,
                "num_predict": 260,
            },
        }

        if images:
            payload["images"] = images

        # Авто-ретрай при сбое QC или объеме вне 75-130 слов
        last_cleaned = ""
        current_prompt = user_prompt
        for attempt in range(1, 4):
            temp = 0.72 if attempt == 1 else 0.78
            payload["options"]["temperature"] = temp
            payload["prompt"] = current_prompt

            try:
                resp = requests.post(
                    f"{self.host}/api/generate",
                    json=payload,
                    timeout=self.timeout,
                )
                if resp.status_code == 200:
                    raw_result = resp.json().get("response", "").strip()
                    if raw_result:
                        cleaned = clean_generated_message(raw_result, date_str=date_str, city=city, include_dates=include_dates)
                        words = len(cleaned.split())
                        bio_words_count = len((host.get("about_me_clean") or host.get("about_me") or "").split())
                        is_minimal_bio = bio_words_count < 25
                        qc_ok, issues = validate_message_qc(cleaned, is_minimal_bio=is_minimal_bio, host_data=host, trip_city=city)
                        last_cleaned = cleaned

                        min_limit = 30 if is_minimal_bio else 55
                        max_limit = 70 if is_minimal_bio else 130
                        if words < min_limit or words > max_limit or "would love to connect!" in cleaned.lower() or not qc_ok:
                            logger.warning(
                                f"Ollama generation attempt {attempt} failed QC ({words} words): {issues}. Retrying..."
                            )
                            current_prompt = (
                                user_prompt
                                + f"\n\nCRITICAL RETRY INSTRUCTION: Previous draft had issues: {issues}. Write strictly between 60 and 90 words across 2-3 short, breathable paragraphs, warm, natural, and personal, with no banned cliches."
                            )
                            continue

                        logger.info(f"Ollama generation attempt {attempt} passed QC ({words} words)")
                        return cleaned
                logger.warning(f"Ollama returned status {resp.status_code}: {resp.text}")
            except Exception as e:
                logger.warning(f"Ollama generation error on attempt {attempt}: {e}")

        if last_cleaned and 50 <= len(last_cleaned.split()) <= 130 and "would love to connect!" not in last_cleaned.lower():
            return last_cleaned

        # Фолбэк на надежный генератор шаблонов
        raw_fallback = generate_stay_request(
            host=host,
            check_in=check_in,
            check_out=check_out,
            guest_context=guest_context,
            include_dates=False,
        )
        return clean_generated_message(raw_fallback, date_str="", city=city, include_dates=False)

    def generate_first_request(
        self,
        host: Dict[str, Any],
        check_in: str = DEFAULT_CHECK_IN,
        check_out: str = DEFAULT_CHECK_OUT,
        guest_context: Optional[str] = None,
        photo_paths: Optional[List[Union[str, Path]]] = None,
        image_insights: Optional[str] = None,
    ) -> str:
        """Алиас для generate_request с поддержкой динамических дат и городов."""
        return self.generate_request(
            host=host,
            check_in=check_in,
            check_out=check_out,
            guest_context=guest_context,
            photo_paths=photo_paths,
            image_insights=image_insights,
        )


def generate_first_request(
    host: Dict[str, Any],
    check_in: str = DEFAULT_CHECK_IN,
    check_out: str = DEFAULT_CHECK_OUT,
    guest_context: Optional[str] = None,
    photo_paths: Optional[List[Union[str, Path]]] = None,
    image_insights: Optional[str] = None,
    model: str = DEFAULT_TEXT_MODEL,
) -> str:
    """Глобальная функция генерации первого запроса через Ollama."""
    agent = get_llm_agent(model=model)
    return agent.generate_request(
        host=host,
        check_in=check_in,
        check_out=check_out,
        guest_context=guest_context,
        photo_paths=photo_paths,
        image_insights=image_insights,
    )


# Синглтон для повторного использования
_llm_instance: Optional[CouchsurfingLLM] = None


def get_llm_agent(model: str = DEFAULT_TEXT_MODEL) -> CouchsurfingLLM:
    """Возвращает глобальный экземпляр CouchsurfingLLM."""
    global _llm_instance
    if _llm_instance is None or _llm_instance.model != model:
        _llm_instance = CouchsurfingLLM(model=model)
    return _llm_instance


INBOX_SYSTEM_PROMPT = """You are Andrey, an easygoing, friendly, and respectful Couchsurfer and ski instructor.
You are replying to a Couchsurfing host in Tokyo who just replied to your stay request for Nov 18–20.

CORE PRINCIPLES:
1. READ WHAT THEY ACTUALLY WROTE:
   - If they offer to hang out, grab a drink, coffee, tea, or beer: react enthusiastically and accept!
   - If they mention a specific location, neighborhood, or museum (e.g., Kagurazaka, bar, exhibit): acknowledge it warmly.
   - If they mention medical issues, surgery, or hospital: show genuine human empathy, wish them a smooth procedure and speedy recovery. NEVER mention ski season or skiing to someone who is having surgery or going to the hospital!
   - If they can only host/meet in the evening: reassure them that you are very flexible and will work around their schedule.
2. TONE & STYLE:
   - Casual, warm, human, and conversational.
   - NOT robotic, corporate, or overly formal. Avoid cliché customer-service openings like "Thank you for reaching out to me".
   - Keep replies concise: strictly 2–4 sentences (unless short/brief tone is selected).
   - Sign off naturally: "Cheers, Andrey" or "Best, Andrey".
3. CONTEXT RECALL:
   - You travel light with a small backpack.
   - You are self-sufficient and respectful of their personal time."""


def classify_host_response(
    host_message: str,
    model: str = DEFAULT_TEXT_MODEL,
    timeout: int = 25,
) -> Dict[str, Any]:
    """
    Классифицирует ответ хоста Couchsurfing через Ollama qwen2.5:7b на дифференцированные категории:
    1. WELCOMING / MEETUP OFFER: хост согласился принять ИЛИ предлагает встретиться / выпить кофе/чай/пиво / сходить в бар/музей
    2. POLITE DECLINE WITH REASON: хост вежливо отказал по личной/медицинской причине (операция, больница, отъезд)
    3. COLD DECLINE: просто отказ без предложения встреч и без критических причин
    4. QUESTION: хост задает конкретный вопрос (время прибытия, планы, снаряжение)
    5. DECLINED_SYSTEM: автоматический отказ платформы
    """
    msg_clean = (host_message or "").strip()
    if not msg_clean:
        return {"category": "DECLINED_SYSTEM", "reason": "Empty message body", "confidence": 0.9}

    lower = msg_clean.lower().replace("’", "'").replace("‘", "'")

    # Быстрый детект системных сообщений
    if any(s in lower for s in ("host declined", "request expired", "stay request declined", "request was declined")):
        return {"category": "DECLINED_SYSTEM", "reason": "System decline phrase detected", "confidence": 1.0}

    # Быстрый детект медицинских причин (операция, больница)
    if any(m in lower for m in ("surgery", "hospital", "operation", "medical", "treatment", "doctor", "injured", "injury")):
        return {"category": "POLITE DECLINE WITH REASON", "reason": "Medical / surgery reason detected", "confidence": 0.98}

    # Детект отрицания встреч / отсутствия времени (не давать ложного Meetup Offer)
    has_neg_meetup = bool(
        re.search(r"(unable|not able|cannot|can't|won't be able|won't have time|no time|won't time).{0,40}(hang out|meet|catch up)", lower) or 
        re.search(r"(meet up|hang out).{0,40}(won't|cannot|can't|no time|won't time)", lower)
    )

    # Быстрый детект встреч / прогулок / баров / выпивки (Meetup offer)
    has_pos_meetup = any(k in lower for k in (
        "grab a beer", "grab some tea", "grab a drink", "have a drink", "grab tea or a beer",
        "grab tea", "café-bar", "cafe-bar", "let's have a drink", "let's grab", "happy to see you",
        "see you in the evening", "recommend this museum", "kagurazaka"
    )) or (("meet up" in lower or "hang out" in lower) and not has_neg_meetup)

    if has_pos_meetup and not has_neg_meetup:
        return {"category": "WELCOMING / MEETUP OFFER", "reason": "Meetup or hangout offer detected", "confidence": 0.95}

    if has_neg_meetup or any(k in lower for k in ("unable to host", "can't host", "cannot host", "won't be able to host", "no space")):
        return {"category": "COLD DECLINE", "reason": "Host politely declined without meetup offer", "confidence": 0.95}

    prompt = f"""You are an expert assistant managing Couchsurfing messages.
Analyze the following reply from a Couchsurfing host and classify it into EXACTLY ONE of these categories:

1. "WELCOMING / MEETUP OFFER": Host accepts hosting OR cannot host overnight but warmly offers to meet up, hang out, grab tea/coffee/beer, visit a bar/museum, or explore.
2. "POLITE DECLINE WITH REASON": Host politely declines due to a personal situation (e.g. hospital, surgery, medical issues, traveling away, family visiting).
3. "COLD DECLINE": Host simply declines without an offer to meet up or a sensitive medical/personal reason.
4. "QUESTION": Host asks questions about arrival time, schedule, baggage, ski plans, etc.
5. "DECLINED_SYSTEM": Automated platform cancellation or system rejection.

Host's message:
\"\"\"{msg_clean}\"\"\"

Return ONLY a valid JSON object in this exact format:
{{"category": "WELCOMING / MEETUP OFFER" | "POLITE DECLINE WITH REASON" | "COLD DECLINE" | "QUESTION" | "DECLINED_SYSTEM", "reason": "brief explanation"}}
"""

    payload = {
        "model": model,
        "prompt": prompt,
        "system": INBOX_SYSTEM_PROMPT,
        "stream": False,
        "format": "json",
        "options": {
            "temperature": 0.1,
            "num_predict": 100,
        },
    }

    try:
        resp = requests.post(
            f"{DEFAULT_OLLAMA_HOST}/api/generate",
            json=payload,
            timeout=timeout,
        )
        if resp.status_code == 200:
            raw_text = resp.json().get("response", "").strip()
            parsed = json.loads(raw_text)
            cat = str(parsed.get("category", "")).strip()
            valid_cats = (
                "WELCOMING / MEETUP OFFER", "POLITE DECLINE WITH REASON", 
                "COLD DECLINE", "QUESTION", "DECLINED_SYSTEM",
                "ACCEPTED", "CHAT_INTERESTED", "DECLINED_POLITE"
            )
            for vc in valid_cats:
                if cat.upper() == vc.upper():
                    return {
                        "category": vc,
                        "reason": parsed.get("reason", "LLM classified"),
                        "confidence": 0.95,
                    }
    except Exception as e:
        logger.warning(f"Ollama response classification failed: {e}")

    # Надежный rule-based fallback
    if any(k in lower for m in ("surgery", "hospital", "operation") if m in lower):
        return {"category": "POLITE DECLINE WITH REASON", "reason": "Rule-based medical decline", "confidence": 0.95}
    if any(k in lower for k in ("grab", "drink", "coffee", "tea", "beer", "meet up", "hang out", "see you")):
        return {"category": "WELCOMING / MEETUP OFFER", "reason": "Rule-based meetup offer", "confidence": 0.9}
    if any(k in lower for k in ("sorry", "cannot", "can't host", "unable to host", "won't be able", "busy")):
        return {"category": "COLD DECLINE", "reason": "Rule-based cold decline", "confidence": 0.9}
    if "?" in msg_clean:
        return {"category": "QUESTION", "reason": "Rule-based question detection", "confidence": 0.85}

    return {"category": "WELCOMING / MEETUP OFFER", "reason": "Rule-based welcoming fallback", "confidence": 0.7}


def generate_inbox_reply(
    host_name: str,
    host_message: str,
    category: Optional[str] = None,
    my_context: Optional[str] = None,
    tone: str = "casual",
    model: str = DEFAULT_TEXT_MODEL,
    timeout: int = 40,
) -> str:
    """
    Генерирует умный контекстный персонализированный ответ на входящее сообщение от хоста через Ollama qwen2.5:7b.
    Строго следует Core Principles:
    - Считывает реальный контекст хоста (напитки, чай/пиво, локации Kagurazaka, музей, операция/больница)
    - Никогда не упоминает лыжи/сезон хосту, которому предстоит операция
    - Соглашается на вечернюю встречу и проявляет гибкость
    - Поддерживает тональности: casual, short, detailed, thanks
    """
    h_name = (host_name or "there").strip()
    # Очищаем фамилию/инициалы для обращения по первому имени
    first_name = h_name.split()[0] if h_name else "there"
    msg_clean = (host_message or "").strip()
    ctx = (my_context or (
        "Andrey, sports coach and traveler. "
        "Arriving in Tokyo Nov 18 (~5 PM), departing Nov 20 morning. "
        "Travel light with small backpack, minimal footprint, "
        "easygoing, completely flexible with host's schedule and timing."
    )).strip()
    lower_msg = msg_clean.lower().replace("’", "'").replace("‘", "'")
    tone_str = (tone or "casual").strip().lower()

    if tone_str in ("short", "короткий", "brief"):
        tone_key = "short"
    elif tone_str in ("detailed", "подробный", "long", "full"):
        tone_key = "detailed"
    elif tone_str in ("thanks", "gratitude", "благодарность", "thank"):
        tone_key = "thanks"
    else:
        tone_key = "casual"

    # Детекция ключевых контекстов
    has_medical = any(k in lower_msg for k in ("surgery", "hospital", "operation", "medical", "treatment", "doctor", "health", "recover"))
    has_neg_meetup = bool(
        re.search(r"(unable|not able|cannot|can't|won't be able|won't have time|no time|won't time).{0,40}(hang out|meet|catch up)", lower_msg) or 
        re.search(r"(meet up|hang out).{0,40}(won't|cannot|can't|no time|won't time)", lower_msg)
    )
    has_drinks_or_meetup = (not has_neg_meetup) and (
        any(k in lower_msg for k in (
            "grab a beer", "grab some tea", "grab a drink", "have a drink", "grab tea or a beer",
            "grab tea", "café-bar", "cafe-bar", "let's have a drink", "let's grab", "happy to see you in the evening",
            "see you in the evening", "recommend this museum", "kagurazaka"
        )) or (("meet up" in lower_msg or "hang out" in lower_msg) and not has_neg_meetup)
    )
    is_declined = (category in ("COLD DECLINE", "POLITE DECLINE WITH REASON", "DECLINED_POLITE", "DECLINED_SYSTEM")) or any(
        k in lower_msg for k in ("unable to host", "can't host", "cannot host", "won't be able to host", "unfortunately", "i'm unable", "no space")
    )

    # -------------------------------------------------------------
    # СЦЕНАРИЙ 1: Медицинские причины / Операция / Больница (Kumi T.)
    # -------------------------------------------------------------
    if has_medical or category == "POLITE DECLINE WITH REASON":
        user_prompt = f"""Host Name: {first_name}
Incoming message from host:
\"\"\"{msg_clean}\"\"\"

CRITICAL INSTRUCTIONS:
The host mentioned they are going to the hospital for surgery or medical reasons.
Show warm, genuine human empathy. Wish them the smoothest procedure and a fast, comfortable recovery. Reassure them not to worry at all about hosting, their health comes first.
NEVER mention skiing, snow, Hokkaido, or sports to someone having surgery!
Length: strictly 2-3 sentences.
Output ONLY the message text. Sign off: Best, Andrey"""
        num_predict = 100

    # -------------------------------------------------------------
    # СЦЕНАРИЙ 2: Встреча / Чай / Пиво / Кафе-бар / Кагуразака (Sho, Riko, Kengo)
    # -------------------------------------------------------------
    elif (has_drinks_or_meetup or category in ("WELCOMING / MEETUP OFFER", "WELCOMING", "CHAT_INTERESTED", "ACCEPTED")) and not has_neg_meetup:
        specific_hints = []
        if "kagurazaka" in lower_msg or "evening" in lower_msg:
            specific_hints.append("Host suggested an evening meetup / Kagurazaka. Accept enthusiastically and confirm you are very flexible with time.")
        if "museum" in lower_msg or "café-bar" in lower_msg or "bar" in lower_msg or "friend" in lower_msg:
            specific_hints.append("Host recommended a museum and invited you to their friend's café-bar. Thank them for the recommendations and accept warmly!")
        if "tea" in lower_msg or "beer" in lower_msg:
            specific_hints.append("Host offered to grab tea or beer if they find time. Accept enthusiastically and keep it low-pressure around their availability.")

        hints_str = " ".join(specific_hints) if specific_hints else "Accept warmly and casually."

        if tone_key == "short":
            user_prompt = f"""Host Name: {first_name}
Incoming message from host:
\"\"\"{msg_clean}\"\"\"

Instructions:
{hints_str}
Write a crisp 1-2 sentence response (strictly 15 to 25 words). Casual, low-pressure.
Output ONLY the message text. Sign off: Cheers, Andrey"""
            num_predict = 60
        else:
            user_prompt = f"""Host Name: {first_name}
Incoming message from host:
\"\"\"{msg_clean}\"\"\"

My Context:
{ctx}

Instructions:
{hints_str}
Write a warm, natural, friendly Couchsurfing reply (strictly 2-3 sentences).
No corporate fluff. Keep it relaxed and flexible.
Output ONLY the message text. Sign off: Cheers, Andrey"""
            num_predict = 120

    # -------------------------------------------------------------
    # СЦЕНАРИЙ 3: Обычный отказ без предложений встреч (Cold decline - Fuka, Stani)
    # -------------------------------------------------------------
    elif is_declined:
        has_snow = any(k in lower_msg for k in ("snow", "season", "tomamu", "ski", "skier"))
        if has_snow:
            user_prompt = f"""Host Name: {first_name}
Incoming message from host:
\"\"\"{msg_clean}\"\"\"

Instructions:
The host declined but kindly wished good stay/snow.
Write a concise, warm sign-off (1-2 sentences): thank them for letting you know and wish them a great season too.
Output ONLY the message text. Sign off: Cheers, Andrey"""
        else:
            user_prompt = f"""Host Name: {first_name}
Incoming message from host:
\"\"\"{msg_clean}\"\"\"

Instructions:
The host politely declined.
Write a concise acknowledgement (1 sentence, e.g. "Thanks for letting me know, {first_name}! Have a great week ahead!").
Output ONLY the message text. Sign off: Cheers, Andrey"""
        num_predict = 60

    # -------------------------------------------------------------
    # СЦЕНАРИЙ 4: Ответы на вопросы хоста (Question / Plans)
    # -------------------------------------------------------------
    else:
        user_prompt = f"""Host Name: {first_name}
Incoming message from host:
\"\"\"{msg_clean}\"\"\"

My Context:
{ctx}

Instructions:
Answer their questions directly and warmly (arriving Nov 18 ~5 PM, traveling light, heading to Tomamu).
Strictly 2-3 sentences.
Output ONLY the message text. Sign off: Cheers, Andrey"""
        num_predict = 110

    payload = {
        "model": model,
        "system": INBOX_SYSTEM_PROMPT,
        "prompt": user_prompt,
        "stream": False,
        "options": {
            "temperature": 0.5 if (has_medical or is_declined) else 0.7,
            "top_p": 0.9,
            "num_predict": num_predict,
        },
    }

    try:
        resp = requests.post(
            f"{DEFAULT_OLLAMA_HOST}/api/generate",
            json=payload,
            timeout=timeout,
        )
        if resp.status_code == 200:
            result = resp.json().get("response", "").strip()
            if result:
                result = re.sub(r'^[„"\'«]+|[”"\'»]+$', '', result).strip()
                result = result.replace("—", ", ").replace("–", "-")
                if not result.endswith("Andrey") and not result.endswith("Andrey."):
                    result += "\n\nCheers, Andrey"
                return result
    except Exception as e:
        logger.warning(f"Ollama inbox reply generation error: {e}")

    # Надежные контекстные фолбэки при сбое модели
    if has_medical or category == "POLITE DECLINE WITH REASON":
        fallback = f"I'm really sorry to hear about your surgery, {first_name}. Wishing you the smoothest procedure and a fast, easy recovery! Please take good care of yourself and don't worry about hosting at all.\n\nBest, Andrey"
    elif "kagurazaka" in lower_msg or ("evening" in lower_msg and has_drinks_or_meetup):
        fallback = f"That sounds fantastic, {first_name}! I'd love to explore Kagurazaka with you in the evening. Completely flexible to work around your schedule!\n\nCheers, Andrey"
    elif "museum" in lower_msg or "café-bar" in lower_msg or "cafe-bar" in lower_msg:
        fallback = f"Thanks so much for the museum tip and recommendations, {first_name}! I would love to grab a drink at your friend's café-bar and experience the nostalgic feel.\n\nCheers, Andrey"
    elif "tea" in lower_msg or "beer" in lower_msg:
        fallback = f"That sounds great, {first_name}! I'd love to grab a tea or beer whenever you have some free time. Very flexible!\n\nCheers, Andrey"
    elif is_declined:
        if any(k in lower_msg for k in ("snow", "season", "tomamu")):
            fallback = f"Thanks for letting me know, {first_name}! Hope you have a wonderful season as well!\n\nCheers, Andrey"
        else:
            fallback = f"Thanks for letting me know, {first_name}! Have a great week ahead!\n\nCheers, Andrey"
    else:
        fallback = f"Hi {first_name}! Arriving Nov 18 around 5 PM, traveling light with minimal luggage and very flexible with your timing. Looking forward to connecting!\n\nCheers, Andrey"

    return fallback.replace("—", ", ").replace("–", "-")
