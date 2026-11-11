import json
import time
import threading
import sys
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from test_isolation import activate_test_isolation, get_output_dir

activate_test_isolation()

import uvicorn
from playwright.sync_api import sync_playwright
from app import app, db_session, queue_manager

PORT = 8765

def run_e2e_test():
    # 1. Start live uvicorn server in a background thread
    config = uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="error")
    server = uvicorn.Server(config)
    srv_thread = threading.Thread(target=server.run, daemon=True)
    srv_thread.start()

    time.sleep(1.5)

    captured_request = {}
    captured_response = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1280, "height": 900})

        # Inject trip context before navigation
        context.add_init_script("""
            localStorage.setItem('cs_selected_trip', JSON.stringify({
                "trip_id": "kyoto-trip-3lhdfema5epclndo82mclxklzfytp-3LhDfEmA5Epclndo82MclXkLzFy_tp",
                "city": "Kyoto",
                "arrival": "2026-11-18",
                "departure": "2026-11-21",
                "datesText": "2026-11-18 – 2026-11-21",
                "url": "https://www.couchsurfing.com/c/trips/kyoto-trip-3lhdfema5epclndo82mclxklzfytp-3LhDfEmA5Epclndo82MclXkLzFy_tp"
            }));
            localStorage.setItem('cs_selected_city', 'Kyoto');
            localStorage.setItem('cs_active_tab', 'search');
        """)

        page = context.new_page()
        page.on("dialog", lambda d: print(f"[BROWSER DIALOG] {d.type}: {d.message}") or d.accept())

        # Intercept network requests & responses
        def on_request(req):
            if "/api/requests/queue_selected" in req.url:
                captured_request["url"] = req.url
                captured_request["method"] = req.method
                captured_request["headers"] = req.headers
                captured_request["post_data"] = req.post_data

        def on_response(res):
            if "/api/requests/queue_selected" in res.url:
                captured_response["status"] = res.status
                try:
                    captured_response["json"] = res.json()
                except Exception:
                    captured_response["body"] = res.text()

        page.on("request", on_request)
        page.on("response", on_response)

        print("[1] Opening application page in Playwright Chromium...")
        page.goto(f"http://127.0.0.1:{PORT}/", wait_until="networkidle")
        page.wait_for_timeout(2000)

        # Ensure search tab is active
        page.evaluate("switchTab('search')")
        page.wait_for_timeout(1500)

        # Wait for table rows to render
        page.wait_for_selector("#hosts-table-body tr", timeout=10000)
        rows = page.locator("#hosts-table-body tr")
        row_count = rows.count()
        print(f"[1] Search table rendered with {row_count} rows.")
        assert row_count >= 3, "At least 3 rows must be present in Search table"

        # [2] Select 3 new unsent hosts
        selected_usernames = []
        for i in range(3):
            row = rows.nth(i)
            u = row.get_attribute("data-username")
            selected_usernames.append(u)
            cb = row.locator(".host-row-cb")
            cb.check()

        print(f"[2] Selected 3 hosts: {selected_usernames}")

        # [3] Verify counter shows 3
        btn_queue = page.locator("#btn-queue-selected")
        btn_text = page.locator("#btn-queue-selected-text").inner_text()
        print(f"[3] Queue button text: '{btn_text}'")
        assert "3" in btn_text, f"Expected 3 in button text, got: {btn_text}"

        # [4] Click + Queue
        print("[4] Clicking '+ Queue' button...")
        btn_queue.click()

        # [5] & [6] Wait for network request and inspect payload/response
        page.wait_for_timeout(3000)
        print("\n--- [5 & 6] ACTUAL HTTP REQUEST / RESPONSE CAPTURED ---")
        print("HTTP Method:", captured_request.get("method"))
        print("URL:", captured_request.get("url"))
        raw_payload = captured_request.get("post_data")
        print("Payload (raw JSON):", raw_payload)
        parsed_payload = json.loads(raw_payload) if raw_payload else {}
        print("Payload usernames:", parsed_payload.get("usernames"))
        print("Payload trip_id:", parsed_payload.get("trip_id"))
        print("Response HTTP Status:", captured_response.get("status"))
        resp_json = captured_response.get("json", {})
        print("Response JSON:\n", json.dumps(resp_json, ensure_ascii=False, indent=2))
        
        assert captured_response.get("status") == 200, "Expected HTTP 200"
        assert resp_json.get("added_count") == 3, f"Expected 3 added, got {resp_json.get('added_count')}"

        # [7] Verify three records appeared in Queue on page 1
        page.wait_for_timeout(2000)
        page.evaluate("switchTab('queue'); setQueueFilter('draft');")
        page.wait_for_timeout(2500)
        queue_cards = page.locator("#queue-cards-container .message-card")
        q_count = queue_cards.count()
        print(f"\n[7] Queue Page 1 cards count: {q_count}")
        card_usernames = []
        for i in range(min(10, q_count)):
            c_u = queue_cards.nth(i).get_attribute("data-username")
            if c_u:
                card_usernames.append(c_u.lower())
        print("[7] Visible Queue card usernames on Page 1:", card_usernames)
        for u in selected_usernames:
            assert u.lower() in card_usernames, f"Host {u} MUST appear on Page 1 of Queue"
        print("[7] All 3 hosts successfully verified on Page 1 of Queue!")

        # [8] Verify they disappeared from Search
        page.evaluate("switchTab('search')")
        page.wait_for_timeout(1500)
        new_search_rows = page.locator("#hosts-table-body tr")
        current_search_users = []
        for i in range(new_search_rows.count()):
            u = new_search_rows.nth(i).get_attribute("data-username")
            if u:
                current_search_users.append(u.lower())
        print("[8] Current Search top usernames:", current_search_users[:5])
        for u in selected_usernames:
            assert u.lower() not in current_search_users, f"Host {u} MUST be removed from Search"
        print("[8] All 3 hosts successfully removed from Search table!")

        # [9] & [10] Reload page and verify state persistence
        print("\n[9 & 10] Reloading browser page...")
        page.reload(wait_until="networkidle")
        page.wait_for_timeout(2500)

        # Check search still does not contain them
        reloaded_search_users = []
        for i in range(page.locator("#hosts-table-body tr").count()):
            u = page.locator("#hosts-table-body tr").nth(i).get_attribute("data-username")
            if u:
                reloaded_search_users.append(u.lower())
        for u in selected_usernames:
            assert u.lower() not in reloaded_search_users, f"Host {u} must remain absent from Search after reload"

        # Check Queue still has them
        page.evaluate("switchTab('queue'); setQueueFilter('draft');")
        page.wait_for_timeout(2500)
        reloaded_q_users = []
        for i in range(page.locator("#queue-cards-container .message-card").count()):
            u = page.locator("#queue-cards-container .message-card").nth(i).get_attribute("data-username")
            if u:
                reloaded_q_users.append(u.lower())
        print("[10] Queue cards after reload:", reloaded_q_users[:5])
        for u in selected_usernames:
            assert u.lower() in reloaded_q_users, f"Host {u} must persist in Queue after page reload"
        print("[10] State successfully persisted across browser reload!")

        # [11] Switch to another Trip Card (trip_kyoto_nov_25_28)
        print("\n[11] Switching to second Trip Card ('trip_kyoto_nov_25_28')...")
        page.evaluate("""
            const otherTripContext = {
                trip_id: 'trip_kyoto_nov_25_28',
                city: 'Kyoto',
                arrival: '2026-11-25',
                departure: '2026-11-28',
                datesText: '2026-11-25 – 2026-11-28',
                url: 'https://www.couchsurfing.com/c/trips/trip_kyoto_nov_25_28'
            };
            saveTripContext(otherTripContext);
            loadQueue();
        """)
        page.wait_for_timeout(2500)

        other_trip_q_users = []
        for i in range(page.locator("#queue-cards-container .message-card").count()):
            u = page.locator("#queue-cards-container .message-card").nth(i).get_attribute("data-username")
            if u:
                other_trip_q_users.append(u.lower())
        print("[11] Other trip Queue card usernames:", other_trip_q_users)
        for u in selected_usernames:
            assert u.lower() not in other_trip_q_users, f"Host {u} MUST NOT appear in other Trip Card!"
        print("[11] Isolation confirmed! None of the 3 hosts leaked to the other Trip Card.")

        browser.close()

    # SQL check of trip_hosts
    print("\n--- SQL VERIFICATION OF trip_hosts ---")
    with db_session() as conn:
        cur = conn.cursor()
        for u in selected_usernames:
            cur.execute("SELECT trip_id, host_id, created_at FROM trip_hosts WHERE LOWER(host_id) = LOWER(?);", (u,))
            rows = [tuple(r) for r in cur.fetchall()]
            print(f"Host '{u}' in trip_hosts:", rows)
            assert len(rows) == 1, f"Host {u} must have exactly 1 link in trip_hosts"
            assert rows[0][0] == "kyoto-trip-3lhdfema5epclndo82mclxklzfytp-3LhDfEmA5Epclndo82MclXkLzFy_tp"

    # Cleanup test records
    print("\n--- CLEANUP TEST DATA ---")
    with db_session() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM trip_hosts WHERE host_id IN (?, ?, ?);", tuple(selected_usernames))
        cur.execute("DELETE FROM sending_queue WHERE host_id IN (?, ?, ?);", tuple(selected_usernames))
        cur.execute("DELETE FROM generated_messages WHERE host_id IN (?, ?, ?);", tuple(selected_usernames))

    q_file = get_output_dir() / "requests_queue.json"
    with open(q_file, "r", encoding="utf-8") as f:
        items = json.load(f)
    items = [x for x in items if (x.get("username") or "").lower() not in [u.lower() for u in selected_usernames]]
    with open(q_file, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)

    print("ALL CLEANUP COMPLETED. BASELINE DATA RESTORED.")
    print(">>> PLAYWRIGHT REAL BROWSER E2E TEST: 100% SUCCESS <<<")

if __name__ == "__main__":
    run_e2e_test()
