"""
test_unit.py - Модульные тесты для логики классификации, фильтрации, соцсетей и генерации запросов.
"""

import unittest
from gender_classifier import classify_host
from filter_hosts import (
    HostFilter,
    detect_secret_code,
    extract_sleeping_arrangement,
    extract_house_rules,
    clean_text,
)
from social_extractor import extract_instagram
from message_generator import generate_stay_request, format_date_range
from hosting_guard import HostingAvailabilityGuard, check_host_can_overnight


class TestGenderClassifier(unittest.TestCase):
    def test_male_classification(self):
        gender, h_type = classify_host("Ryu I.", "Fit Japanese from Tokyo", "ryu-tokyo")
        self.assertEqual(gender, "Male")
        self.assertEqual(h_type, "solo")

        gender, h_type = classify_host("Takumi T.", "Solo adventure boy", "takumitezuka")
        self.assertEqual(gender, "Male")
        self.assertEqual(h_type, "solo")

        gender, h_type = classify_host("Koji A.", "Working in tech", "kojiarie")
        self.assertEqual(gender, "Male")

        gender, h_type = classify_host("Kentaro S.", "I don't host single female traveler", "kentaro-shibamoto")
        self.assertEqual(gender, "Male")

    def test_female_classification(self):
        gender, h_type = classify_host("Sakura M.", "Welcome to Tokyo", "sakura-tokyo")
        self.assertEqual(gender, "Female")
        self.assertEqual(h_type, "solo")

        gender, h_type = classify_host("Nozomi T.", "Solo traveler", "nozomi-nozomi-1")
        self.assertEqual(gender, "Female")

        gender, h_type = classify_host("Ai T.", "The girl who walks", "thegirlwhowalks")
        self.assertEqual(gender, "Female")

    def test_couple_classification(self):
        gender, h_type = classify_host("Ken & Yoko", "We live in Shibuya", "ken_yoko")
        self.assertEqual(gender, "Couple")
        self.assertEqual(h_type, "couple")

        gender, h_type = classify_host("Alex", "I live with my boyfriend in Tokyo", "alex_tokyo")
        self.assertEqual(gender, "Couple")
        self.assertEqual(h_type, "couple")


class TestSocialExtractor(unittest.TestCase):
    def test_instagram_extraction(self):
        url, handle = extract_instagram("Hi!! I’m Yuki from Japan. instagram.com/iamyukiyamada/ I love traveling")
        self.assertEqual(handle, "iamyukiyamada")
        self.assertEqual(url, "https://www.instagram.com/iamyukiyamada/")

        url, handle = extract_instagram("My Instagram: @boku___20___ — 36K followers")
        self.assertEqual(handle, "boku___20___")
        self.assertEqual(url, "https://www.instagram.com/boku___20___/")

        url, handle = extract_instagram("■Instagram @dan_ryo_yoshi")
        self.assertEqual(handle, "dan_ryo_yoshi")
        self.assertEqual(url, "https://www.instagram.com/dan_ryo_yoshi/")

        url, handle = extract_instagram("I have no social media.")
        self.assertEqual(handle, "")
        self.assertEqual(url, "")


class TestMessageGenerator(unittest.TestCase):
    def test_date_range_formatting(self):
        formatted = format_date_range("2026-11-11", "2026-11-14")
        self.assertIn("Nov 11–14", formatted)

    def test_stay_request_structure(self):
        host = {
            "name": "Koji A.",
            "clean_name": "Koji",
            "about_me_clean": "I love good food, ramen and creative design.",
            "sleeping_arrangement": "Private Room",
            "secret_code_hint": "tokyo_ramen",
            "secret_code_detected": True,
        }
        msg = generate_stay_request(host, "2026-11-11", "2026-11-14")
        self.assertIn("Hi Koji!", msg)
        self.assertIn("Nov 11–14", msg)
        self.assertIn("Tomamu", msg)
        self.assertIn("coach skiing", msg)
        self.assertIn("Andrey", msg)


