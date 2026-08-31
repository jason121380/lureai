from pathlib import Path

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
QA = ROOT / "qa"
QA.mkdir(exist_ok=True)


def assert_no_horizontal_overflow(page):
    overflow = page.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth")
    assert not overflow, "page has horizontal overflow"


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(
        headless=True,
        executable_path="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    )
    console_errors = []

    page = browser.new_page(viewport={"width": 1440, "height": 900})
    page.on("console", lambda message: console_errors.append(f"{message.text} @ {message.location}") if message.type == "error" else None)
    page.goto("http://127.0.0.1:8765", wait_until="networkidle")
    assert page.locator("#messages").is_visible()
    page.locator("#prompt").fill("顧客不滿意怎麼處理？")
    page.locator("#send-button").click()
    page.locator(".message-status").wait_for(state="visible", timeout=10000)
    assert "已根據知識庫回答" in page.locator(".message-status").inner_text()
    page.locator(".citation-button").first.click()
    assert page.locator("#source-drawer.open").is_visible()
    page.wait_for_timeout(250)
    assert page.locator("#source-drawer").bounding_box()["x"] < 1440
    page.screenshot(path=QA / "source-drawer-desktop.png", full_page=True)
    page.locator("#source-close").click()
    page.locator("#prompt").fill("染髮多少錢？")
    page.locator("#send-button").click()
    page.wait_for_function("document.querySelectorAll('.message-status').length === 2", timeout=10000)
    assert "需要人工協助" in page.locator(".message-status").last.inner_text()
    page.wait_for_timeout(250)
    page.screenshot(path=QA / "chat-desktop.png", full_page=True)
    assert_no_horizontal_overflow(page)

    page.goto("http://127.0.0.1:8765/admin.html", wait_until="networkidle")
    page.locator("#admin-token").fill("local-admin")
    page.locator("#save-token").click()
    page.wait_for_function("document.querySelector('#stat-chunks').textContent === '15'")
    page.screenshot(path=QA / "admin-desktop.png", full_page=True)
    assert_no_horizontal_overflow(page)

    mobile = browser.new_page(viewport={"width": 390, "height": 844}, device_scale_factor=1)
    mobile.on("console", lambda message: console_errors.append(f"{message.text} @ {message.location}") if message.type == "error" else None)
    mobile.goto("http://127.0.0.1:8765", wait_until="networkidle")
    mobile.locator("#menu-button").click()
    assert mobile.locator("#sidebar.open").is_visible()
    mobile.screenshot(path=QA / "chat-mobile.png", full_page=True)
    assert_no_horizontal_overflow(mobile)

    mobile.goto("http://127.0.0.1:8765/admin.html", wait_until="networkidle")
    mobile.locator("#admin-token").fill("local-admin")
    mobile.locator("#save-token").click()
    mobile.wait_for_function("document.querySelector('#stat-chunks').textContent === '15'")
    mobile.screenshot(path=QA / "admin-mobile.png", full_page=True)
    assert_no_horizontal_overflow(mobile)

    assert not console_errors, f"browser console errors: {console_errors}"
    browser.close()
    print("BROWSER_SMOKE: PASS")
