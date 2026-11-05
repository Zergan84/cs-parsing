#!/usr/bin/env python3
"""
reprocess_queue.py - Аудит, жесткая фильтрация и перегенерация текущей очереди запросов.

1. Загружает output/requests_queue.json.
2. Прогоняет через HostingAvailabilityGuard (дисквалифицирует lucyatkins1, omshanti, .-yuki, rinochan, Aiyami).
3. Переопределяет пол через обновленный GenderClassifier (Jose, Felipe, Hide, Shunsuke -> Male; Gloria, Sophia, Franca, Kumi, Naoko -> Female).
4. Очищает артефакты кодировки (\ufffd, кракозябры, концевые точки).
5. Обновляет guest_context на расширенный бэкграунд Андрея.
6. Перегенерирует сообщения через llm_agent с детерминированной очисткой от тире, tonight и машинных клише.
7. Сохраняет обновленную очередь в output/requests_queue.json и выводит итоговую таблицу.
"""

import json
import logging
import re
import sys
import time
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).parent
OUTPUT_DIR = BASE_DIR / "output"
QUEUE_FILE = OUTPUT_DIR / "requests_queue.json"
BACKUP_FILE = OUTPUT_DIR / "requests_queue_before_reprocess.json"
REJECTED_FILE = OUTPUT_DIR / "rejected_hosts.json"

sys.path.insert(0, str(BASE_DIR))

from hosting_guard import HostingAvailabilityGuard
from gender_classifier import GenderClassifier
from llm_agent import CouchsurfingLLM, DEFAULT_GUEST_CONTEXT, clean_generated_message

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("ReprocessQueue")


