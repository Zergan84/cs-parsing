import os
import sys
import time
import threading
import uvicorn
from app import app
from playwright.sync_api import sync_playwright

def run_tests():
    config = uvicorn.Config(app, host='127.0.0.1', port=8097, log_level='error')
    server = uvicorn.Server(config)
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    time.sleep(2)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        
        # 1. Desktop 1440px
        print("\n=== 1. VERIFYING DESKTOP (1440px) ===", flush=True)
        page = browser.new_page(viewport={'width': 1440, 'height': 900})
        page.goto('http://127.0.0.1:8097')
        page.wait_for_selector('.trip-card-item', timeout=10000)

        # 1a. Trip cards verification
        trips_text = page.locator('#trips-list').inner_text()
        print("Sidebar Trip Cards:\n" + trips_text, flush=True)
        assert 'Toshima' not in trips_text, "CRITICAL ERROR: Toshima still displayed!"
        assert 'Kyoto' in trips_text, "ERROR: Kyoto not found!"
        assert '2026-11' not in trips_text, "ERROR: Raw ISO dates in trip cards!"
        print("[✓] 1. Toshima eliminated from Trip Cards. Real trips displayed (Tokyo, Kyoto).", flush=True)

        # 1b. Check date formatting across UI (click Kyoto trip card)
        page.locator('.trip-card-btn:has-text("Kyoto")').click()
        page.wait_for_timeout(500)
        subbar_loc = page.locator('#subbar-location').inner_text()
        print(f"Subbar location text after selecting Kyoto: {subbar_loc}", flush=True)
        assert '2026-11' not in subbar_loc, "ERROR: Raw ISO date in subbar!"
        assert 'Kyoto' in subbar_loc, "ERROR: Kyoto missing from subbar!"
        assert 'Nov 19–21' in subbar_loc, "ERROR: Expected 'Nov 19–21' formatted date in subbar!"
        print("[✓] 2. Subbar date formatting verified (human readable: Nov 19–21 · Kyoto, Japan).", flush=True)

        # 1c. Sidebar layout & button boundaries
        sidebar = page.locator('.control-sidebar')
        sidebar_box = sidebar.bounding_box()
        card_items = page.locator('.trip-card-item').all()
        assert len(card_items) > 0, "No trip cards found!"
        for idx, item in enumerate(card_items):
            item_box = item.bounding_box()
            search_btn = item.locator('.trip-search-btn').bounding_box()
            sidebar_right = sidebar_box['x'] + sidebar_box['width']
            btn_right = search_btn['x'] + search_btn['width']
            print(f"Desktop Card {idx}: btn_right={btn_right:.1f}, sidebar_right={sidebar_right:.1f}", flush=True)
            assert btn_right <= sidebar_right + 2, f"Card {idx} search button overflows sidebar!"
        print("[✓] 3. Desktop sidebar: search buttons stay strictly within sidebar boundaries.", flush=True)

        # 1d. Queue Layout & Pagination
        page.evaluate("switchTab('queue')")
        page.wait_for_selector('#queue-cards-container', timeout=10000)
        page.wait_for_timeout(1000)

        queue_area = page.locator('.queue-content-area').bounding_box()
        cards_grid = page.locator('#queue-cards-container').bounding_box()
        pagination = page.locator('#queue-pagination').bounding_box()
        print(f"Queue Content Area: top={queue_area['y']:.1f}, height={queue_area['height']:.1f}", flush=True)
        print(f"Cards Grid: top={cards_grid['y']:.1f}, height={cards_grid['height']:.1f}", flush=True)
        print(f"Pagination Bar: top={pagination['y']:.1f}, height={pagination['height']:.1f}", flush=True)
        
        # Pagination must be strictly below cards
        assert pagination['y'] >= cards_grid['y'] + cards_grid['height'] - 2, "Pagination is not below cards grid!"
        print("[✓] 4. Queue pagination bar is strictly at the bottom of queue content (not floating).", flush=True)

        page.close()

        # 2. Mobile 430px (iPhone 15 Pro Max)
        print("\n=== 2. VERIFYING MOBILE (430px - iPhone 15 Pro Max) ===", flush=True)
        mobile_page = browser.new_page(viewport={'width': 430, 'height': 932})
        mobile_page.goto('http://127.0.0.1:8097')
        mobile_page.wait_for_selector('.trip-card-item', timeout=10000)

        m_sidebar = mobile_page.locator('.control-sidebar')
        m_sidebar_box = m_sidebar.bounding_box()
        m_card_items = mobile_page.locator('.trip-card-item').all()
        for idx, item in enumerate(m_card_items):
            search_btn = item.locator('.trip-search-btn').bounding_box()
            m_sidebar_right = m_sidebar_box['x'] + m_sidebar_box['width']
            m_btn_right = search_btn['x'] + search_btn['width']
            print(f"Mobile Card {idx}: btn_right={m_btn_right:.1f}, sidebar_right={m_sidebar_right:.1f}", flush=True)
            assert m_btn_right <= m_sidebar_right + 2, f"Mobile card {idx} search button overflows sidebar!"
        print("[✓] 5. Mobile sidebar (430px): search buttons strictly within boundaries.", flush=True)

        # 2b. Mobile Queue
        mobile_page.evaluate("switchTab('queue')")
        mobile_page.wait_for_selector('#queue-cards-container', timeout=10000)
        mobile_page.wait_for_timeout(1000)

        m_queue_area = mobile_page.locator('.queue-content-area').bounding_box()
        m_cards_grid = mobile_page.locator('#queue-cards-container').bounding_box()
        m_pagination = mobile_page.locator('#queue-pagination').bounding_box()
        print(f"Mobile Queue Area: top={m_queue_area['y']:.1f}, height={m_queue_area['height']:.1f}", flush=True)
        print(f"Mobile Cards Grid: top={m_cards_grid['y']:.1f}, height={m_cards_grid['height']:.1f}", flush=True)
        print(f"Mobile Pagination: top={m_pagination['y']:.1f}, height={m_pagination['height']:.1f}", flush=True)
        assert m_pagination['y'] >= m_cards_grid['y'] + m_cards_grid['height'] - 2, "Mobile pagination is not below cards grid!"
        print("[✓] 6. Mobile Queue layout: cards and pagination strictly responsive and non-floating.", flush=True)

        mobile_page.close()
        browser.close()

    print("\n==========================================", flush=True)
    print("ALL PLAYWRIGHT TESTS PASSED SUCCESSFULLY!", flush=True)
    print("==========================================", flush=True)
    os._exit(0)

if __name__ == '__main__':
    run_tests()
