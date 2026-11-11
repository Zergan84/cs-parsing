import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from test_isolation import activate_test_isolation, get_db_path, get_output_dir

activate_test_isolation()

from playwright.sync_api import sync_playwright

DB_PATH = str(get_db_path())

def get_db_stats():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM sending_queue")
    total_sq = cur.fetchone()[0]
    cur.execute("SELECT status, count(*) FROM sending_queue GROUP BY status")
    by_status = dict(cur.fetchall())
    conn.close()
    return {"total": total_sq, "by_status": by_status}

def run_investigation():
    initial_db = get_db_stats()
    print("=== INITIAL DB STATE ===")
    print(json.dumps(initial_db, indent=2))

    network_log = []
    console_log = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        page = context.new_page()

        # Listen to console & dialogs
        page.on("console", lambda msg: console_log.append(f"[{msg.type}] {msg.text}"))
        page.on("pageerror", lambda err: console_log.append(f"[PAGEERROR] {err}"))
        page.on("dialog", lambda d: (print(f"[BROWSER DIALOG: {d.type}] {d.message}"), d.accept()))

        # Listen to network
        def on_request(req):
            if "/api/" in req.url:
                network_log.append({
                    "event": "request",
                    "time": time.time(),
                    "method": req.method,
                    "url": req.url,
                    "post_data": req.post_data
                })

        def on_response(resp):
            if "/api/" in resp.url:
                try:
                    body = resp.json()
                except Exception:
                    body = resp.text()[:200]
                network_log.append({
                    "event": "response",
                    "time": time.time(),
                    "method": resp.request.method,
                    "url": resp.url,
                    "status": resp.status,
                    "body": body
                })

        page.on("request", on_request)
        page.on("response", on_response)

        print("\n--- Navigating to http://127.0.0.1:8000 ---")
        page.goto("http://127.0.0.1:8000", wait_until="networkidle")
        page.wait_for_timeout(2000)

        # 1. Click on Kyoto Trip Card
        print("Selecting Kyoto Trip Card in UI...")
        kyoto_card = page.locator("button[data-trip-city='Kyoto']").first
        if kyoto_card.count() > 0:
            kyoto_card.click()
            page.wait_for_timeout(1500)
            print("Clicked Kyoto trip card in UI")
        else:
            print("Kyoto trip card button not found by city, selecting via JS...")
            page.evaluate("""() => {
                const btn = Array.from(document.querySelectorAll('button')).find(b => (b.textContent || '').includes('Kyoto'));
                if (btn) btn.click();
            }""")
            page.wait_for_timeout(1500)

        badge_init = page.locator("#nav-queue-count").text_content()
        print(f"Initial #nav-queue-count: {badge_init}")

        # Check Trip Cards
        trips = page.evaluate("""() => {
            return {
                selectedTripContext: typeof selectedTripContext !== 'undefined' ? selectedTripContext : null,
                selectedHostCity: typeof selectedHostCity !== 'undefined' ? selectedHostCity : null,
                hostsCount: typeof hostsData !== 'undefined' ? hostsData.length : 0,
                selectedCount: typeof selectedHostUsernames !== 'undefined' ? selectedHostUsernames.size : 0,
                selectedUsernames: typeof selectedHostUsernames !== 'undefined' ? Array.from(selectedHostUsernames) : []
            };
        }""")
        print("Frontend state after trip selection:", json.dumps(trips, indent=2))

        # Check first page host rows
        first_page_hosts = page.evaluate("""() => {
            const rows = document.querySelectorAll('#hosts-table-body tr');
            return Array.from(rows).map(r => ({
                username: r.dataset.username,
                checkbox: r.querySelector('.host-row-cb') ? r.querySelector('.host-row-cb').checked : null,
                text: r.innerText.split('\\n')[0]
            }));
        }""")
        print(f"Found {len(first_page_hosts)} rows on page 1:")
        for h in first_page_hosts[:5]:
            print("  ", h)

        # ------------------------------------------------------------------
        # TEST A: Single host selection -> Add Selected
        # ------------------------------------------------------------------
        print("\n=======================================================")
        print("TEST A: Single host selection -> Add Selected")
        print("=======================================================")
        net_len_before = len(network_log)
        
        # Click first checkbox
        first_cb = page.locator("#hosts-table-body tr .host-row-cb").first
        first_cb.check()
        page.wait_for_timeout(500)

        sel_state_a = page.evaluate("""() => Array.from(selectedHostUsernames || [])""")
        btn_text_a = page.locator("#btn-queue-selected-text").text_content()
        print(f"Selected usernames: {sel_state_a}, Button text: {btn_text_a}")

        badge_before_a = page.locator("#nav-queue-count").text_content()
        db_before_a = get_db_stats()

        # Click Add Selected
        print("Clicking #btn-queue-selected...")
        page.locator("#btn-queue-selected").click()
        page.wait_for_timeout(3000)

        badge_after_a = page.locator("#nav-queue-count").text_content()
        db_after_a = get_db_stats()
        print(f"Badge before: {badge_before_a} -> Badge after: {badge_after_a}")
        print(f"DB total before: {db_before_a['total']} -> DB total after: {db_after_a['total']}")

        print("Network traffic during Test A:")
        for ev in network_log[net_len_before:]:
            print(" ", ev.get("method"), ev.get("url"), ev.get("status") or ev.get("post_data"), "->", str(ev.get("body"))[:120] if "body" in ev else "")

        # ------------------------------------------------------------------
        # TEST B: 13 hosts on one page -> Add Selected
        # ------------------------------------------------------------------
        print("\n=======================================================")
        print("TEST B: Multiple hosts selection -> Add Selected")
        print("=======================================================")
        net_len_before = len(network_log)

        # Navigate back to search if switched
        page.evaluate("""() => switchTab('search')""")
        page.wait_for_timeout(1000)

        # Check all available checkboxes on current page
        cbs = page.locator("#hosts-table-body tr .host-row-cb")
        cb_count = cbs.count()
        print(f"Checking all {cb_count} checkboxes on page...")
        for i in range(cb_count):
            cbs.nth(i).check()
        page.wait_for_timeout(500)

        sel_state_b = page.evaluate("""() => Array.from(selectedHostUsernames || [])""")
        print(f"Selected usernames count: {len(sel_state_b)}: {sel_state_b}")

        badge_before_b = page.locator("#nav-queue-count").text_content()
        db_before_b = get_db_stats()

        print("Clicking #btn-queue-selected...")
        page.locator("#btn-queue-selected").click()
        page.wait_for_timeout(3000)

        badge_after_b = page.locator("#nav-queue-count").text_content()
        db_after_b = get_db_stats()
        print(f"Badge before: {badge_before_b} -> Badge after: {badge_after_b}")
        print(f"DB total before: {db_before_b['total']} -> DB total after: {db_after_b['total']}")

        print("Network traffic during Test B:")
        for ev in network_log[net_len_before:]:
            print(" ", ev.get("method"), ev.get("url"), ev.get("status") or ev.get("post_data"), "->", str(ev.get("body"))[:120] if "body" in ev else "")

        # ------------------------------------------------------------------
        # TEST C: Check across two pages (pagination)
        # ------------------------------------------------------------------
        print("\n=======================================================")
        print("TEST C: Selection across pages")
        print("=======================================================")
        page.evaluate("""() => switchTab('search')""")
        page.wait_for_timeout(1000)

        # Let's inspect pager
        pager_info = page.evaluate("""() => {
            return {
                currentPage: typeof hostsCurrentPage !== 'undefined' ? hostsCurrentPage : null,
                totalHosts: typeof hostsData !== 'undefined' ? hostsData.length : 0,
                selected: typeof selectedHostUsernames !== 'undefined' ? Array.from(selectedHostUsernames) : []
            };
        }""")
        print("Pager info before page change:", pager_info)

        # ------------------------------------------------------------------
        # TEST E: Repeated adding of already queued hosts (reproducing 2013 -> 2217 -> 2013)
        # ------------------------------------------------------------------
        print("\n=======================================================")
        print("TEST E: Repeated adding of already queued hosts (Check 2013 -> 2217 -> 2013)")
        print("=======================================================")
        net_len_before = len(network_log)

        # Let's see what happens if we select some hosts and click Add Selected
        # We also observe DOM mutation on #nav-queue-count
        dom_values = []
        page.evaluate("""() => {
            window.__badgeValues = [];
            const el = document.getElementById('nav-queue-count');
            if (el) {
                const obs = new MutationObserver(() => {
                    window.__badgeValues.push({
                        time: Date.now(),
                        val: el.textContent
                    });
                });
                obs.observe(el, { characterData: true, childList: true, subtree: true });
            }
        }""")

        # Select first row again
        cbs = page.locator("#hosts-table-body tr .host-row-cb")
        if cbs.count() > 0:
            cbs.first.check()
            page.wait_for_timeout(300)

        print("Clicking #btn-queue-selected while observing badge mutations...")
        page.locator("#btn-queue-selected").click()
        page.wait_for_timeout(3000)

        observed_mutations = page.evaluate("""() => window.__badgeValues || []""")
        print("Observed #nav-queue-count mutations:", observed_mutations)
        final_badge = page.locator("#nav-queue-count").text_content()
        print(f"Final badge value: {final_badge}")

        print("Network traffic during Test E:")
        for ev in network_log[net_len_before:]:
            print(" ", ev.get("method"), ev.get("url"), ev.get("status") or ev.get("post_data"), "->", str(ev.get("body"))[:120] if "body" in ev else "")

        # Save full logs into isolated test output dir
        out_dir = get_output_dir()
        (out_dir / "backups").mkdir(parents=True, exist_ok=True)
        with open(out_dir / "backups" / "playwright_test_network_log.json", "w") as f:
            json.dump(network_log, f, indent=2)
        with open(out_dir / "backups" / "playwright_test_console_log.json", "w") as f:
            json.dump(console_log, f, indent=2)

        browser.close()

if __name__ == "__main__":
    run_investigation()
