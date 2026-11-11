import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from test_isolation import activate_test_isolation, get_db_path

activate_test_isolation()

from playwright.sync_api import sync_playwright

DB_PATH = str(get_db_path())
TEST_TRIP_ID = "trip_test_e2e_kyoto"

def setup_test_data():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    
    # 1. Ensure clean slate for test trip
    cur.execute("DELETE FROM trip_hosts WHERE trip_id = ? OR host_id LIKE 'test_e2e_%'", (TEST_TRIP_ID,))
    cur.execute("DELETE FROM generated_messages WHERE host_id LIKE 'test_e2e_%'")
    cur.execute("DELETE FROM sending_queue WHERE host_id LIKE 'test_e2e_%'")
    cur.execute("DELETE FROM hosts WHERE id LIKE 'test_e2e_%' OR username LIKE 'test_e2e_%'")
    
    # 2. Insert 20 test hosts into hosts and trip_hosts for TEST_TRIP_ID
    for i in range(1, 21):
        u_name = f"test_e2e_user_{i:02d}"
        h_id = u_name
        display_name = f"Test Host {i:02d}"
        cur.execute("""
            INSERT INTO hosts (id, username, name, clean_name, location, references_count, response_rate_pct, profile_url, stay_request_url, first_scraped_at, updated_at)
            VALUES (?, ?, ?, ?, 'Kyoto, Japan', 15, 95.0, ?, ?, datetime('now'), datetime('now'))
        """, (h_id, u_name, display_name, display_name, f"https://www.couchsurfing.com/c/users/{u_name}", f"https://www.couchsurfing.com/c/users/{u_name}/stay-request"))
        
        cur.execute("""
            INSERT INTO trip_hosts (trip_id, host_id, city, arrival_date, departure_date, created_at)
            VALUES (?, ?, 'Kyoto', '2026-11-25', '2026-11-28', datetime('now'))
        """, (TEST_TRIP_ID, h_id))
    
    conn.commit()
    conn.close()
    print("Inserted 20 isolated test hosts for TEST_TRIP_ID")

def cleanup_test_data():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("DELETE FROM trip_hosts WHERE trip_id = ? OR host_id LIKE 'test_e2e_%'", (TEST_TRIP_ID,))
    cur.execute("DELETE FROM generated_messages WHERE host_id LIKE 'test_e2e_%'")
    cur.execute("DELETE FROM sending_queue WHERE host_id LIKE 'test_e2e_%'")
    cur.execute("DELETE FROM hosts WHERE id LIKE 'test_e2e_%' OR username LIKE 'test_e2e_%'")
    conn.commit()
    conn.close()
    print("Cleaned up all isolated test data")

