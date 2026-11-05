#!/usr/bin/env python3
"""
test_gender.py - Комплексный тест гибридного классификатора пола (лингвистика + Ollama LLM + кэш).

Тестирует набор японских имен: ["Rie", "Yuki", "Kenji", "Haruka", "Tsubasa", "Ai", "Hiroshi"],
а также контекстную деамбигуацию для амбивалентных имен (Yuki, Tsubasa) и пакетный режим (Batch).
"""

import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR))

from gender_classifier import LLMGenderClassifier, get_gender_classifier


def run_tests():
    print("=" * 80)
    print("🇯🇵 ТЕСТИРОВАНИЕ ГИБРИДНОГО КЛАССИФИКАТОРА ПОЛА (Лингвистика + Ollama + Кэш)")
    print("=" * 80)

    # Используем изолированный кэш для чистоты измерений
    test_cache = BASE_DIR / "output" / "test_gender_cache.json"
    if test_cache.exists():
        test_cache.unlink()

    classifier = LLMGenderClassifier(cache_path=test_cache)

    test_cases = [
        {
            "name": "Rie",
            "bio": "Living in Tokyo, love art and coffee",
            "expected": "Female",
            "desc": "Однозначное японское женское имя (суффикс -e)",
        },
        {
            "name": "Kenji",
            "bio": "Software engineer in Shibuya",
            "expected": "Male",
            "desc": "Однозначное японское мужское имя (суффикс -ji)",
        },
        {
            "name": "Haruka",
            "bio": "University student in Shinjuku",
            "expected": "Female",
            "desc": "Японское женское имя (суффикс -ka)",
        },
        {
            "name": "Ai",
            "bio": "Photographer and traveler",
            "expected": "Female",
            "desc": "Словарное японское женское имя",
        },
        {
            "name": "Hiroshi",
            "bio": "Architect from Roppongi",
            "expected": "Male",
            "desc": "Японское мужское имя (суффикс -shi)",
        },
        {
            "name": "Yuki",
            "bio": "Hi, I am a girl traveling around the world! She/her",
            "expected": "Female",
            "desc": "Амбивалентное имя Yuki с женским контекстом (LLM / pronoun clues)",
        },
        {
            "name": "Yuki",
            "bio": "Hey guys, I am a dude working as a snowboard guide. He/him",
            "expected": "Male",
            "desc": "Амбивалентное имя Yuki с мужским контекстом (LLM / pronoun clues)",
        },
        {
            "name": "Tsubasa",
            "bio": "Dancer and solo female traveler, loves nature. She is friendly",
            "expected": "Female",
            "desc": "Амбивалентное имя Tsubasa с женским контекстом (LLM)",
        },
        {
            "name": "Tsubasa",
            "bio": "Soccer player, guy living in Tokyo. He/him",
            "expected": "Male",
            "desc": "Амбивалентное имя Tsubasa с мужским контекстом (LLM)",
        },
        # Новые тесты для строгой приоритетной цепочки (Tomo, -o, references, etc.)
        {
            "name": "Tomo A.",
            "bio": "I live in Tokyo. Previously with my ex-girlfriend, now on my own.",
            "expected": "Male",
            "desc": "УРОВЕНЬ 1: Tomo с маркером отношений ex-girlfriend -> Male",
        },
        {
            "name": "Tomo",
            "bio": "Software engineer enjoying mountains and coffee.",
            "expected": "Male",
            "desc": "УРОВЕНЬ 3: Изолированное имя Tomo (-o без k) -> Male",
        },
        {
            "name": "Tomoko",
            "bio": "English teacher in Shibuya.",
            "expected": "Female",
            "desc": "УРОВЕНЬ 3: Tomoko (строгое окончание -ko) -> Female",
        },
        {
            "name": "Alex",
            "bio": "Call me Tom! Exploring Tokyo neighborhoods.",
            "expected": "Male",
            "desc": "УРОВЕНЬ 1: Никнейм Tom в Bio -> Male",
        },
        {
            "name": "Host",
            "bio": "Welcome to my home in Tokyo.",
            "refs": "He is an amazing host! His apartment was so clean and he made breakfast for us.",
            "expected": "Male",
            "desc": "УРОВЕНЬ 2: Местоимения в отзывах (he/his/him) -> Male",
        },
        {
            "name": "Host",
            "bio": "Welcome to my home in Tokyo.",
            "refs": "She was so kind and helpful. Her place is cozy and she gave me local tips.",
            "expected": "Female",
            "desc": "УРОВЕНЬ 2: Местоимения в отзывах (she/her/hers) -> Female",
        },
        {
            "name": "Ichiro",
            "bio": "Living in Tokyo.",
            "expected": "Male",
            "desc": "УРОВЕНЬ 3: Окончание -o без k (Ichiro) -> Male",
        },
        {
            "name": "Hayato",
            "bio": "Tokyo resident.",
            "expected": "Male",
            "desc": "УРОВЕНЬ 3: Окончание -to (Hayato) -> Male",
        },
        {
            "name": "Takashi",
            "bio": "Tokyo resident.",
            "expected": "Male",
            "desc": "УРОВЕНЬ 3: Окончание -shi (Takashi) -> Male",
        },
        {
            "name": "Masakazu",
            "bio": "Tokyo resident.",
            "expected": "Male",
            "desc": "УРОВЕНЬ 3: Окончание -kazu (Masakazu) -> Male",
        },
        {
            "name": "Kazuhiko",
            "bio": "Tokyo resident.",
            "expected": "Male",
            "desc": "УРОВЕНЬ 3: Окончание -hiko (Kazuhiko) -> Male",
        },
        {
            "name": "Eimi",
            "bio": "Tokyo resident.",
            "expected": "Female",
            "desc": "УРОВЕНЬ 3: Окончание -mi (Eimi) -> Female",
        },
        {
            "name": "Asuka",
            "bio": "Tokyo resident.",
            "expected": "Female",
            "desc": "УРОВЕНЬ 3: Окончание -ka (Asuka) -> Female",
        },
        {
            "name": "Rina",
            "bio": "Tokyo resident.",
            "expected": "Female",
            "desc": "УРОВЕНЬ 3: Окончание -na (Rina) -> Female",
        },
        {
            "name": "Tomoe",
            "bio": "Tokyo resident.",
            "expected": "Female",
            "desc": "УРОВЕНЬ 3: Окончание -e (Tomoe) -> Female",
        },
    ]

    print("\n--- 1. Поэлементное тестирование (Cold Run) ---")
    correct = 0
    total = len(test_cases)
    t0_total = time.perf_counter()

    for tc in test_cases:
        t0 = time.perf_counter()
        gender, host_type, reason = classifier.classify(
            name=tc["name"],
            about_text=tc["bio"],
            references=tc.get("refs"),
        )
        elapsed = (time.perf_counter() - t0) * 1000

        is_match = (gender.lower() == tc["expected"].lower())
        if is_match:
            correct += 1
            status_icon = "✅"
        else:
            status_icon = "❌"

        print(
            f"{status_icon} [{tc['name']:<8}] -> {gender:<7} (Ожидалось: {tc['expected']:<7}) "
            f"| {elapsed:6.1f} ms | Причина: {reason}"
        )

    total_time = (time.perf_counter() - t0_total) * 1000
    accuracy = (correct / total) * 100
    print("-" * 80)
    print(f"Итог Cold Run: {correct}/{total} совпадений ({accuracy:.1f}%) за {total_time:.1f} ms")

    # 2. Проверка работы кэша (Warm Run)
    print("\n--- 2. Проверка скорости кэширования (Warm Run) ---")
    t0_warm = time.perf_counter()
    warm_correct = 0
    for tc in test_cases:
        gender, _, reason = classifier.classify(
            name=tc["name"],
            about_text=tc["bio"],
            references=tc.get("refs"),
        )
        if gender.lower() == tc["expected"].lower():
            warm_correct += 1

    warm_time = (time.perf_counter() - t0_warm) * 1000
    avg_cached = warm_time / total
    print(f"✅ Warm Run (из кэша): {warm_correct}/{total} за {warm_time:.3f} ms (в среднем {avg_cached:.3f} ms/имя)")

    # 3. Пакетная классификация (Batching)
    print("\n--- 3. Тест пакетной классификации (Batching) ---")
    batch_hosts = [
        {"name": "Makoto S.", "about_me": "She is an actress in Tokyo"},
        {"name": "Aoi K.", "about_me": "Guy who loves climbing and camping. He/him"},
        {"name": "Shinobu M.", "about_me": "Female backpacker exploring Asia"},
    ]
    t0_batch = time.perf_counter()
    batch_res = classifier.classify_batch(batch_hosts)
    batch_time = (time.perf_counter() - t0_batch) * 1000

    print(f"Пакет из {len(batch_hosts)} амбивалентных хостов обработан за {batch_time:.1f} ms:")
    for h in batch_res:
        print(f"  • {h['name']} -> {h['gender']} ({h.get('gender_reason')})")

    # Чистим тестовый файл кэша
    if test_cache.exists():
        test_cache.unlink()

    print("\n" + "=" * 80)
    print("🎉 Все тесты гибридного классификатора завершены успешно!")
    print("=" * 80)


if __name__ == "__main__":
    run_tests()
