"""
test_unit.py - Модульные тесты для логики классификации, фильтрации, соцсетей и генерации запросов.
"""

import unittest
from test_isolation import activate_test_isolation

activate_test_isolation()

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
    def test_male_classification_with_context(self):
        # 1. Контекст в Bio (самоидентификация, маркеры пола, местоимения)
        gender, h_type = classify_host("Takumi T.", "Solo adventure boy", "takumitezuka")
        self.assertEqual(gender, "Male")
        self.assertEqual(h_type, "solo")

        gender, h_type = classify_host("Kentaro S.", "Working in tech (he/him). Ex-girlfriend lives in Osaka.", "kentaro-shibamoto")
        self.assertEqual(gender, "Male")
        self.assertEqual(h_type, "solo")

        # 2. Контекст в отзывах (References)
        gender, h_type = classify_host("Ryu I.", "Fit traveler from Tokyo", "ryu-tokyo", references=["He was a great host, he showed us Kyoto."])
        self.assertEqual(gender, "Male")

        gender, h_type = classify_host("Koji A.", "Working in tech", "kojiarie", references=["He hosted us warmly, very polite guy."])
        self.assertEqual(gender, "Male")

    def test_female_classification_with_context(self):
        # 1. Контекст в Bio
        gender, h_type = classify_host("Ai T.", "The girl who walks", "thegirlwhowalks")
        self.assertEqual(gender, "Female")
        self.assertEqual(h_type, "solo")

        gender, h_type = classify_host("Sakura M.", "Welcome to Tokyo! Solo female traveler (she/her).", "sakura-tokyo")
        self.assertEqual(gender, "Female")
        self.assertEqual(h_type, "solo")

        # 2. Контекст в отзывах
        gender, h_type = classify_host("Nozomi T.", "Solo traveler", "nozomi-nozomi-1", references=["She is an amazing person, she guided us everywhere."])
        self.assertEqual(gender, "Female")

    def test_couple_classification(self):
        gender, h_type = classify_host("Ken & Yoko", "We live in Shibuya", "ken_yoko")
        self.assertEqual(gender, "Couple")
        self.assertEqual(h_type, "couple")

        gender, h_type = classify_host("Alex", "I live with my boyfriend in Tokyo", "alex_tokyo")
        self.assertEqual(gender, "Couple")
        self.assertEqual(h_type, "couple")

    def test_unknown_without_context_and_international_names(self):
        # Без явных контекстных подтверждений пол ВСЕГДА Unknown (никаких догадок по имени или суффиксу)
        self.assertEqual(classify_host("Ryu I.", "Fit Japanese from Tokyo", "ryu-tokyo")[0], "Unknown")
        self.assertEqual(classify_host("Sakura M.", "Welcome to Tokyo", "sakura-tokyo")[0], "Unknown")
        self.assertEqual(classify_host("Koji A.", "Working in tech", "kojiarie")[0], "Unknown")
        self.assertEqual(classify_host("Nozomi T.", "Solo traveler", "nozomi-1")[0], "Unknown")

        # Международные имена без контекста (ранее ошибочно классифицировавшиеся по суффиксам -ka, -mi, -na)
        self.assertEqual(classify_host("Luka", "Software engineer from Croatia", "luka_travel")[0], "Unknown")
        self.assertEqual(classify_host("Mika", "Art enthusiast from Helsinki", "mika_art")[0], "Unknown")
        self.assertEqual(classify_host("Slava", "Backpacker from Prague", "slava_p")[0], "Unknown")
        self.assertEqual(classify_host("Sami", "Coffee lover from Finland", "sami_c")[0], "Unknown")
        self.assertEqual(classify_host("Krishna", "Music producer from Mumbai", "krishna_m")[0], "Unknown")
        self.assertEqual(classify_host("Andrea", "Photographer from Milan", "andrea_ph")[0], "Unknown")

        # Те же международные имена С явными контекстными подтверждениями
        self.assertEqual(classify_host("Luka", "Software engineer", "luka_travel", references=["He was super helpful! He cooked dinner."])[0], "Male")
        self.assertEqual(classify_host("Mika", "Art enthusiast (she/her)", "mika_art")[0], "Female")
        self.assertEqual(classify_host("Mika", "Art enthusiast", "mika_art", references=["He is a great host, he showed me the gallery."])[0], "Male")
        self.assertEqual(classify_host("Slava", "As a guy I love the outdoors (he/him)", "slava_p")[0], "Male")
        self.assertEqual(classify_host("Sami", "Coffee lover", "sami_c", references=["He welcomed me like a brother."])[0], "Male")
        self.assertEqual(classify_host("Krishna", "Music producer", "krishna_m", references=["He is a polite gentleman, he helped us a lot."])[0], "Male")


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
        self.assertIn("Hi Koji", msg)
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
        
        # Проверяем динамический город
        self.assertIn("Kyoto", msg)
        self.assertNotIn("Tokyo", msg)

        # Проверяем отсутствие забаненных шаблонных фраз и запрещенных тем
        self.assertNotIn("won't crowd your space", msg.lower())
        self.assertNotIn("crowd your routine", msg.lower())
        self.assertNotIn("sharing travel stories in the evening", msg.lower())
        self.assertNotIn("love your passion for", msg.lower())
        self.assertNotIn("excited about your", msg.lower())
        self.assertNotIn("takkyubin", msg.lower())
        self.assertNotIn("daypack", msg.lower())

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
            "I’ve been fortunate to explore widely myself, and those wild contrasts — between buzzing megalopolises "
            "like NYC and the quiet serenity of Nepal — made me truly value how Couchsurfing connects kindred spirits. "
            "I’m Andrey, a sports coach and filmmaker visiting Tokyo to explore the city and its creative culture.\n\n"
            "I’d love to cross paths, hear about your favorite neighborhood spots, and trade travel memories "
            "if our schedules align!\n\n"
            "Warmly,\n"
            "Andrey"
        )
        cleaned = clean_generated_message(raw_expanded, date_str="Nov 16–19", city="Tokyo")
        words = len(cleaned.split())
        self.assertTrue(60 <= words <= 130, f"Expected 60-130 words, got {words}")
        ok, issues = validate_message_qc(cleaned, trip_city="Tokyo")
        self.assertTrue(ok, f"Validation failed with issues: {issues}")

        # Проверка отбраковки бана "Hope you have a great season!"
        with_season = cleaned + " Hope you have a great season!"
        ok_season, issues_season = validate_message_qc(with_season, trip_city="Tokyo")
        self.assertFalse(ok_season)
        self.assertTrue(any("hope you have a great season" in i.lower() for i in issues_season))

    def test_qc_rejects_inferred_travel_route_and_hokkaido(self):
        """QC бракует сообщения с выдуманным маршрутом путешествия (Hokkaido, on my way north)."""
        from llm_agent import validate_message_qc

        # 1. Поездка в Киото, но сообщение утверждает путь на Хоккайдо
        route_hallucination = (
            "Hi Masafumi,\n\n"
            "Love that you take time to chat with locals while traveling.\n\n"
            "I'm Andrey, heading through Kyoto on my way up north for skiing in Hokkaido. "
            "I speak English, French, and Russian too. Would be great to share travel stories.\n\n"
            "Cheers,\nAndrey"
        )
        ok, issues = validate_message_qc(route_hallucination, trip_city="Kyoto")
        self.assertFalse(ok)
        self.assertTrue(any("route" in i.lower() or "hokkaido" in i.lower() for i in issues), f"Issues: {issues}")

    def test_qc_rejects_ungrounded_shared_interest_assumption(self):
        """QC бракует сообщения с выдуманным общим интересом (We both enjoy language learning)."""
        from llm_agent import validate_message_qc

        host_without_language = {
            "name": "Masafumi",
            "about_me_clean": "I love chatting with local people to understand cultural differences when traveling.",
            "interests": "Travel, Culture, Photography",
        }
        msg_with_false_shared_interest = (
            "Hi Masafumi,\n\n"
            "Love that you chat with locals when traveling. "
            "We both enjoy language learning, I speak English, French, and Russian too. "
            "I'm visiting Kyoto and would be glad to share travel stories if your timing works out.\n\n"
            "Cheers,\nAndrey"
        )
        ok, issues = validate_message_qc(
            msg_with_false_shared_interest,
            host_data=host_without_language,
            trip_city="Kyoto",
        )
        self.assertFalse(ok)
        self.assertTrue(
            any("shared interest" in i.lower() or "language learning" in i.lower() for i in issues),
            f"Expected shared interest rejection, got: {issues}",
        )

    def test_rule_based_fallback_contains_no_route_inferences(self):
        """Генератор шаблонов не должен подставлять Хоккайдо или выдуманные маршруты."""
        from message_generator import generate_stay_request

        test_hosts = [
            {"name": "Masafumi", "city": "Kyoto", "about_me_clean": "Traveler exploring cultures"},
            {"name": "Shohei", "city": "Kyoto", "about_me_clean": "Learning koto traditional instrument"},
            {"name": "Daichi", "city": "Kyoto", "about_me_clean": "Street photo fan"},
            {"name": "Lina", "city": "Kyoto", "about_me_clean": ""},
        ]
        for host in test_hosts:
            msg = generate_stay_request(host)
            self.assertNotIn("Hokkaido", msg)
            self.assertNotIn("hokkaido", msg.lower())
            self.assertNotIn("on my way up north", msg.lower())
            self.assertNotIn("heading north", msg.lower())
            self.assertNotIn("we both enjoy language learning", msg.lower())


