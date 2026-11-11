#!/usr/bin/env python3
"""
hotfix_approved_texts.py
Горячая очистка и нормализация сообщений кандидатов со статусом 'Approved'.
Безопасно обновляет requests.db / couchsurfing_platform.db и requests_queue.json на лету,
не прерывая работу фонового процесса автопилота.
"""

import sys
import os
import re
import json
import random
import sqlite3
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = Path(os.environ.get("CS_OUTPUT_DIR", str(BASE_DIR / "output"))).resolve()
DB_PATH = Path(os.environ.get("CS_DB_PATH", str(BASE_DIR / "requests.db"))).resolve()
PLATFORM_DB = Path(os.environ.get("CS_DB_PATH", str(OUTPUT_DIR / "couchsurfing_platform.db"))).resolve()
QUEUE_FILE = OUTPUT_DIR / "requests_queue.json"

GREETINGS = [
    "Hi {name},",
    "Hey {name}!",
    "Hello {name},",
    "Hey {name},"
]


def clean_name_str(raw_name: str) -> str:
    """
    Очищает имя хоста:
    - убирает приписки вида 'in Tokyo', 'from ...', 'at ...'
    - берет первое слово (разделители: пробелы, подчеркивания)
    - оставляет только буквенные символы и приводит к Title Case
    """
    if not raw_name:
        return "there"
    # Удаляем приписки городов и предлогов
    name_clean = re.sub(r'(?i)\s+(in|from|at)\s+.*$', '', raw_name).strip()
    # Первое слово
    first_token = re.split(r'[\s_]+', name_clean)[0]
    # Очистка от небуквенных символов
    letters = re.sub(r'[^a-zA-Zа-яА-ЯёЁ]', '', first_token)
    if letters:
        return letters.capitalize()
    # Fallback для CJK/иероглифов
    cjk = re.sub(r'[^\w]|[\d_]', '', first_token)
    return cjk if cjk else "there"


def sanitize_approved_message(text: str, host_clean_name: str = "") -> str:
    """
    Нормализует первую строку сообщения и устраняет неестественные вставки:
    1) Нормализация первой строки (приветствие + чистое имя).
    2) Рандомизация приветствия ('Hi {name},', 'Hey {name}!', 'Hello {name},', 'Hey {name},').
    3) Устранение дословных вставок bio (', introvert who loves...', 'introvert who loves...').
    """
    if not text:
        return text
    
    lines = text.split('\n')
    if not lines:
        return text
    
    first_line = lines[0].strip()
    if not first_line:
        return text

    # Убираем типичные LLM-префиксы: "Sure thing! Let's get started. ", "Sure, "
    first_line = re.sub(
        r'^(?:Sure(?:\s+thing)?|Yes|Great|Awesome|Cool|Certainly)[!,.]?\s*(?:Let\'?s\s+get\s+started[.!:]?\s*)?',
        '',
        first_line,
        flags=re.IGNORECASE
    ).strip()

    # Шаблон 1: Строка начинается с приветствия: "Hey {Name}...", "Hi {Name}...", "Hello {Name}...", "Hola {Name}..."
    match_greeting = re.match(r'^(Hey|Hi|Hello|Hola|Ciao)\s+([^,\n!:]+)([,!:]?)(.*)$', first_line, re.IGNORECASE)
    if match_greeting:
        raw_name = match_greeting.group(2)
        rest_of_line = match_greeting.group(4).strip()
        clean_name = clean_name_str(raw_name)
        if (not clean_name or clean_name.lower() in ("sure", "there")) and host_clean_name:
            clean_name = clean_name_str(host_clean_name)

        if clean_name and clean_name.lower() != "there":
            rest_of_line = re.sub(rf'^{re.escape(clean_name)}[!:,]\s*', '', rest_of_line, flags=re.IGNORECASE).strip()

        # Устраняем неестественные конструкции bio вида ", introvert who loves...", "introvert who loves..."
        rest_of_line = re.sub(
            r'^[,\s]*(?:(?:an?\s+)?[a-zA-Z\s-]{2,30}\s+who\s+(?:loves|enjoys|likes|is|wants)[^,.]+[,.]?\s*)',
            '',
            rest_of_line,
            flags=re.IGNORECASE
        ).strip()
        rest_of_line = re.sub(r'^[,\s.:;]+', '', rest_of_line).strip()
        if rest_of_line:
            rest_of_line = rest_of_line[0].upper() + rest_of_line[1:]

        chosen_greeting = random.choice(GREETINGS).format(name=clean_name)
        if rest_of_line:
            lines[0] = f"{chosen_greeting} {rest_of_line}"
        else:
            lines[0] = chosen_greeting
        return '\n'.join(lines)

    # Шаблон 2: Строка начинается с имени без "Hey" (например, "Naoko, I just read...")
    match_name_first = re.match(r'^([a-zA-Zа-яА-ЯёЁ_.-]+)([,!:])\s*(.*)$', first_line)
    if match_name_first:
        raw_name = match_name_first.group(1)
        rest_of_line = match_name_first.group(3).strip()
        clean_name = clean_name_str(raw_name)

        rest_of_line = re.sub(
            r'^[,\s]*(?:(?:an?\s+)?[a-zA-Z\s-]{2,30}\s+who\s+(?:loves|enjoys|likes|is|wants)[^,.]+[,.]?\s*)',
            '',
            rest_of_line,
            flags=re.IGNORECASE
        ).strip()
        rest_of_line = re.sub(r'^[,\s.:;]+', '', rest_of_line).strip()
        if rest_of_line:
            rest_of_line = rest_of_line[0].upper() + rest_of_line[1:]

        chosen_greeting = random.choice(GREETINGS).format(name=clean_name)
        if rest_of_line:
            lines[0] = f"{chosen_greeting} {rest_of_line}"
        else:
            lines[0] = chosen_greeting
        return '\n'.join(lines)

    # Шаблон 3: Если в сообщении вообще не было приветствия, но есть имя хоста
    if host_clean_name:
        clean_name = clean_name_str(host_clean_name)
        chosen_greeting = random.choice(GREETINGS).format(name=clean_name)
        return f"{chosen_greeting}\n\n{text}"

    return text