def main():
    print("=" * 80)
    print("🚀 КРИТИЧЕСКИЙ АУДИТ И РЕФАКТОРИНГ ОЧЕРЕДИ REQUESTS_QUEUE.JSON")
    print("=" * 80)

    if not QUEUE_FILE.exists():
        print(f"[!] Файл очереди не найден: {QUEUE_FILE}")
        sys.exit(1)

    with open(QUEUE_FILE, "r", encoding="utf-8") as f:
        queue = json.load(f)

    print(f"[*] Загружено анкет из очереди: {len(queue)}")

    # Сохраняем резервную копию
    with open(BACKUP_FILE, "w", encoding="utf-8") as f:
        json.dump(queue, f, ensure_ascii=False, indent=2)
    print(f"[+] Резервная копия сохранена: {BACKUP_FILE.name}")

    guard = HostingAvailabilityGuard()
    classifier = GenderClassifier()
    llm = CouchsurfingLLM()
    print(f"[*] Статус Ollama: {'ONLINE' if llm.is_available() else 'OFFLINE (Fallback)'}")

    target_disqualify = {"lucyatkins1", "omshanti", ".-yuki", "rinochan", "aiyami"}
    rejected_list = []
    accepted_queue = []

    print("\n" + "=" * 80)
    print("1️⃣ ЭТАП ФИЛЬТРАЦИИ (Hosting Availability Guard)")
    print("=" * 80)

    for item in queue:
        username = item.get("username", "")
        name = item.get("name", "")

        # Проверка через жесткий guard
        can_host, reason, conf = guard.verify_host(item, use_llm=False)

        if not can_host:
            rejected_entry = {
                "username": username,
                "name": name,
                "reason": reason,
                "rejected_at": datetime.now().isoformat(),
            }
            rejected_list.append(rejected_entry)
            print(f"❌ REJECTED: {username:20} ({name:15}) -> {reason}")
        else:
            accepted_queue.append(item)

    print(f"\n📊 Итог фильтрации: отсеяно {len(rejected_list)} невалидных хостов, осталось {len(accepted_queue)}")

    # Сохраняем список забракованных хостов
    with open(REJECTED_FILE, "w", encoding="utf-8") as f:
        json.dump(rejected_list, f, ensure_ascii=False, indent=2)
    print(f"[+] Забракованные анкеты сохранены в {REJECTED_FILE.name}")

    # Проверка, что все 5 обязательных кейсов гарантированно удалены
    disqualified_found = [r["username"] for r in rejected_list if r["username"].lower() in target_disqualify]
    print(f"[*] Подтверждение удаления целевых кейсов ({len(disqualified_found)}/5): {disqualified_found}")
    for t in target_disqualify:
        assert not any(x.get("username", "").lower() == t for x in accepted_queue), (
            f"❌ КРИТИЧЕСКАЯ ОШИБКА: Целевой хост '{t}' остался в очереди!"
        )
    print("✅ Все 5 проблемных анкет (lucyatkins1, omshanti, .-yuki, rinochan, Aiyami) УСПЕШНО УДАЛЕНЫ!")

    print("\n" + "=" * 80)
    print("2️⃣ ЭТАП: ПЕРЕОПРЕДЕЛЕНИЕ ПОЛА И ОЧИСТКА АРТЕФАКТОВ КОДИРОВКИ")
    print("=" * 80)

    for item in accepted_queue:
        orig_name = item.get("name", "")
        # Очистка от \ufffd и непечатаемых символов
        clean_name_raw = classifier._clean_name(item.get("clean_name") or orig_name)
        item["clean_name"] = clean_name_raw
        item["name"] = re.sub(r"[\ufffd\x00-\x1f\x7f-\x9f]", "", str(orig_name)).strip()

        # Классификация пола
        est_gender, host_type, g_reason = classifier.classify(
            name=clean_name_raw,
            about_text=item.get("about_me_clean") or item.get("about_me") or "",
            username=item.get("username", ""),
            references=item.get("references_count", 0),
        )
        item["estimated_gender"] = est_gender
        item["host_type"] = host_type
        print(f"👤 {item.get('username'):20} -> {clean_name_raw:15} | Gender: {est_gender:7} ({host_type}) | Rule: {g_reason}")

    # Валидация специфических требований пользователя к именам
    for it in accepted_queue:
        uname = it.get("username", "").lower()
        cname = it.get("clean_name", "")
        cname_lower = cname.lower()
        gen = it.get("estimated_gender", "")

        if any(m in cname_lower for m in ("jose", "felipe", "hide", "shunsuke")):
            assert gen == "Male", f"❌ ОШИБКА: {cname} ({uname}) определен как {gen}, ожидался Male!"
        if any(f in cname_lower for f in ("gloria", "sophia", "franca", "kumi", "naoko")):
            assert gen == "Female", f"❌ ОШИБКА: {cname} ({uname}) определен как {gen}, ожидался Female!"

    print("✅ Все проверки имен (Jose, Felipe, Hide, Shunsuke -> Male; Gloria, Sophia, Franca, Kumi, Naoko -> Female) УСПЕШНО ПРОЙДЕНЫ!")

    print("\n" + "=" * 80)
    print("3️⃣ ЭТАП: ПЕРЕГЕНЕРАЦИЯ СООБЩЕНИЙ С РАСШИРЕННЫМ БЭКГРАУНДОМ")
    print("=" * 80)

    total_gen = len(accepted_queue)
    for idx, item in enumerate(accepted_queue, 1):
        username = item.get("username")
        cname = item.get("clean_name")
        check_in = item.get("check_in", "2026-11-18")
        check_out = item.get("check_out", "2026-11-20")

        # Применяем актуальный расширенный контекст
        item["guest_context"] = DEFAULT_GUEST_CONTEXT

        t0 = time.perf_counter()
        raw_msg = llm.generate_request(
            host=item,
            check_in=check_in,
            check_out=check_out,
            guest_context=DEFAULT_GUEST_CONTEXT,
        )
        duration = time.perf_counter() - t0

        # Детерминированная очистка текста
        msg = clean_generated_message(raw_msg, date_str=f"Nov 18-20, 2026", city=item.get("location", "Tokyo"))

        # Проверки на тире, tonight, клише
        assert "—" not in msg, f"Found em-dash in msg for {username}"
        assert "–" not in msg, f"Found en-dash in msg for {username}"
        assert not re.search(r"\btonight\b", msg, re.IGNORECASE), f"Found tonight in msg for {username}"
        assert not re.search(r"\bI noticed\b", msg, re.IGNORECASE), f"Found 'I noticed' in msg for {username}"
        assert "\ufffd" not in msg, f"Found unicode replacement char in msg for {username}"

        item["generated_message"] = msg
        item["updated_at"] = datetime.now().isoformat()

        print(f"[{idx}/{total_gen}] ✍️ {username:20} ({cname:12}) | {len(msg.split())} слов | {duration:.2f} сек")

    # Сохраняем обновленный requests_queue.json
    with open(QUEUE_FILE, "w", encoding="utf-8") as f:
        json.dump(accepted_queue, f, ensure_ascii=False, indent=2)

    print("\n" + "=" * 80)
    print("🎉 УСПЕШНО СОХРАНЕНО: output/requests_queue.json")
    print("=" * 80)

    print(f"\n📋 ИТОГОВЫЙ СПИСОК ОТОБРАННЫХ ВАЛИДНЫХ АНКЕТ ({len(accepted_queue)} хостов):")
    print(f"{'#':<3} | {'Username':<22} | {'Name':<16} | {'Gender':<8} | {'Type':<6} | {'Refs':<4} | {'Location':<25}")
    print("-" * 95)
    for i, it in enumerate(accepted_queue, 1):
        loc = it.get('location', '')
        if isinstance(loc, dict):
            loc = loc.get('locationDescription', {}).get('description', 'Tokyo')
        loc = str(loc)[:24]
        print(f"{i:<3} | {it.get('username',''):<22} | {it.get('clean_name',''):<16} | {it.get('estimated_gender',''):<8} | {it.get('host_type',''):<6} | {it.get('references_count',0):<4} | {loc:<25}")

    print("=" * 95)
    print("🏆 ВСЕ ТАСКИ ПО АУДИТУ И ПЕРЕГЕНЕРАЦИИ ОЧЕРЕДИ УСПЕШНО ВЫПОЛНЕНЫ!")


if __name__ == "__main__":
    main()