class TestLgbtqPayloadFilter(unittest.TestCase):
    """Тестирование полного удаления фильтра isLGBTQ из API-запросов."""

    def test_remove_lgbtq_filter_nested_dict(self):
        from scraper import remove_lgbtq_filter

        raw_payload = {
            "0": {
                "json": {
                    "locationId": "2v0h3CCbfIOkN7lkfv0mo30zZmI_lc",
                    "filters": {
                        "isLGBTQ": False,
                        "status": "Accepting Guests",
                        "minReferences": 0,
                    },
                    "limit": 20,
                }
            }
        }
        cleaned, modified = remove_lgbtq_filter(raw_payload)
        self.assertTrue(modified)
        self.assertNotIn("isLGBTQ", cleaned["0"]["json"]["filters"])
        self.assertEqual(cleaned["0"]["json"]["filters"]["status"], "Accepting Guests")
        self.assertEqual(cleaned["0"]["json"]["limit"], 20)
        # Убеждаемся, что ключ полностью удален, а не заменен на None/False
        self.assertNotIn("islgbtq", str(cleaned).lower())

    def test_remove_lgbtq_filter_list_and_variants(self):
        from scraper import remove_lgbtq_filter

        raw_list = [
            {"name": "test1", "is_lgbtq": False, "gender": "Any"},
            {"name": "test2", "isLGBTQ": True, "active": True},
        ]
        cleaned, modified = remove_lgbtq_filter(raw_list)
        self.assertTrue(modified)
        self.assertEqual(len(cleaned), 2)
        self.assertNotIn("is_lgbtq", cleaned[0])
        self.assertNotIn("isLGBTQ", cleaned[1])
        self.assertEqual(cleaned[0]["gender"], "Any")
        self.assertEqual(cleaned[1]["active"], True)