class TestFilterAndExtraction(unittest.TestCase):
    def setUp(self):
        self.filter = HostFilter()

    def test_stop_triggers_exclusion(self):
        t1 = self.filter.check_stop_triggers("*NOT ACCEPTING GUESTS NOW Into cross-cultural communication")
        self.assertTrue(len(t1) > 0)
        self.assertIn("not accepting guests", t1)

        t2 = self.filter.check_stop_triggers("I'm back in Japan, but I'm far away from Tokyo for a while and cannot host anyone right now. Please refrain from sending stay requests")
        self.assertTrue(len(t2) > 0)
        self.assertIn("cannot host", t2)

        t3 = self.filter.check_stop_triggers("*Currently not in Japan* I'm just a modest boy who is full of curiosity")
        self.assertTrue(len(t3) > 0)
        self.assertIn("currently not in japan", t3)

        t4 = self.filter.check_stop_triggers("Currently on a working holiday in Perth, WA, Australia! People with crooked glasses...")
        self.assertTrue(len(t4) > 0)
        self.assertIn("working holiday", t4)

        t5 = self.filter.check_stop_triggers("We can host only for one night. If you need more than one night, please check our listing on Airbnb.")
        self.assertTrue(len(t5) > 0)
        self.assertIn("airbnb", t5)
        self.assertIn("only for one night", t5)

    def test_secret_code_detection(self):
        has_code, code = detect_secret_code("If you've read my description, please write 'holyfalcon' in your request")
        self.assertTrue(has_code)
        self.assertEqual(code, "holyfalcon")

        has_code, code = detect_secret_code("The secret word is sakura2026")
        self.assertTrue(has_code)
        self.assertEqual(code, "sakura2026")

        has_code, code = detect_secret_code("I ONLY HOST PEOPLE WHO READ MY ENTIRE PROFILE BEFORE SENDING A REQUEST")
        self.assertTrue(has_code)
        self.assertIn("Profile reading required", code)

    def test_sleeping_arrangement_extraction(self):
        self.assertEqual(extract_sleeping_arrangement("You will sleep in a Private Room with a lock"), "Private Room")
        self.assertEqual(extract_sleeping_arrangement("We offer a shared room with bunk beds"), "Shared Room")
        self.assertEqual(extract_sleeping_arrangement("Couch in the living room / common area"), "Common Area / Couch")

    def test_house_rules_extraction(self):
        rules = extract_house_rules("No smoking inside. Late nights okay. Please respect quiet hours.")
        self.assertIn("No smoking", rules)
        self.assertIn("Late nights okay", rules)


class TestHostingAvailabilityGuard(unittest.TestCase):
    def setUp(self):
        self.guard = HostingAvailabilityGuard()

    def test_moeko_hosting_guard(self):
        """Проверка отсева анкеты Моэко (живет с родителями, невозможно принять гостей)."""
        moeko_text = (
            "Hosting Not Available — My name is Moeko. I am a Japanese teacher to foreigners. "
            "Now I live my parents house, so impossible to host you. But we can have coffee and chat, or explore city."
        )
        matched_text, pattern = self.guard.check_regex_blacklist(moeko_text)
        self.assertTrue(bool(matched_text))
        self.assertIn("hosting not available", matched_text.lower())

        # Проверка через общий интерфейс
        host = {
            "username": "moekookuya",
            "name": "Moeko",
            "about_me_clean": moeko_text,
        }
        can_host, full_reason = check_host_can_overnight(host, use_llm=False)
        self.assertFalse(can_host)
        self.assertIn("Blacklist match", full_reason)


