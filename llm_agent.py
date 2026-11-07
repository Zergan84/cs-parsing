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
    "Andrey, ski coach heading to Club Med Tomamu (Hokkaido) for the winter season, "
    "stopping in Tokyo for 2 days (Nov 18–20). Quiet, tidy, out exploring during the day."
)

SYSTEM_PROMPT = """You are Andrey, an independent, seasoned Couchsurfer (10+ years) and professional ski instructor.
You are writing a thoughtful, warm, and detailed one-time stay request to a Couchsurfing host in Japan.

YOUR PERSONALITY & STORY (weave naturally):
- Lifelong traveler with 55+ countries visited, deeply valuing real human connection across massive contrasts — from fast-paced hubs like NYC and Hong Kong to remote mountain villages in Nepal and rural Cambodia.
- Professional ski instructor heading up to Hokkaido (Club Med Tomamu) for the winter season.
- Passionate about local cultures, photography, cooking authentic meals, and deep conversations.
- Extremely considerate guest: bulky winter luggage and skis are shipped directly to Hokkaido via Yamato Takkyubin, so you travel with only a lightweight daypack. Fully independent during the day, deeply respectful of house rules and quiet hours.

WRITING PRINCIPLES:
1. SHOW GENUINE CARE & SUPPORT:
   Read the host's passions (travel, arts, cooking, cinema, nature, sports, community). Validate what they do! Show that you truly care about their world and see the human behind the profile.
2. EXPANDED BUT CRISP LENGTH:
   Aim for 85 to 120 words across 3 short, breathable paragraphs. Never write walls of text; keep it conversational, warm, and articulate.
3. ABSOLUTE BAN:
   NEVER use robotic clichés like "I won't crowd your space or routine", "Hope you have a great season!", or "Love your passion for X". Speak like an appreciative, mature traveler.

FEW-SHOT EXAMPLES OF THE TARGET LEVEL:

Example 1 (Host loves traveling & visited 30+ countries):
"Hi Yuka,

36 countries is a wonderful milestone! Traveling that extensively really shifts how you see the world — what’s been the one place that surprised you the most so far?

I’ve been fortunate to explore around 55 countries myself, and those wild contrasts — between buzzing megalopolises like NYC and the quiet serenity of Nepal — made me truly value how Couchsurfing connects kindred spirits. I’m Andrey, a ski coach passing through Tokyo Nov 16–19 before heading north to Hokkaido for the winter season at Club Med Tomamu. 

I travel ultra-light with just a small daypack since my winter gear is shipped ahead. I’d love to cross paths, support your favorite neighborhood spots, and trade travel memories over tea or a drink if our schedules align!

Warmly,
Andrey"

Example 2 (Host loves cooking, neighborhood culture, quiet cozy lifestyle):
"Hi Kenta,

Your philosophy around home-cooked meals and exploring quiet neighborhood markets completely resonates with me. There’s something special about food bringing people together across cultures.

I’m Andrey, a ski instructor spending a couple of days in Tokyo from Nov 16 to 19 before kicking off the winter coaching season in Hokkaido. Having traveled through 55+ countries, I always find that simple moments — like sharing a home recipe or hearing someone's local perspective — are the highlights of any journey. 

I travel completely mobile with just a daypack (my ski gear is already forwarded to Tomamu), and I always adapt effortlessly to my host's rhythm. Would be wonderful to share a casual dinner or tea and swap stories!

Cheers,
Andrey"

Example 3 (Host loves arts, indie cinema, or creative projects):
"Hey Daisuke,

Really enjoyed reading your thoughts on indie cinema and creative spaces in Tokyo. It's inspiring to see people actively nurturing local arts and staying curious about different storytelling styles.

I’m Andrey, heading to Club Med Tomamu in Hokkaido for the ski season, passing through Tokyo Nov 16–19. Outside coaching on snow, I've spent years traveling through 55 countries — from street photography in Hong Kong to mountain life in Nepal — and I deeply appreciate exchanging ideas with people immersed in their creative craft.

My luggage is forwarded ahead, so I only carry a daypack and stay very flexible. Would love to buy you a coffee or drink and chat about Tokyo’s hidden creative corners if you have some free time!

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


def validate_message_qc(text: str) -> Tuple[bool, List[str]]:
    """
    Контроль качества (QC Validator):
    1. Длина строго в диапазоне 75-130 слов (целевой диапазон: 85-120 слов).
    2. Отсутствие шаблонных копипаст.
    3. Отсутствие строго запрещенных фраз (BANNED):
       - 'won't crowd your space', 'crowd your space', 'crowd your routine'
       - 'love your passion for', 'excited about your', 'hope you have a great season'
       - 'sharing travel stories in the evening', 'grabbing a quick coffee and sharing travel stories'
       - 'I noticed', 'I saw on your profile', 'According to your bio', 'caught my eye'
       - 'tonight'
       - em-dashes ('—')
       - робо-дат в формате ISO (2026-11-18)
    """
    if not text:
        return False, ["Empty text"]

    words = len(text.strip().split())
    issues = []

    if words < 75:
        issues.append(f"Too short ({words} words, minimum 75)")
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
    ]
    for b in banned_phrases:
        if b in lower:
            issues.append(f"Contains forbidden cliche: '{b}'")

    if "—" in text:
        issues.append("Contains forbidden em-dash ('—')")

    if re.search(r"\b\d{4}-\d{2}-\d{2}\b", text):
        issues.append("Contains robotic ISO date format")

    return len(issues) == 0, issues


def clean_generated_message(text: str, date_str: str = "Nov 18–20", city: str = "Tokyo") -> str:
    """
    Детерминированная постобработка (Regex Sanitizer) и комплексная проверка стоп-листа:
    1. Жесткая замена em-dash (—) и en-dash (–) на запятые или стандартный дефис (-).
    2. Устранение галлюцинаций времени ('tonight' -> 'in the evening').
    3. Исключение машинных штампов и запрещенных шаблонов.
    4. Замена машинных ISO дат (2026-11-18) на разговорные ({date_str}).
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

    # 5. Замена ISO дат на разговорный формат
    text = re.sub(r"\b\d{4}-\d{2}-\d{2}(?:\s*(?:to|-|–)\s*\d{4}-\d{2}-\d{2})?\b", date_str, text)

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

    # Заглушка
    text = re.sub(r"(?i)\bwould love to connect!?\s*", "", text)

    # "excited to" -> "looking forward to"
    text = re.sub(r"\bexcited to\b", "looking forward to", text, flags=re.IGNORECASE)

    # "fascinating" -> "inspiring"
    text = re.sub(r"\bfascinating\b", "inspiring", text, flags=re.IGNORECASE)

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


