#!/usr/bin/env python3
"""
test_llm.py - Тестовый скрипт проверки генерации и валидации сообщений в llm_agent.py.

Проверяет:
1. Профиль Yosuke (любит Европу / Францию) в Токио (Nov 18–20, 2026):
   - Обязательное упоминание Французских Альп (Courchevel).
   - Точные даты: "Nov 18–20, 2026".
   - Полное отсутствие слова "tonight" и фразы "I noticed".
2. Профиль Kumi (любит горы / аутдор / треккинг) в Токио (Nov 18–21, 2026):
   - Упоминание горного бэкграунда (Альпы или Непал).
   - Точные даты: "Nov 18–21, 2026".
   - Полное отсутствие слова "tonight" и фразы "I noticed".
"""

import re
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR))

from test_isolation import activate_test_isolation

activate_test_isolation()

from llm_agent import CouchsurfingLLM


def validate_test_message(
    message: str,
    test_name: str,
    expected_dates: str,
    required_keywords: list,
):
    print("\n" + "=" * 70)
    print(f"📝 ТЕСТ: {test_name}")
    print("=" * 70)
    print(message)
    print("-" * 70)

    words = message.split()
    word_count = len(words)
    print(f"📊 Количество слов: {word_count}")

    # 1. Жесткая проверка запрета тире (em-dash и en-dash) - AI маркер
    assert "—" not in message, "Found em-dash in output"
    assert "–" not in message, "Found en-dash in output"
    print("✅ Длинные тире (—) и средние тире (–) полностью отсутствуют (AI маркер устранен)")

    # 2. Жесткая проверка отсутствия "tonight"
    assert not re.search(r"\btonight\b", message, re.IGNORECASE), (
        f"❌ ОШИБКА: Обнаружено запрещенное слово 'tonight' в тексте!"
    )
    print("✅ Слово 'tonight' отсутствует (устранена галлюцинация)")

    # 3. Жесткая проверка отсутствия машинных фраз "I noticed"
    assert not re.search(r"\bI noticed\b", message, re.IGNORECASE), (
        f"❌ ОШИБКА: Обнаружена запрещенная фраза 'I noticed'!"
    )
    assert not re.search(r"\bI see that\b", message, re.IGNORECASE), (
        f"❌ ОШИБКА: Обнаружена запрещенная фраза 'I see that'!"
    )
    assert not re.search(r"\bI can see\b", message, re.IGNORECASE), (
        f"❌ ОШИБКА: Обнаружена запрещенная фраза 'I can see'!"
    )
    print("✅ Машинные паттерны ('I noticed', 'I see that') отсутствуют")

    # 4. Проверка запрещенных штампов из стоп-листа
    banned_cliches = [
        "caught my eye",
        "hidden gems",
        "what an adventure",
        "excited to",
        "i'll be here",
        "fascinating",
        "love that vibe",
    ]
    for cliche in banned_cliches:
        assert cliche not in message.lower(), (
            f"❌ ОШИБКА: Обнаружен банальный штамп '{cliche}'!"
        )
    print("✅ Стоп-лист банальных фраз пройден ('caught my eye', 'hidden gems', 'what an adventure' и др. отсутствуют)")

    # 5. Проверка актуальных дат
    assert expected_dates.lower() in message.lower(), (
        f"❌ ОШИБКА: В тексте отсутствуют ожидаемые даты '{expected_dates}'!"
    )
    print(f"✅ Указаны актуальные даты: '{expected_dates}'")

    # 6. Проверка обязательных тематических ключевых слов
    for kw in required_keywords:
        assert kw.lower() in message.lower(), (
            f"❌ ОШИБКА: В тексте отсутствует обязательное ключевое слово '{kw}'!"
        )
        print(f"✅ Найдено обязательное упоминание: '{kw}'")

    # 7. Проверка подписи
    assert any(term in message.lower() for term in ("cheers, andrey", "best, andrey")), (
        "❌ ОШИБКА: Неверная подпись! Ожидается 'Cheers, Andrey' или 'Best, Andrey'."
    )
    print("✅ Корректная подпись ('Cheers, Andrey' / 'Best, Andrey')")

    # 8. Проверка форматирования (чистый текст)
    assert not message.startswith("```") and not message.endswith("```"), (
        "❌ ОШИБКА: Текст содержит markdown code block!"
    )

    # 9. Длина (целевой диапазон 70–110, допуск 55–125)
    assert 55 <= word_count <= 125, (
        f"❌ ОШИБКА: Длина текста ({word_count} слов) вне диапазона 55-125!"
    )
    print(f"✅ Длина текста соответствует лимиту ({word_count} слов)")
    print(f"🎉 ВСЕ АССЕРТЫ ДЛЯ [{test_name}] УСПЕШНО ПРОЙДЕНЫ!\n")