class TestDynamicTripAndDateSync(unittest.TestCase):
    def setUp(self):
        from auto_sender import AutoSender
        self.sender = AutoSender(dry_run=True, headless=True)

    def test_city_and_dates_extraction(self):
        # 1. Токио с явными датами
        tokyo_candidate = {
            "username": "tokyo_host_1",
            "target_city": "Tokyo",
            "arrival_date": "2026-11-16",
            "departure_date": "2026-11-19"
        }
        arr, dep, city = self.sender.get_host_dates_and_city(tokyo_candidate)
        self.assertEqual(city, "Tokyo")
        self.assertEqual(arr, "2026-11-16")
        self.assertEqual(dep, "2026-11-19")

        # 2. Киото со составным location и check_in / check_out
        kyoto_candidate = {
            "username": "kyoto_host_1",
            "location": "Kyoto, Kansai, Japan",
            "check_in": "2026-11-19",
            "check_out": "2026-11-22"
        }
        arr, dep, city = self.sender.get_host_dates_and_city(kyoto_candidate)
        self.assertEqual(city, "Kyoto")
        self.assertEqual(arr, "2026-11-19")
        self.assertEqual(dep, "2026-11-22")

        # 3. Саппоро
        sapporo_candidate = {
            "username": "sapporo_host_1",
            "location": "Sapporo, Hokkaido, Japan",
            "arrival_date": "2026-11-22",
            "departure_date": "2026-11-25"
        }
        arr, dep, city = self.sender.get_host_dates_and_city(sapporo_candidate)
        self.assertEqual(city, "Sapporo")
        self.assertEqual(arr, "2026-11-22")
        self.assertEqual(dep, "2026-11-25")

        # 4. Район Токио (Минато) корректно мапится на Токио
        minato_candidate = {
            "username": "minato_host_1",
            "location": "Minato, Tokyo, Japan"
        }
        arr, dep, city = self.sender.get_host_dates_and_city(minato_candidate)
        self.assertEqual(city, "Tokyo")


class TestInboxResponderPromptAndClassification(unittest.TestCase):
    """Тесты классификации и генерации ответов Inbox AI Responder."""

    def test_classification_differentiation(self):
        from llm_agent import classify_host_response

        # Kumi: операция/больница
        kumi_msg = "unfortunately i will be at the hospital for surgery, and i will come back after Nov.22"
        res_kumi = classify_host_response(kumi_msg)
        self.assertEqual(res_kumi["category"], "POLITE DECLINE WITH REASON")

        # Sho: чай/пиво
        sho_msg = "I don't have much time, but if I do find some, let's grab some tea or a beer."
        res_sho = classify_host_response(sho_msg)
        self.assertEqual(res_sho["category"], "WELCOMING / MEETUP OFFER")

        # Kengo: Кагуразака вечер
        kengo_msg = "My favorite spots are Kagurazaka... I’d be happy to see you in the evening then if we can!"
        res_kengo = classify_host_response(kengo_msg)
        self.assertEqual(res_kengo["category"], "WELCOMING / MEETUP OFFER")

        # Riko: кафе-бар и музей
        riko_msg = "no space in my house... but let's have a drink in a café-bar run by a friend. I highly recommend this museum."
        res_riko = classify_host_response(riko_msg)
        self.assertEqual(res_riko["category"], "WELCOMING / MEETUP OFFER")

        # Fuka: отказ и отсутствие времени
        fuka_msg = "Unfortunately, I'm unable to host you nor hang out with you during the time of your stay."
        res_fuka = classify_host_response(fuka_msg)
        self.assertEqual(res_fuka["category"], "COLD DECLINE")

        # Stani: занят и нет времени
        stani_msg = "Unfortunately, I won’t be able to host you. Recently I’ve been very busy and won’t have time."
        res_stani = classify_host_response(stani_msg)
        self.assertEqual(res_stani["category"], "COLD DECLINE")

    def test_kumi_no_skiing_talk(self):
        from llm_agent import generate_inbox_reply
        kumi_msg = "unfortunately i will be at the hospital for surgery"
        reply = generate_inbox_reply("Kumi", kumi_msg, category="POLITE DECLINE WITH REASON")
        self.assertNotIn("ski", reply.lower())
        self.assertNotIn("snow", reply.lower())
        self.assertNotIn("tomamu", reply.lower())
        self.assertTrue("recovery" in reply.lower() or "surgery" in reply.lower() or "smooth" in reply.lower())