class TestHostingAvailabilityGuardRefined(unittest.TestCase):
    """Тестирование разграничения явного отказа, временного отсутствия и несовпадения проживания."""

    def setUp(self):
        from hosting_guard import HostingAvailabilityGuard
        self.guard = HostingAvailabilityGuard()

    def test_traveling_around_and_abroad_not_rejected(self):
        # Фразы о путешествиях не должны приводить к автоматическому отказу
        sample1 = "I love traveling around Japan and meeting people from different cultures."
        hit1 = self.guard.check_regex_blacklist(sample1)
        self.assertIsNone(hit1, "traveling around не должно отсекаться")

        sample2 = "Traveling abroad has always been my passion. When I'm in Kyoto, I love to host!"
        hit2 = self.guard.check_regex_blacklist(sample2)
        self.assertIsNone(hit2, "traveling abroad не должно отсекаться")

        sample3 = "I am someone who loves traveling through Asia."
        hit3 = self.guard.check_regex_blacklist(sample3)
        self.assertIsNone(hit3, "traveling through не должно отсекаться")

    def test_explicit_refusal_is_rejected(self):
        # Однозначный отказ CAN'T HOST / NOT HOSTING обязан отсекаться
        hit1 = self.guard.check_regex_blacklist("Currently I cannot host anyone due to renovations.")
        self.assertIsNotNone(hit1)
        self.assertIn("cannot host", hit1[0].lower())

        hit2 = self.guard.check_regex_blacklist("Please note: not hosting at the moment.")
        self.assertIsNotNone(hit2)
        self.assertIn("not hosting", hit2[0].lower())

        hit3 = self.guard.check_regex_blacklist("Do not send me requests, I'm busy.")
        self.assertIsNotNone(hit3)

    def test_guest_restrictions_are_rejected(self):
        # Женщины только / только кофе / нет места
        hit1 = self.guard.check_regex_blacklist("I live alone so female guests only please.")
        self.assertIsNotNone(hit1)

        hit2 = self.guard.check_regex_blacklist("I can only meet for coffee or drinks in town.")
        self.assertIsNotNone(hit2)

        hit3 = self.guard.check_regex_blacklist("Living with my parents, no space to host.")
        self.assertIsNotNone(hit3)


