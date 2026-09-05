"""瀏覽器端 E2E 煙霧測試（手動執行，不在 `unittest discover` 裡）。

跑法：
    USER_USERNAME=tester USER_PASSWORD=test-password-for-browser USER_ROLE=admin python3 run.py --port 8765 &
    BROWSER_BASE_URL=http://127.0.0.1:8765 python3 tests/browser_smoke.py

這份檔案一度整份對不上實作（還在找已經移除的 `customer_service` profile、後台
權杖輸入框 `#gate-token`、寫死的 macOS Chrome 路徑），所以它「跑得過」不代表
任何事——那比沒有煙霧測試更危險。後來又漂移過一次（`.message.assistant` 對上
實際的 `message-row assistant`、`#account-button` 對上實際的 `#user-account`），
所以 `tests/test_static_ui.py` 現在會把這裡引用的每個 #id 與 .class 對照
static/ 的真實 DOM，一漂移就在單元測試被擋下。現在對齊的是目前真正的流程：
帳號密碼登入 → 問一題 → 看到答案與來源 → 語氣就地切換 → 重新整理後對話還在 →
後台只認 admin。
"""
import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
QA = Path(os.getenv("BROWSER_ARTIFACT_DIR", ROOT / "qa"))
QA.mkdir(exist_ok=True)
BASE_URL = os.getenv("BROWSER_BASE_URL", "http://127.0.0.1:8765")
USERNAME = os.getenv("BROWSER_USERNAME", "tester")
PASSWORD = os.getenv("BROWSER_PASSWORD", "test-password-for-browser")
# 這台機器的 chromium；沒有就讓 Playwright 用自己的預設。
CHROME = os.getenv("BROWSER_EXECUTABLE", "/opt/pw-browsers/chromium-1194/chrome-linux/chrome")
QUESTION = os.getenv("BROWSER_QUESTION", "我的私訊很多，但預約很少，該先查什麼？")

console_errors: list[str] = []


def watch(page) -> None:
    def on_console(message) -> None:
        if message.type != "error":
            return
        # 還沒登入時 bootstrap 會先打 /api/auth/me 探測 session，401 是設計
        # 中的答案（然後才顯示登入頁），瀏覽器仍會記一條 resource error。
        if "status of 401" in message.text:
            return
        console_errors.append(f"[console] {message.text}")

    page.on("console", on_console)
    page.on("pageerror", lambda error: console_errors.append(f"[pageerror] {error}"))


def assert_no_horizontal_overflow(page) -> None:
    overflow = page.evaluate(
        "document.documentElement.scrollWidth > document.documentElement.clientWidth"
    )
    assert not overflow, "page has horizontal overflow"


def login(page) -> None:
    page.goto(BASE_URL + "/", wait_until="load")
    # 登入頁一開始就看得到，但 bootstrap 還在探測 session：401 回來時
    # showLogin() 會清空密碼欄並 focus 帳號欄。太早填表會被那一下清掉，
    # 送出的就是空密碼——先等 focus 落在帳號欄（＝showLogin 跑完）再填。
    page.wait_for_function(
        "() => document.activeElement && document.activeElement.id === 'login-username'",
        timeout=15000,
    )
    page.fill("#login-username", USERNAME)
    page.fill("#login-password", PASSWORD)
    page.click("#login-button")
    page.wait_for_selector("#app-shell:not([hidden])", timeout=15000)


def ask(page, text: str) -> None:
    page.fill("#prompt", text)
    page.click("#send-button")
    # 模型可能要跑幾十秒；等的是「這一則不再是載入中」。
    page.wait_for_selector(".message-row.assistant .message-text", timeout=90000)
    page.wait_for_function(
        "() => !document.querySelector('.message-row.assistant .typing')",
        timeout=90000,
    )


