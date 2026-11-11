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

def get_db_stats():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM sending_queue")
    total_sq = cur.fetchone()[0]
    cur.execute("SELECT status, count(*) FROM sending_queue GROUP BY status")
    by_status = dict(cur.fetchall())
    conn.close()
    return {"total": total_sq, "by_status": by_status}

def run_test():
    print("=== INITIAL DB STATE ===")
    print(json.dumps(get_db_stats(), indent=2))

    network_log = []
    console_log = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        page = context.new_page()

        page.on("console", lambda msg: console_log.append(f"[{msg.type}] {msg.text}"))
        page.on("pageerror", lambda err: console_log.append(f"[PAGEERROR] {err}"))
        page.on("dialog", lambda d: (print(f"[DIALOG {d.type}] {d.message}"), d.accept()))

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
                    try:
                        body = resp.text()[:200]
                    except Exception:
                        body = "<unavailable>"
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

        # 1. Set localStorage with active Kyoto trip context
        page.goto("http://127.0.0.1:8000")
        kyoto_trip_ctx = {
            "trip_id": "kyoto-trip-3lhdfema5epclndo82mclxklzfytp-3LhDfEmA5Epclndo82MclXkLzFy_tp",
            "city": "Kyoto",
            "arrival": "2026-11-19",
            "departure": "2026-11-21",
            "datesText": "Nov 19 – 21, 2026",
            "url": "https://www.couchsurfing.com/c/trips/kyoto-trip-3lhdfema5epclndo82mclxklzfytp-3LhDfEmA5Epclndo82MclXkLzFy_tp"
        }
        page.evaluate(f"""(ctx) => {{
            localStorage.setItem('cs_selected_trip', JSON.stringify(ctx));
            localStorage.setItem('cs_selected_city', 'Kyoto');
        }}""", kyoto_trip_ctx)

        # Reload with active context
        page.reload(wait_until="networkidle")
        page.wait_for_timeout(2000)

        # Verify frontend state
        fe_state = page.evaluate("""() => {
            return {
                selectedTripContext: typeof selectedTripContext !== 'undefined' ? selectedTripContext : null,
                selectedHostCity: typeof selectedHostCity !== 'undefined' ? selectedHostCity : null,
                hostsCount: typeof hostsData !== 'undefined' ? hostsData.length : 0,
                selectedCount: typeof selectedHostUsernames !== 'undefined' ? selectedHostUsernames.size : 0,
                selectedUsernames: typeof selectedHostUsernames !== 'undefined' ? Array.from(selectedHostUsernames) : []
            };
        }""")
        print("Frontend state on reload with active trip:")
        print(json.dumps(fe_state, indent=2))

        # Check rows in table
        rows_data = page.evaluate("""() => {
            const trs = document.querySelectorAll('#hosts-table-body tr');
            return Array.from(trs).map(r => ({
                username: r.dataset.username,
                text: r.innerText.replace(/\\s+/g, ' ').trim()
            }));
        }""")
        print(f"Table rows count on page 1: {len(rows_data)}")
        for r in rows_data[:5]:
            print("  ", r["username"], "->", r["text"][:80])

        # ------------------------------------------------------------------
        # TEST A: Select 1 host -> Add Selected
        # ------------------------------------------------------------------
        print("\n=======================================================")
        print("TEST A: Select 1 host -> Add Selected (With active trip)")
        print("=======================================================")
        net_before = len(network_log)

        cbs = page.locator("#hosts-table-body tr .host-row-cb")
        first_u = rows_data[0]["username"]
        print(f"Checking first row checkbox for @{first_u}...")
        cbs.first.check()
        page.wait_for_timeout(300)

        sel = page.evaluate("""() => Array.from(selectedHostUsernames)""")
        btn_txt = page.locator("#btn-queue-selected-text").text_content()
        badge_before = page.locator("#nav-queue-count").text_content()
        print(f"Selected: {sel}, Button text: {btn_txt}, Badge before: {badge_before}")

        print("Clicking Add Selected...")
        page.locator("#btn-queue-selected").click()
        page.wait_for_timeout(3000)

        badge_after = page.locator("#nav-queue-count").text_content()
        print(f"Badge after Test A: {badge_after}")

        print("Network traffic during Test A:")
        for ev in network_log[net_before:]:
            m = ev.get("method")
            u = ev.get("url")
            st = ev.get("status") or ev.get("post_data")
            b = str(ev.get("body"))[:100] if "body" in ev else ""
            print(f"  {m} {u} {st} -> {b}")

        # ------------------------------------------------------------------
        # TEST B: Select 13 hosts -> Add Selected
        # ------------------------------------------------------------------
        print("\n=======================================================")
        print("TEST B: Select 13 hosts -> Add Selected (With active trip)")
        print("=======================================================")
        net_before = len(network_log)

        # Switch back to Search tab
        page.evaluate("""() => switchTab('search')""")
        page.wait_for_timeout(1000)

        cbs = page.locator("#hosts-table-body tr .host-row-cb")
        cb_count = min(13, cbs.count())
        print(f"Selecting {cb_count} checkboxes...")
        for i in range(cb_count):
            cbs.nth(i).check()
        page.wait_for_timeout(500)

        sel_b = page.evaluate("""() => Array.from(selectedHostUsernames)""")
        print(f"Selected {len(sel_b)} hosts: {sel_b}")

        badge_before_b = page.locator("#nav-queue-count").text_content()
        print("Clicking Add Selected...")
        page.locator("#btn-queue-selected").click()
        page.wait_for_timeout(3000)

        badge_after_b = page.locator("#nav-queue-count").text_content()
        print(f"Badge before: {badge_before_b} -> Badge after: {badge_after_b}")

        print("Network traffic during Test B:")
        for ev in network_log[net_before:]:
            m = ev.get("method")
            u = ev.get("url")
            st = ev.get("status") or ev.get("post_data")
            b = str(ev.get("body"))[:100] if "body" in ev else ""
            print(f"  {m} {u} {st} -> {b}")

        # ------------------------------------------------------------------
        # TEST E: Check badge mutation (2013 -> 2217 -> 2013)
        # ------------------------------------------------------------------
        print("\n=======================================================")
        print("TEST E: Check badge mutations on repeated Add Selected")
        print("=======================================================")
        net_before = len(network_log)

        page.evaluate("""() => switchTab('search')""")
        page.wait_for_timeout(1000)

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

        cbs = page.locator("#hosts-table-body tr .host-row-cb")
        if cbs.count() > 0:
            cbs.first.check()
            page.wait_for_timeout(300)

        print("Clicking Add Selected...")
        page.locator("#btn-queue-selected").click()
        page.wait_for_timeout(3000)

        badge_mutations = page.evaluate("""() => window.__badgeValues || []""")
        print("DOM Mutation log of #nav-queue-count:", badge_mutations)
        print("Final badge text:", page.locator("#nav-queue-count").text_content())

        print("Network traffic during Test E:")
        for ev in network_log[net_before:]:
            m = ev.get("method")
            u = ev.get("url")
            st = ev.get("status") or ev.get("post_data")
            b = str(ev.get("body"))[:100] if "body" in ev else ""
            print(f"  {m} {u} {st} -> {b}")

        browser.close()

if __name__ == "__main__":
    run_test()
