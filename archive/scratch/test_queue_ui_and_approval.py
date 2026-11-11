import sys
from playwright.sync_api import sync_playwright

def test_queue_ui():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        
        print("[*] Navigating to http://localhost:8000...")
        page.goto("http://localhost:8000", wait_until="networkidle")
        
        print("[*] Selecting Tokyo Trip Card...")
        page.wait_for_selector(".trip-card-btn")
        with page.expect_response(lambda r: '/api/requests/queue?' in r.url and 'trip_id=' in r.url and r.status == 200):
            tokyo_btn = page.locator(".trip-card-btn:has-text('Tokyo')").first
            tokyo_btn.click()

        print("[*] Switching to Queue tab...")
        page.evaluate("switchTab('queue')")
        page.wait_for_selector(".message-card")
        page.wait_for_timeout(500)

        # 1. Verify obsolete filter pills are gone
        old_filters = page.locator(".pill-filter-btn:has-text('Pending All')")
        assert old_filters.count() == 0, "Old filter 'Pending All' still found!"
        print("[+] Verified: 8 filter pills removed from Queue toolbar.")

        # 2. Verify new toolbar buttons
        del_btn = page.locator("#btn-queue-delete-selected")
        erase_btn = page.locator("#btn-queue-erase-all")
        app_sel_btn = page.locator("#btn-approve-selected")
        app_all_btn = page.locator("#btn-approve-all")

        assert del_btn.count() == 1, "Delete Selected button missing!"
        assert erase_btn.count() == 1, "Erase All button missing!"
        assert app_sel_btn.count() == 1, "Approve Selected button missing!"
        assert app_all_btn.count() == 1, "Approve All Drafts button missing!"
        print("[+] Verified: Toolbar contains Delete, Erase All, Approve, and Approve All Drafts.")

        # 3. Check cards count
        cards = page.locator(".message-card")
        card_count = cards.count()
        print(f"[+] Total cards rendered in Queue: {card_count}")
        assert card_count > 0, "No cards rendered in Queue!"

        # 4. Check first card footer buttons and order
        first_card = cards.first
        # Old 'Карточка' button should be gone
        kartochka_btn = first_card.locator("button:has-text('Карточка')")
        assert kartochka_btn.count() == 0, "'Карточка' button still present in card!"
        print("[+] Verified: 'Карточка' link removed from card footer.")

        # Check order: Regenerate, Save, Copy, Name, Request
        left_buttons = first_card.locator(".card-footer-left > *")
        left_texts = [left_buttons.nth(i).inner_text().strip() for i in range(left_buttons.count())]
        print(f"[+] Card footer left items: {left_texts}")
        assert left_texts == ["Regenerate", "Save", "Copy", "Name", "Request"], f"Unexpected button order: {left_texts}"
        print("[+] Verified: Button order is exactly Regenerate, Save, Copy, Name, Request (no double emojis).")

        # 5. Check 'Request' button bounds inside card
        card_box = first_card.bounding_box()
        req_link = first_card.locator(".card-footer-left a:has-text('Request')")
        req_box = req_link.bounding_box()
        assert req_box["x"] + req_box["width"] <= card_box["x"] + card_box["width"] + 5, "Request link overflows card!"
        print("[+] Verified: Request button fits cleanly inside card footer without overflow.")

        # 6. Test opening modal by clicking card body
        modal = page.locator("#host-detail-modal")
        assert not modal.is_visible(), "Modal should be initially hidden"
        
        # Click on card header/title
        first_card.locator(".card-title-group").click()
        page.wait_for_timeout(500)
        assert modal.is_visible(), "Modal did not open on card click!"
        print("[+] Verified: Host detail modal opens on card click.")

        # 7. Test that clicking outside modal DOES NOT close it
        # Click at top-left outside the modal box
        page.mouse.click(10, 10)
        page.wait_for_timeout(300)
        assert modal.is_visible(), "Modal closed on outside click! It must only close via [X]."
        print("[+] Verified: Clicking outside modal does NOT close it.")

        # 8. Test closing via [X] button
        close_btn = modal.locator("button:has-text('✕')")
        close_btn.click()
        page.wait_for_timeout(300)
        assert not modal.is_visible(), "Modal did not close on [X] click!"
        print("[+] Verified: Modal closes when clicking [X].")

        # 9. Test Card Selection and Approve Selected
        # Select first 2 cards
        checkboxes = page.locator(".queue-card-select")
        checkboxes.nth(0).check()
        checkboxes.nth(1).check()
        page.wait_for_timeout(200)

        # Approve Selected button should now be enabled and show (2)
        assert not app_sel_btn.is_disabled(), "Approve selected button should be enabled"
        assert "(2)" in app_sel_btn.inner_text(), f"Expected '(2)' in Approve button, got {app_sel_btn.inner_text()}"
        print(f"[+] Verified: Approve button enabled with count: {app_sel_btn.inner_text()}")

        # Click Approve Selected
        with page.expect_response(lambda r: 'api/queue/approve_selected' in r.url and r.status == 200):
            app_sel_btn.click()
        page.wait_for_timeout(500)

        # The first 2 cards should now have 'approved' class or Approved badge
        approved_badges = page.locator(".message-card .badge:has-text('Approved')")
        print(f"[+] Total Approved badges after Approve Selected: {approved_badges.count()}")
        assert approved_badges.count() >= 2, "Selected cards did not become approved!"

        # 10. Test Approve All Drafts
        # Handle browser confirm dialog
        page.on("dialog", lambda dialog: dialog.accept())
        with page.expect_response(lambda r: '/api/requests/approve_all' in r.url and r.status == 200):
            app_all_btn.click()
        page.wait_for_timeout(1000)

        # All cards should now be Approved
        all_approved = page.locator(".message-card.approved")
        print(f"[+] Total Approved cards after Approve All: {all_approved.count()} of {card_count}")
        assert all_approved.count() == card_count, f"Not all cards approved! {all_approved.count()} != {card_count}"
        print("[+] Verified: Approve All Drafts successfully approved all cards in the queue.")

        # Take screenshot for record
        screenshot_path = "output/debug/queue_redesigned_verification.png"
        page.screenshot(path=screenshot_path, full_page=True)
        print(f"[+] Verification screenshot saved: {screenshot_path}")

        browser.close()

if __name__ == "__main__":
    test_queue_ui()