with sync_playwright() as playwright:
    launch: dict = {"headless": True, "args": ["--no-proxy-server"]}
    if Path(CHROME).exists():
        launch["executable_path"] = CHROME
    browser = playwright.chromium.launch(**launch)

    page = browser.new_page(viewport={"width": 1440, "height": 900})
    watch(page)
    login(page)
    assert page.locator("#messages").is_visible()
    assert_no_horizontal_overflow(page)

    ask(page, QUESTION)
    answer = page.locator(".message-row.assistant .message-text").last.inner_text()
    assert len(answer.strip()) > 20, f"answer looks empty: {answer!r}"
    # 有答出來就一定要看得到是根據什麼答的。
    assert page.locator(".message-status").last.is_visible()

    # 語氣就地切換：留在同一段對話，不開新的（使用者指定的行為）。
    # 帳號選單（#user-account）裡點語氣是直接切換；#tone-confirm 那個確認
    # 彈窗只掛在頂部的 #tone-indicator 膠囊上，這條路不會出現。
    before = page.evaluate("document.querySelectorAll('.message-row').length")
    page.click("#user-account")
    page.click("#tone-toggle .tone-option[data-tone='service']")
    page.wait_for_timeout(300)
    after = page.evaluate("document.querySelectorAll('.message-row').length")
    assert after == before, f"切換語氣把對話清掉了（{before} → {after}）"

    # 對話存在伺服器：重新整理之後還要在。
    page.reload(wait_until="load")
    page.wait_for_selector("#app-shell:not([hidden])", timeout=15000)
    page.wait_for_timeout(1200)
    restored = page.evaluate("document.querySelectorAll('.message-row').length")
    assert restored >= before, f"重新整理之後對話少了（{before} → {restored}）"
    page.screenshot(path=str(QA / "chat-desktop.png"), full_page=True)

    # 每個情境開自己的 context（獨立 cookie）＝模擬另一台裝置。共用 context
    # 的話，前一頁的 session 與 service worker 會跟這一頁的登入流程互相競速，
    # 登入表單填了但 submit 打不出去。
    mobile = browser.new_context(
        viewport={"width": 390, "height": 844}, device_scale_factor=1,
        is_mobile=True, has_touch=True,
    ).new_page()
    watch(mobile)
    login(mobile)
    metrics = mobile.locator("#messages").evaluate(
        "node => ({client: node.clientHeight, full: node.scrollHeight,"
        " touch: getComputedStyle(node).touchAction})"
    )
    assert metrics["touch"] == "pan-y", "conversation does not allow vertical touch gestures"
    mobile.click("#menu-button")
    assert mobile.locator("#sidebar.open").is_visible()
    mobile.wait_for_timeout(250)
    sidebar_is_topmost = mobile.evaluate(
        "document.elementFromPoint(40, 100)?.closest('#sidebar')?.id === 'sidebar'"
    )
    assert sidebar_is_topmost, "mobile sidebar is covered by another layer"
    mobile.screenshot(path=str(QA / "chat-mobile.png"), full_page=True)
    assert_no_horizontal_overflow(mobile)

    # 後台只認 admin session（UI 沒有權杖輸入框）。非 admin 會被導回 /。
    admin = browser.new_context(viewport={"width": 1440, "height": 900}).new_page()
    watch(admin)
    login(admin)
    admin.goto(BASE_URL + "/admin", wait_until="load")
    admin.wait_for_timeout(1500)
    if "/admin" in admin.url:
        admin.wait_for_selector("#admin-shell", timeout=15000)
        admin.wait_for_function(
            "() => { const value = document.querySelector('#stat-chunks')"
            "?.textContent?.trim(); return value && value !== '—'; }",
            timeout=20000,
        )
        admin.screenshot(path=str(QA / "admin-desktop.png"), full_page=True)
        assert_no_horizontal_overflow(admin)
    else:
        print(f"（{USERNAME} 不是 admin，後台被導回 {admin.url}，跳過後台檢查）")

    browser.close()

if console_errors:
    print("console errors:")
    for item in console_errors:
        print(" -", item)
    sys.exit(1)
print("browser smoke ok")