class CouchsurfingLLM:
    """
    Класс интеграции с локальной Ollama для мультимодального анализа профилей
    и генерации персонализированных Couchsurfing сообщений.
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
        from message_generator import extract_greeting_name
        first_name = extract_greeting_name(host)

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

        # Извлечение структурированных профильных данных
        from profile_analyzer import get_profile_analyzer
        analyzer = get_profile_analyzer()
        details = analyzer.extract_host_profile_details(host)

        date_str = format_conversational_dates(check_in, check_out)
        has_secret = host.get("secret_code_detected", False)
        secret_code = host.get("secret_code_hint", "")

        interests_str = ", ".join(details["host_interests"])
        bio_snippet = details["clean_bio"][:1200]
        if not bio_snippet.strip():
            bio_snippet = "Friendly local resident interested in cultural exchange and hosting world travelers."

        prompt = f"""Write a Couchsurfing stay request to {first_name}.

HOST PROFILE:
- Name: {first_name}
- Location: {city}
- Focus & Specific Interests: {interests_str}
- Occupation: {details["host_occupation"]}
- Languages: {", ".join(details["host_languages"])}
- Profile Bio:
\"\"\"
{bio_snippet}
\"\"\""""

        if has_secret and secret_code:
            prompt += f"\n- Secret Keyword / Rule: '{secret_code}' (incorporate this word naturally into the message)"

        if image_insights:
            prompt += f"\n- Photo Insights: {image_insights}"

        prompt += f"""

WRITE A THOUGHTFUL, DETAILED STAY REQUEST (TARGET LENGTH: 85 TO 120 WORDS IN 3 SHORT BREATHABLE PARAGRAPHS):
1. Paragraph 1 (Genuine Care & Connection): Address {first_name} warmly. Reference their specific passions, philosophy, or lifestyle ({interests_str.split(',')[0] if interests_str else 'their bio and perspectives'}). Show genuine appreciation and validate what they do. Ask an engaging question or make a relatable observation. Never use robotic openers like "I noticed" or "I saw on your profile".
2. Paragraph 2 (Trip Context & Contrast): Naturally introduce yourself as Andrey, a ski coach/instructor passing through {city} {date_str} on the way to Club Med Tomamu in Hokkaido for the winter. Weave in your background traveling through 55+ countries (massive contrasts like NYC/Hong Kong vs mountain villages in Nepal/Cambodia) and why Couchsurfing connects kindred spirits. Mention casually that all bulky winter luggage/skis are shipped ahead via Yamato Takkyubin, so you travel light with only a daypack and easily adapt to their rhythm.
3. Paragraph 3 (The Vibe & Closing): Warm, low-pressure, appreciative sign-off. Express enthusiasm to share tea/drink, explore a local neighborhood spot, or swap stories if schedules align.
4. Sign-off: "Warmly, Andrey", "Cheers, Andrey", or "Best, Andrey".

STRICT NEGATIVE CONSTRAINTS (NEVER USE THESE PHRASES):
- NEVER write: "I won't crowd your space or routine" (BANNED!)
- NEVER write: "Hope you have a great season!" or "Love your passion for [X]!" or "Excited about your [X]!" (Too robotic/fake).
- NEVER use fixed formulaic closings like "How about grabbing a quick coffee and sharing travel stories in the evening?".

Output ONLY the message text in 3 short, breathable paragraphs. Total length must be strictly between 85 and 120 words.
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
            )
            return clean_generated_message(raw_fallback, date_str=format_conversational_dates(check_in, check_out), city=host.get("city", "Tokyo"))

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
                        cleaned = clean_generated_message(raw_result, date_str=date_str, city=city)
                        words = len(cleaned.split())
                        qc_ok, issues = validate_message_qc(cleaned)
                        last_cleaned = cleaned

                        if words < 75 or words > 130 or "would love to connect!" in cleaned.lower() or not qc_ok:
                            logger.warning(
                                f"Ollama generation attempt {attempt} failed QC ({words} words): {issues}. Retrying..."
                            )
                            current_prompt = (
                                user_prompt
                                + f"\n\nCRITICAL RETRY INSTRUCTION: Previous draft had issues: {issues}. Write strictly between 85 and 120 words across 3 breathable paragraphs, warm and thoughtful, with no banned phrases."
                            )
                            continue

                        logger.info(f"Ollama generation attempt {attempt} passed QC ({words} words)")
                        return cleaned
                logger.warning(f"Ollama returned status {resp.status_code}: {resp.text}")
            except Exception as e:
                logger.warning(f"Ollama generation error on attempt {attempt}: {e}")

        if last_cleaned and 75 <= len(last_cleaned.split()) <= 135 and "would love to connect!" not in last_cleaned.lower():
            return last_cleaned

        # Фолбэк на надежный генератор шаблонов
        raw_fallback = generate_stay_request(
            host=host,
            check_in=check_in,
            check_out=check_out,
            guest_context=guest_context,
        )
        return clean_generated_message(raw_fallback, date_str=date_str, city=city)

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
   - You travel light (bulky luggage already forwarded to Hokkaido).
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
        "Andrey, ski coach heading to Club Med Tomamu (Hokkaido) for winter season. "
        "Arriving in Tokyo Nov 18 (~5 PM), departing Nov 20 morning. "
        "Bulky gear already forwarded to Hokkaido, travel light with small backpack, minimal footprint, "
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