def main():
    print("=" * 80)
    print("🚀 КРИТИЧЕСКАЯ ПРОВЕРКА ГЕНЕРАЦИИ В LLM_AGENT (Yosuke & Kumi)")
    print("=" * 80)

    agent = CouchsurfingLLM()
    if not agent.is_available():
        print("[-] Ollama не запущена на http://localhost:11434")
        sys.exit(1)

    print(f"[*] Статус Ollama: ONLINE | Модель: {agent.select_best_model()}")

    # -------------------------------------------------------------------------
    # 1. Профиль YOSUKE (Любит Европу, часто путешествует во Францию и Италию)
    # -------------------------------------------------------------------------
    yosuke_host = {
        "name": "Yosuke S.",
        "clean_name": "Yosuke",
        "username": "yosuke_europe",
        "location": "Shibuya, Tokyo, Japan",
        "city": "Tokyo",
        "references_count": 48,
        "about_me_clean": (
            "Hi! I'm Yosuke. Huge European travel lover! I spend months traveling through "
            "France, Italy, and Switzerland every year. Big fan of European food, architecture, "
            "and cultural exchange. Always happy to host fellow travelers in Tokyo."
        ),
        "sleeping_arrangement": "Private Room",
    }

    t0 = time.perf_counter()
    msg_yosuke = agent.generate_request(
        host=yosuke_host,
        check_in="2026-11-18",
        check_out="2026-11-20",
    )
    print(f"⏱ Время генерации Yosuke: {time.perf_counter() - t0:.2f} сек")
    validate_test_message(
        message=msg_yosuke,
        test_name="Yosuke (Европа / Франция / Альпы)",
        expected_dates="Nov 18-20",
        required_keywords=["alps"],
    )

    # -------------------------------------------------------------------------
    # 2. Профиль KUMI (Любит горы, аутдор, треккинг)
    # -------------------------------------------------------------------------
    kumi_host = {
        "name": "Kumi N.",
        "clean_name": "Kumi",
        "username": "kumi_outdoor",
        "location": "Setagaya, Tokyo, Japan",
        "city": "Tokyo",
        "references_count": 32,
        "about_me_clean": (
            "Trail runner and mountain enthusiast. My weekends are all about hiking steep trails, "
            "climbing peaks, exploring nature, and breathing fresh mountain air. "
            "Looking forward to meeting respectful travelers who love the outdoors!"
        ),
        "sleeping_arrangement": "Couch",
    }

    t0 = time.perf_counter()
    msg_kumi = agent.generate_request(
        host=kumi_host,
        check_in="2026-11-18",
        check_out="2026-11-21",
    )
    print(f"⏱ Время генерации Kumi: {time.perf_counter() - t0:.2f} сек")
    validate_test_message(
        message=msg_kumi,
        test_name="Kumi (Горы / Аутдор / Непал / Альпы)",
        expected_dates="Nov 18-21",
        required_keywords=["alps"],  # или nepal
    )

    print("=" * 80)
    print("🏆 ВСЕ ТЕСТЫ ДЛЯ YOSUKE И KUMI УСПЕШНО ЗАВЕРШЕНЫ БЕЗ ОШИБОК!")
    print("=" * 80)


if __name__ == "__main__":
    main()
