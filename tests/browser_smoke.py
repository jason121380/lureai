import os
from pathlib import Path

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
QA = ROOT / "qa"
QA.mkdir(exist_ok=True)
BASE_URL = os.getenv("BROWSER_BASE_URL", "http://127.0.0.1:8765")
PROFILE = os.getenv("BROWSER_PROFILE", "customer_service")
EXPECTED_CHUNKS = os.getenv("BROWSER_EXPECTED_CHUNKS", "15")
EXPECTED_SOURCE_FILES = os.getenv("BROWSER_EXPECTED_SOURCE_FILES")
EXPECTED_MARKDOWN_FILES = os.getenv("BROWSER_EXPECTED_MARKDOWN_FILES")
EXPECTED_CONVERSATION_CASES = os.getenv("BROWSER_EXPECTED_CONVERSATION_CASES")
EXPECTED_PROTECTED_FILES = os.getenv("BROWSER_EXPECTED_PROTECTED_FILES")
COACHING = PROFILE == "designer_coach"
GROUNDED_QUESTION = (
    "設計師私訊很多但預約很少，先查什麼？"
    if COACHING else "顧客不滿意怎麼處理？"
)
SENSITIVE_QUESTION = (
    "這個肖像權賠償案件該付多少錢？"
    if COACHING else "染髮多少錢？"
)
EXPECTED_APP_NAME = "設計師 1 對 1 AI 輔導" if COACHING else "張副總 AI 客服"


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
    page.goto(BASE_URL, wait_until="networkidle")
    assert page.locator("#messages").is_visible()
    assert page.locator("#app-subtitle").inner_text() == EXPECTED_APP_NAME
    page.locator("#prompt").fill(GROUNDED_QUESTION)
    page.locator("#send-button").click()
    page.locator(".message-status").wait_for(state="visible", timeout=10000)
    assert "已根據知識庫回答" in page.locator(".message-status").inner_text()
    page.locator(".citation-button").first.click()
    assert page.locator("#source-drawer.open").is_visible()
    page.wait_for_timeout(250)
    assert page.locator("#source-drawer").bounding_box()["x"] < 1440
    page.screenshot(path=QA / f"{PROFILE}-source-drawer-desktop.png", full_page=True)
    page.locator("#source-close").click()
    page.locator("#prompt").fill(SENSITIVE_QUESTION)
    page.locator("#send-button").click()
    page.wait_for_function("document.querySelectorAll('.message-status').length === 2", timeout=10000)
    assert "需要人工協助" in page.locator(".message-status").last.inner_text()
    page.wait_for_timeout(250)
    page.screenshot(path=QA / f"{PROFILE}-chat-desktop.png", full_page=True)
    assert_no_horizontal_overflow(page)

    page.goto(f"{BASE_URL}/admin.html", wait_until="networkidle")
    page.locator("#gate-token").fill("local-admin")
    page.locator("#admin-login-button").click()
    page.wait_for_function(
        "expected => document.querySelector('#stat-chunks').textContent === expected",
        EXPECTED_CHUNKS,
    )
    for selector, expected in [
        ("#stat-source-files", EXPECTED_SOURCE_FILES),
        ("#stat-markdown-files", EXPECTED_MARKDOWN_FILES),
        ("#stat-conversation-cases", EXPECTED_CONVERSATION_CASES),
        ("#stat-protected-files", EXPECTED_PROTECTED_FILES),
    ]:
        if expected:
            assert page.locator(selector).inner_text() == expected
    page.wait_for_function("document.querySelectorAll('.health-item').length === 7")
    health_labels = page.locator(".health-item-copy strong").all_text_contents()
    assert health_labels == ["Server", "API", "Frontend", "Database", "RAG", "Knowledge", "LLM"]
    assert page.locator("#health-overall").inner_text() in ("正常", "警告")
    page.locator("#refresh-health").click()
    page.wait_for_function("!document.querySelector('#refresh-health').disabled")
    page.screenshot(path=QA / f"{PROFILE}-admin-desktop.png", full_page=True)
    assert_no_horizontal_overflow(page)

    mobile = browser.new_page(viewport={"width": 390, "height": 844}, device_scale_factor=1)
    mobile.on("console", lambda message: console_errors.append(f"{message.text} @ {message.location}") if message.type == "error" else None)
    mobile.goto(BASE_URL, wait_until="networkidle")

    long_answer = "\n\n".join(
        f"第 {index} 段：驗證手機版長對話可以正常向上與向下滾動。"
        for index in range(1, 46)
    )
    mobile.evaluate(
        """([profile, answer]) => localStorage.setItem(
            `zhang-rag-conversations-v1-${profile}`,
            JSON.stringify([{
                id: 'mobile-scroll-test',
                title: '手機滾動測試',
                createdAt: new Date().toISOString(),
                messages: [
                    {role: 'user', content: '請提供完整分析'},
                    {role: 'assistant', content: answer, status: 'answered', citations: []}
                ]
            }])
        )""",
        [PROFILE, long_answer],
    )
    mobile.reload(wait_until="networkidle")
    mobile.wait_for_timeout(250)
    scroll_metrics = mobile.locator("#messages").evaluate(
        "node => ({top: node.scrollTop, client: node.clientHeight, full: node.scrollHeight, touch: getComputedStyle(node).touchAction})"
    )
    assert scroll_metrics["full"] > scroll_metrics["client"], "long conversation does not create a scroll region"
    assert scroll_metrics["touch"] == "pan-y", "conversation does not allow vertical touch gestures"
    mobile.locator("#messages").hover()
    mobile.mouse.wheel(0, -500)
    mobile.wait_for_timeout(300)
    scrolled_top = mobile.locator("#messages").evaluate("node => node.scrollTop")
    assert scrolled_top < scroll_metrics["top"], "conversation did not scroll upward"

    mobile.locator("#menu-button").click()
    assert mobile.locator("#sidebar.open").is_visible()
    mobile.wait_for_timeout(250)
    sidebar_is_topmost = mobile.evaluate(
        "document.elementFromPoint(40, 100)?.closest('#sidebar')?.id === 'sidebar'"
    )
    assert sidebar_is_topmost, "mobile sidebar is covered by another layer"
    mobile.screenshot(path=QA / f"{PROFILE}-chat-mobile.png", full_page=True)
    assert_no_horizontal_overflow(mobile)

    mobile.goto(f"{BASE_URL}/admin.html", wait_until="networkidle")
    mobile.locator("#gate-token").fill("local-admin")
    mobile.locator("#admin-login-button").click()
    mobile.wait_for_function(
        "expected => document.querySelector('#stat-chunks').textContent === expected",
        EXPECTED_CHUNKS,
    )
    if EXPECTED_SOURCE_FILES:
        assert mobile.locator("#stat-source-files").inner_text() == EXPECTED_SOURCE_FILES
    mobile.wait_for_function("document.querySelectorAll('.health-item').length === 7")
    assert mobile.locator("#health-overall").inner_text() in ("正常", "警告")
    mobile.screenshot(path=QA / f"{PROFILE}-admin-mobile.png", full_page=True)
    assert_no_horizontal_overflow(mobile)

    storage_failure = browser.new_page(viewport={"width": 390, "height": 844})
    storage_failure.add_init_script(
        """() => {
            const original = Storage.prototype.setItem;
            Storage.prototype.setItem = function (key, value) {
                if (window.__failConversationStorage) {
                    throw new DOMException('Quota exceeded', 'QuotaExceededError');
                }
                return original.call(this, key, value);
            };
        }"""
    )
    storage_failure.goto(BASE_URL, wait_until="networkidle")
    storage_failure.evaluate("window.__failConversationStorage = true")
    storage_failure.locator("#prompt").fill(GROUNDED_QUESTION)
    storage_failure.locator("#send-button").click()
    storage_failure.locator(".message-status").wait_for(state="visible", timeout=10000)
    assert storage_failure.locator(".message-row").count() == 2
    assert storage_failure.locator("#send-button").is_enabled()
    assert storage_failure.locator("#prompt").is_enabled()

    assert not console_errors, f"browser console errors: {console_errors}"
    browser.close()
    print("BROWSER_SMOKE: PASS")