def main():
    print("=" * 60)
    print("🚀 HOTFIX: Горячая санитизация одобренных текстов (Approved)")
    print("=" * 60)

    # 1. Проверяем наличие БД
    target_db = DB_PATH if DB_PATH.exists() else PLATFORM_DB
    if not target_db.exists():
        print(f"❌ Ошибка: база данных не найдена ни по пути {DB_PATH}, ни по {PLATFORM_DB}")
        sys.exit(1)

    print(f"📁 Подключение к базе данных: {target_db}")
    conn = sqlite3.connect(str(target_db))
    cur = conn.cursor()

    # Убеждаемся, что view 'requests' и trigger существуют
    cur.execute("""
    CREATE VIEW IF NOT EXISTS requests AS
    SELECT 
        sq.id AS id,
        sq.host_id AS host_id,
        sq.status AS status,
        gm.message_text AS message,
        h.name AS name,
        h.clean_name AS clean_name,
        h.username AS username
    FROM sending_queue sq
    JOIN generated_messages gm ON sq.current_message_id = gm.id
    LEFT JOIN hosts h ON sq.host_id = h.id;
    """)

    cur.execute("""
    CREATE TRIGGER IF NOT EXISTS update_requests_message
    INSTEAD OF UPDATE OF message ON requests
    BEGIN
        UPDATE generated_messages
        SET message_text = NEW.message
        WHERE id = (SELECT current_message_id FROM sending_queue WHERE id = OLD.id);
    END;
    """)
    conn.commit()

    # Выбираем все строки со статусом 'Approved'
    cur.execute("SELECT id, message, clean_name, name FROM requests WHERE status = 'Approved';")
    approved_rows = cur.fetchall()
    print(f"🔍 Найдено одобренных записей в базе данных: {len(approved_rows)}")

    updated_db_count = 0
    examples = []

    for row_id, message, clean_name, raw_name in approved_rows:
        if not message:
            continue
        effective_name = clean_name or raw_name or ""
        cleaned = sanitize_approved_message(message, effective_name)
        if cleaned != message:
            if len(examples) < 4:
                old_first = message.strip().split('\n')[0]
                new_first = cleaned.strip().split('\n')[0]
                examples.append((old_first, new_first))

            # Выполняем UPDATE requests SET message = ? WHERE id = ?
            cur.execute("UPDATE requests SET message = ? WHERE id = ?", (cleaned, row_id))
            updated_db_count += 1

    conn.commit()
    conn.close()
    print(f"✅ База данных обновлена: {updated_db_count} записей модифицировано.")

    # 2. Обновляем requests_queue.json
    updated_json_count = 0
    if QUEUE_FILE.exists():
        try:
            with open(QUEUE_FILE, "r", encoding="utf-8") as f:
                queue_data = json.load(f)

            for item in queue_data:
                status = item.get("status")
                if status in ("Approved", "Queued"):
                    msg = item.get("generated_message", "")
                    effective_name = item.get("clean_name") or item.get("name") or ""
                    if msg:
                        cleaned = sanitize_approved_message(msg, effective_name)
                        if cleaned != msg:
                            item["generated_message"] = cleaned
                            updated_json_count += 1
                    # Очищаем clean_name если там мусор
                    if effective_name:
                        item["clean_name"] = clean_name_str(effective_name)

            with open(QUEUE_FILE, "w", encoding="utf-8") as f:
                json.dump(queue_data, f, ensure_ascii=False, indent=2)

            print(f"✅ Файл requests_queue.json обновлен: {updated_json_count} карточек модифицировано.")
        except Exception as e:
            print(f"⚠️ Ошибка обновления requests_queue.json: {e}")

    # 3. Примеры исправлений
    if examples:
        print("\n--- Примеры нормализованных приветствий (Before -> After) ---")
        for i, (b, a) in enumerate(examples, 1):
            print(f"{i}. [BEFORE]: {b}")
            print(f"   [AFTER] : {a}\n")

    print("=" * 60)
    print("🎉 Хотфикс успешно завершен! Автопилот продолжает работу без остановок.")
    print("=" * 60)


if __name__ == "__main__":
    main()