class TestSearchSessionCaching(unittest.TestCase):
    """Тестирование кэша Gender и HostingGuard по username в рамках сессии сбора."""

    def test_scraper_session_caching_avoids_repeated_analysis(self):
        from scraper import CouchsurfingScraper
        from unittest.mock import MagicMock

        scraper = CouchsurfingScraper(city="Kyoto", max_scroll_pages=5)
        # Мокаем классификаторы, чтобы отследить количество вызовов
        scraper.gender_classifier.classify = MagicMock(return_value=("Male", "solo", "bio_marker"))
        scraper.hosting_guard.check_regex_blacklist = MagicMock(return_value=None)

        item = {
            "username": "test_host_123",
            "name": "Taro Tanaka",
            "aboutMe": "Software engineer living in Kyoto.",
            "locationSummary": "Kyoto, Japan",
            "totalReferencesCount": 15,
            "isRecentlyActive": True,
        }

        # Первый проход: вызовы должны произойти
        scraper._process_host_from_api(item)
        self.assertIn("test_host_123", scraper.collected_hosts)
        self.assertEqual(scraper.gender_classifier.classify.call_count, 1)
        self.assertEqual(scraper.hosting_guard.check_regex_blacklist.call_count, 1)

        # Второй проход того же профиля (следующий scroll iteration): повторной обработки быть не должно
        scraper._process_host_from_api(item)
        self.assertEqual(scraper.gender_classifier.classify.call_count, 1, "Классификатор не должен вызываться повторно!")
        self.assertEqual(scraper.hosting_guard.check_regex_blacklist.call_count, 1, "Guard не должен вызываться повторно!")


class TestDatabaseSchemaAndTripAssociation(unittest.TestCase):
    """Проверка схемы SQLite и корректности связи trip_hosts."""

    def test_profile_analyses_has_compatibility_score_column(self):
        from database import db_session
        with db_session() as conn:
            cur = conn.cursor()
            cur.execute("PRAGMA table_info(profile_analyses);")
            cols = [r[1] for r in cur.fetchall()]
            self.assertIn("compatibility_score", cols, "Колонка compatibility_score обязана существовать в profile_analyses!")

    def test_trip_hosts_association(self):
        from database import db_session, init_db
        init_db()
        trip_id = "kyoto-trip-3lhdfema5epclndo82mclxklzfytp-3LhDfEmA5Epclndo82MclXkLzFy_tp"
        with db_session() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT OR IGNORE INTO hosts (id, username, name, clean_name, profile_url, stay_request_url, location, first_scraped_at, updated_at)
                VALUES ('unit_kyoto_host_1', 'unit_kyoto_host_1', 'Kenji', 'Kenji', 'http://localhost/u/1', '', 'Kyoto', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP);
                """
            )
            cur.execute(
                """
                INSERT OR IGNORE INTO trip_hosts (trip_id, host_id, city, arrival_date, departure_date, created_at)
                VALUES (?, 'unit_kyoto_host_1', 'Kyoto', '2026-11-19', '2026-11-21', CURRENT_TIMESTAMP);
                """,
                (trip_id,),
            )
            cur.execute("SELECT count(*) FROM trip_hosts WHERE trip_id = ?;", (trip_id,))
            count = cur.fetchone()[0]
            self.assertGreater(count, 0, "Для трипа Киото должны существовать связанные записи в изолированной trip_hosts!")


if __name__ == "__main__":
    unittest.main()