class TestFirstRequestNoBannedTemplatesAndDynamicDates(unittest.TestCase):
    """Тестирует генерацию первых сообщений без шаблонных фраз и с динамическими датами."""

    def test_rule_based_fallback_dynamic_and_no_banned_phrases(self):
        from message_generator import generate_stay_request

        # 1. Киото с датами 16-19 ноября
        kyoto_host = {
            "name": "Kenji",
            "clean_name": "Kenji",
            "city": "Kyoto",
            "location": "Kyoto, Japan",
            "check_in": "2026-11-16",
            "check_out": "2026-11-19",
            "about_me_clean": "Love playing vinyl records, jazz music and brewing coffee."
        }
        msg = generate_stay_request(kyoto_host)
        
        # Проверяем динамический город и даты
        self.assertIn("Kyoto", msg)
        self.assertIn("Nov 16–19", msg)
        self.assertNotIn("Tokyo", msg)
        self.assertNotIn("Nov 18–20", msg)

        # Проверяем отсутствие забаненных шаблонных фраз
        self.assertNotIn("won't crowd your space", msg.lower())
        self.assertNotIn("crowd your routine", msg.lower())
        self.assertNotIn("sharing travel stories in the evening", msg.lower())
        self.assertNotIn("love your passion for", msg.lower())
        self.assertNotIn("excited about your", msg.lower())

        # Проверяем упоминание Takkyubin и лёгкого багажа
        self.assertIn("Takkyubin", msg)
        self.assertIn("daypack", msg)

    def test_qc_validator_rejects_banned_phrases(self):
        from llm_agent import validate_message_qc

        banned_sample = "Hey Kenji! I love your passion for vinyl. I'm passing through Tokyo Nov 18-20. I'll be out in town exploring during the daytime, so I won't crowd your space or routine. Cheers, Andrey"
        ok, issues = validate_message_qc(banned_sample)
        self.assertFalse(ok)
        self.assertTrue(any("banned" in i.lower() or "crowd your space" in i.lower() or "love your passion" in i.lower() or "too short" in i.lower() for i in issues))

    def test_qc_validator_expanded_depth_and_word_range(self):
        from llm_agent import validate_message_qc, clean_generated_message

        raw_expanded = (
            "Hi Yuka,\n\n"
            "36 countries is a wonderful milestone! Traveling that extensively really shifts how you see the world — "
            "what’s been the one place that surprised you the most so far?\n\n"
            "I’ve been fortunate to explore around 55 countries myself, and those wild contrasts — between buzzing megalopolises "
            "like NYC and the quiet serenity of Nepal — made me truly value how Couchsurfing connects kindred spirits. "
            "I’m Andrey, a ski coach passing through Tokyo Nov 16–19 before heading north to Hokkaido for the winter season at Club Med Tomamu.\n\n"
            "I travel ultra-light with just a small daypack since my winter gear is shipped ahead. I’d love to cross paths, "
            "support your favorite neighborhood spots, and trade travel memories over tea or a drink if our schedules align!\n\n"
            "Warmly,\n"
            "Andrey"
        )
        cleaned = clean_generated_message(raw_expanded, date_str="Nov 16–19", city="Tokyo")
        words = len(cleaned.split())
        self.assertTrue(75 <= words <= 130, f"Expected 75-130 words, got {words}")
        ok, issues = validate_message_qc(cleaned)
        self.assertTrue(ok, f"Validation failed with issues: {issues}")

        # Проверка отбраковки бана "Hope you have a great season!"
        with_season = cleaned + " Hope you have a great season!"
        ok_season, issues_season = validate_message_qc(with_season)
        self.assertFalse(ok_season)
        self.assertTrue(any("hope you have a great season" in i.lower() for i in issues_season))


if __name__ == "__main__":
    unittest.main()