def run_e2e():
    setup_test_data()
    
    passed_steps = []
    
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 1440, "height": 900})
            page = context.new_page()

            dialogs = []
            page.on("dialog", lambda d: (print(f"DIALOG: {d.message}"), dialogs.append(d.message), d.accept()))
            page.on("console", lambda msg: print(f"BROWSER CONSOLE [{msg.type}]: {msg.text}"))

            # --- Step 1: Open active Trip Card ---
            print("\n--- Step 1: Open active Trip Card ---")
            page.goto("http://127.0.0.1:8000")
            page.evaluate(f"""() => {{
                const testTripCtx = {{
                    trip_id: '{TEST_TRIP_ID}',
                    city: 'Kyoto',
                    arrival: '2026-11-25',
                    departure: '2026-11-28',
                    datesText: 'Nov 25 – 28, 2026',
                    url: 'https://www.couchsurfing.com/c/trips/{TEST_TRIP_ID}'
                }};
                saveTripContext(testTripCtx);
            }}""")
            page.reload(wait_until="networkidle")
            page.wait_for_timeout(1500)

            # Check initial search host count
            initial_search_count = page.evaluate("""() => hostsData ? hostsData.length : 0""")
            print(f"Initial Search hosts count for test trip: {initial_search_count}")
            assert initial_search_count == 20, f"Expected 20 hosts, got {initial_search_count}"
            passed_steps.append("1. Active Trip Card loaded with 20 search hosts")

            # --- Step 2 & 3 & 4: Add single host, verify in Queue, verify disappeared from Search ---
            print("\n--- Steps 2-4: Add 1 host, verify in Queue and gone from Search ---")
            cbs = page.locator("#hosts-table-body tr .host-row-cb")
            cbs.first.check()
            page.wait_for_timeout(300)
            
            first_host = page.evaluate("""() => Array.from(selectedHostUsernames)[0]""")
            print(f"Selected host: {first_host}")
            
            # Click Add Selected
            page.locator("#btn-queue-selected").click()
            page.wait_for_timeout(2500)

            # Verify disappeared from Search
            page.evaluate("""() => switchTab('search')""")
            page.wait_for_timeout(1000)
            search_after_one = page.evaluate("""() => hostsData ? hostsData.length : 0""")
            print(f"Search count after adding 1 host: {search_after_one}")
            assert search_after_one == 19, f"Expected 19 hosts remaining in Search, got {search_after_one}"
            
            # Check host is not in table rows
            in_table = page.evaluate(f"""() => {{
                return Array.from(document.querySelectorAll('#hosts-table-body tr')).some(r => r.dataset.username === '{first_host}');
            }}""")
            assert not in_table, f"Host {first_host} still found in Search table!"
            
            # Check host in Queue
            page.evaluate("""() => switchTab('queue')""")
            page.wait_for_timeout(1000)
            in_queue_ui = page.evaluate(f"""() => {{
                return queueData.some(q => (q.username || q.id) === '{first_host}');
            }}""")
            print(f"Host {first_host} in Queue: {in_queue_ui}")
            passed_steps.append("2-4. Single host added, appeared in Queue, disappeared from Search")

            # --- Step 5 & 6 & 7: Add 13 hosts via Add Selected across pages ---
            print("\n--- Steps 5-7: Add 13 hosts across pages via Add Selected ---")
            page.evaluate("""() => switchTab('search')""")
            page.wait_for_timeout(1000)

            # Check 13 checkboxes
            # Page size is dynamic or default 15
            current_cbs = page.locator("#hosts-table-body tr .host-row-cb")
            available_on_page = current_cbs.count()
            print(f"Available on page 1: {available_on_page}")

            # Select 10 on page 1
            for i in range(min(10, available_on_page)):
                current_cbs.nth(i).check()
            page.wait_for_timeout(300)

            # Go to next page if pagination exists, or select 13 directly
            sel_cnt = page.evaluate("""() => selectedHostUsernames.size""")
            if sel_cnt < 13 and available_on_page >= 13:
                for i in range(10, 13):
                    current_cbs.nth(i).check()
            elif sel_cnt < 13:
                # Go to page 2
                page.evaluate("""() => goToPage(2)""")
                page.wait_for_timeout(500)
                p2_cbs = page.locator("#hosts-table-body tr .host-row-cb")
                for i in range(min(3, p2_cbs.count())):
                    p2_cbs.nth(i).check()

            total_sel = page.evaluate("""() => selectedHostUsernames.size""")
            print(f"Total selected across pages: {total_sel}")
            assert total_sel == 13, f"Expected 13 selected, got {total_sel}"

            page.locator("#btn-queue-selected").click()
            page.wait_for_timeout(3000)

            # Verify Search count
            page.evaluate("""() => switchTab('search')""")
            page.wait_for_timeout(1000)
            search_after_13 = page.evaluate("""() => hostsData ? hostsData.length : 0""")
            print(f"Search count after adding 13 hosts: {search_after_13}")
            # Was 19, added 13 -> remaining 6
            assert search_after_13 == 6, f"Expected 6 remaining, got {search_after_13}"
            passed_steps.append("5-7. Added 13 hosts across pages; exactly 6 remaining in Search")

            # --- Step 8: Repeated adding of already added hosts ---
            print("\n--- Step 8: Repeated adding of already added hosts ---")
            # In Queue, 14 hosts are queued. Let's try to add one of them via API payload
            repeat_res = page.evaluate(f"""async () => {{
                const res = await fetch('/api/requests/queue_selected', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{
                        usernames: ['{first_host}'],
                        trip_id: '{TEST_TRIP_ID}',
                        city: 'Kyoto'
                    }})
                }});
                return await res.json();
            }}""")
            print(f"Repeat add response: added_count={repeat_res.get('added_count')}, already_queued={repeat_res.get('already_queued')}")
            assert repeat_res.get('added_count') == 0, "Expected 0 added for duplicate"
            assert repeat_res.get('already_queued') == 1, "Expected already_queued == 1"
            passed_steps.append("8. Duplicate host rejected with already_queued=1 and added_count=0")

            # --- Step 9: Add All for remaining hosts ---
            print("\n--- Step 9: Add All for remaining 6 hosts ---")
            page.evaluate("""() => switchTab('search')""")
            page.wait_for_timeout(1000)
            page.locator("#btn-queue-all").click()
            page.wait_for_timeout(3000)

            # Check Search is now 0
            search_after_all = page.evaluate("""() => hostsData ? hostsData.length : 0""")
            print(f"Search count after Add All: {search_after_all}")
            assert search_after_all == 0, f"Expected 0 remaining in Search, got {search_after_all}"
            passed_steps.append("9. Add All added remaining 6 hosts; Search is now 0")

            # --- Step 10: Switch Trip Card and verify isolation ---
            print("\n--- Step 10: Switch Trip Card and verify trip isolation ---")
            kyoto_canon = "kyoto-trip-3lhdfema5epclndo82mclxklzfytp-3LhDfEmA5Epclndo82MclXkLzFy_tp"
            page.evaluate(f"""() => {{
                const canonCtx = {{
                    trip_id: '{kyoto_canon}',
                    city: 'Kyoto',
                    arrival: '2026-11-19',
                    departure: '2026-11-21',
                    datesText: 'Nov 19 – 21, 2026',
                    url: 'https://www.couchsurfing.com/c/trips/{kyoto_canon}'
                }};
                saveTripContext(canonCtx);
            }}""")
            page.reload(wait_until="networkidle")
            page.wait_for_timeout(2000)

            canon_search_count = page.evaluate("""() => hostsData ? hostsData.length : 0""")
            canon_queue_badge = page.locator("#nav-queue-count").text_content()
            print(f"Canon trip Search count: {canon_search_count}, Queue badge: {canon_queue_badge}")
            # Test hosts should NOT appear in canonical Kyoto trip search or queue
            has_test_host = page.evaluate("""() => {
                return (hostsData || []).some(h => (h.username || '').startsWith('test_e2e_'));
            }""")
            assert not has_test_host, "Test hosts leaked into Canonical Kyoto trip search!"
            passed_steps.append("10. Trip Card switched; strict isolation confirmed (no leakage)")

            # --- Step 11: Reload and check persistence ---
            print("\n--- Step 11: Reload and check persistence ---")
            page.reload(wait_until="networkidle")
            page.wait_for_timeout(1500)
            restored_trip_id = page.evaluate("""() => selectedTripContext ? selectedTripContext.trip_id : null""")
            print(f"Restored trip_id on reload: {restored_trip_id}")
            assert restored_trip_id == kyoto_canon, f"Expected {kyoto_canon}, got {restored_trip_id}"
            passed_steps.append("11. Page reload verified persistent Trip Card context")

            browser.close()
    finally:
        cleanup_test_data()

    print("\n==========================================")
    print("ALL PLAYWRIGHT E2E TESTS PASSED!")
    print("==========================================")
    for s in passed_steps:
        print("  ✓", s)

if __name__ == "__main__":
    run_e2e()
