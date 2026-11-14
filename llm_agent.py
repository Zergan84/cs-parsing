"""LLM-assisted profile and inbox analysis. Outgoing stay-request text is generated in core.messaging."""

import base64
import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import requests

logger = logging.getLogger("CouchsurfingLLM")
logging.basicConfig(level=logging.INFO)

DEFAULT_OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
DEFAULT_TEXT_MODEL = "qwen2.5:7b"
DEFAULT_VISION_MODEL = "llama3.2-vision:11b"

# Актуальные даты для текущей кампании в Токио (ноябрь 2026)
DEFAULT_CHECK_IN = os.environ.get("CS_CHECK_IN", "2026-11-18")
DEFAULT_CHECK_OUT = os.environ.get("CS_CHECK_OUT", "2026-11-20")

# Актуальный расширенный бэкграунд гостя
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
        """Compatibility API delegating outgoing requests to the canonical core generator."""
        from core.messaging import build_personalized_message

        trip = {
            "city": host.get("city") or str(host.get("location") or "Kyoto").split(",")[0].strip(),
            "arrival_date": check_in if include_dates else "",
            "departure_date": check_out if include_dates else "",
        }
        message, _, _ = build_personalized_message(host, trip)
        return message

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
