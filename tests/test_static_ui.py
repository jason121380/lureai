import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "static" / "index.html"
ADMIN = ROOT / "static" / "admin.html"
CSS = ROOT / "static" / "app.css"
CHAT_JS = ROOT / "static" / "chat.js"
ADMIN_JS = ROOT / "static" / "admin.js"
APP_JS = ROOT / "static" / "app.js"
LOGO = ROOT / "static" / "logo.png"
FAVICON = ROOT / "static" / "favicon.png"
APP_ICON = ROOT / "static" / "app-icon.png"


class StaticUiTests(unittest.TestCase):
    def test_chat_page_has_required_workbench_regions(self):
        html = INDEX.read_text(encoding="utf-8")

        for element_id in ("conversation-list", "messages", "composer", "source-drawer"):
            self.assertIn(f'id="{element_id}"', html)

    def test_chat_page_only_exposes_conversation_navigation(self):
        html = INDEX.read_text(encoding="utf-8")

        self.assertIn('class="primary-nav"', html)
        self.assertIn('id="conversation-search-input"', html)
        self.assertNotIn('href="admin.html', html)
        self.assertNotIn('id="composer-menu-button"', html)
        self.assertNotIn("mode-switch", html)
        # The settings link to /admin is hidden until chat.js confirms an
        # admin-role session.
        self.assertIn(
            'id="admin-link" class="icon-button" href="/admin" target="_blank" rel="noopener"',
            html,
        )
        self.assertIn('aria-label="設定（另開分頁）" hidden', html)

    def test_chat_page_requires_login_and_shows_private_usage(self):
        html = INDEX.read_text(encoding="utf-8")
        script = CHAT_JS.read_text(encoding="utf-8")

        for element_id in (
            "login-gate", "login-form", "login-username", "login-password",
            "usage-progress", "usage-spend", "usage-tokens", "logout-button",
        ):
            self.assertIn(f'id="{element_id}"', html)
        self.assertIn("/api/auth/login", script)
        self.assertIn("/api/auth/me", script)
        self.assertIn("/api/auth/logout", script)
        self.assertIn("/api/usage", script)

    def test_brand_assets_are_installed(self):
        html = INDEX.read_text(encoding="utf-8")

        self.assertIn('src="logo.png"', html)
        # icon 網址帶版本參數：換圖時避免瀏覽器沿用快取的舊圖。
        self.assertIn('href="favicon.png?v=2"', html)
        self.assertIn('href="app-icon.png?v=2"', html)
        for path in (LOGO, FAVICON, APP_ICON):
            self.assertTrue(path.is_file())
            self.assertGreater(path.stat().st_size, 100)

    def test_chat_page_loads_lucide_and_chat_controller(self):
        html = INDEX.read_text(encoding="utf-8")

        self.assertIn("vendor/lucide.min.js", html)
        self.assertIn("chat.js", html)

    def test_admin_page_has_operational_views(self):
        html = ADMIN.read_text(encoding="utf-8")

        for element_id in ("admin-shell", "stats-grid", "knowledge-results", "quality-list", "knowledge-editor"):
            self.assertIn(f'id="{element_id}"', html)
        self.assertIn('id="admin-shell" class="admin-shell" hidden', html)
        for element_id in ("users", "user-form", "user-username", "user-password", "user-results"):
            self.assertIn(f'id="{element_id}"', html)

    def test_chat_renders_answers_as_lists_not_one_block_of_text(self):
        script = CHAT_JS.read_text(encoding="utf-8")
        css = CSS.read_text(encoding="utf-8")

        for marker in ("BULLET_LINE", "ORDERED_LINE", "<li>", "flushList"):
            self.assertIn(marker, script)
        # Bullets need real list layout, so the assistant bubble drops pre-wrap.
        self.assertIn('text.classList.add("rich")', script)
        self.assertIn(".message-text.rich { white-space: normal; }", css)
        self.assertIn(".message-text.rich ul", css)

    def test_app_shell_row_cannot_be_stretched_by_the_sidebar(self):
        """側欄對話一多時不可以撐破版面：隱含的 auto row 會讓整頁捲動、topbar 被捲掉。"""
        css = CSS.read_text(encoding="utf-8")

        shell = css.split(".app-shell {", 1)[1].split("}", 1)[0]
        self.assertIn("grid-template-rows: minmax(0, 1fr)", shell)
        self.assertIn("height: 100%", shell)
        # 側欄要能被壓縮，內部清單才會接手捲動。
        sidebar = css.split(".sidebar {", 1)[1].split("}", 1)[0]
        self.assertIn("min-height: 0", sidebar)
        # html 也要關掉整頁捲動（body 有、html 沒有時仍會整頁捲）。
        self.assertIn("html, body { height: 100%; margin: 0; overflow: hidden; }", css)

    def test_mobile_sidebar_pushes_the_page_aside(self):
        """選單不是蓋上去，是把主畫面往右推（比照 ChatGPT App）。"""
        css = CSS.read_text(encoding="utf-8")
        script = CHAT_JS.read_text(encoding="utf-8")

        block = css.split("手機版選單是「把主畫面往右推」", 1)[1]
        panel = block.split(".sidebar, .sidebar.desktop-hidden {", 1)[1].split("}", 1)[0]
        pushed = block.split(".sidebar.open + .chat-main {", 1)[1].split("}", 1)[0]

        # 選單一直在底下不動：不做位移、疊在主畫面下面。
        self.assertIn("width: 75%;", panel)
        self.assertIn("transform: none;", panel)
        self.assertIn("z-index: 10;", panel)
        # 動的是主畫面：往右推、縮一點、圓角與陰影。
        self.assertIn("translateX(75%)", pushed)
        self.assertIn("scale(.92)", pushed)
        self.assertIn("border-radius", pushed)
        self.assertIn("box-shadow", pushed)
        # 縮放的原點要在左邊，否則左緣會再往右移、遮罩對不齊。
        self.assertIn("transform-origin: left center;", block.split(".chat-main {", 1)[1].split("}", 1)[0])
        # 主畫面要有自己的底色，不然推開後會透出下面的選單。
        self.assertIn("background: var(--canvas);", block.split(".chat-main {", 1)[1].split("}", 1)[0])
        # 透明遮罩只蓋被推開的那一截，選單本身還要點得到。
        self.assertIn(".drawer-overlay.clear { left: 75%; z-index: 30; }", css)
        opener = script.split("function openSidebar()", 1)[1].split("\n  }", 1)[0]
        self.assertIn('classList.add("clear")', opener)

    def test_a_blank_line_does_not_restart_the_numbering(self):
        """模型常在每個編號之間空一行；空行不能把清單收掉。

        收掉的話每一項都各自變成一個 <ol>，畫面上就全部是「1.」。
        """
        script = CHAT_JS.read_text(encoding="utf-8")

        renderer = script.split("function renderAssistantMarkup", 1)[1].split("\n  }", 1)[0]
        blank_branch = renderer.split("if (!line.trim()) {", 1)[1].split("\n      }", 1)[0]
        # 註解裡會提到 flushList，比對前先把註解拿掉。
        code = "\n".join(
            line for line in blank_branch.splitlines() if not line.strip().startswith("//")
        )
        # 空行只收段落，清單留著；段落與標題那兩條路自己會先 flushList()。
        self.assertIn("flushParagraph();", code)
        self.assertNotIn("flushList", code)
        self.assertIn("flushList();\n      paragraph.push(", renderer)

    def test_knowledge_list_is_a_collapsed_accordion(self):
        """278 則攤開來看不完，做成 QA 式收合清單才掃得動。"""
        css = CSS.read_text(encoding="utf-8")
        script = ADMIN_JS.read_text(encoding="utf-8")

        # 一列一則、彼此靠在一起，列與列之間只有一條分隔線。
        self.assertIn(".knowledge-row { border-top:", css)
        self.assertIn(".knowledge-row:first-of-type { border-top: 0; }", css)
        # 不要外框：#knowledge-results 本身就是 .knowledge-list，再描一個框
        # 就會變成框裡有框。
        container = css.split(".knowledge-list {", 1)[1].split("}", 1)[0]
        self.assertNotIn("border:", container)
        self.assertNotIn("border-radius", container)
        # 預設收合，點標題才展開。
        self.assertIn('class="knowledge-detail" hidden', script)
        self.assertIn('aria-expanded="false"', script)
        self.assertIn("async function toggleKnowledgeRow(index)", script)
        # 清單只帶前 400 字，展開時才抓完整內容。
        self.assertIn('(item.length || 0) > 400', script)
        self.assertIn("/api/admin/knowledge/detail?chunk_id=", script)

    def test_mobile_admin_puts_the_tabs_at_the_bottom(self):
        """手機版後台是 App 的形狀：分頁列在底部、左上角返回前台。

        原本兩者都擠在頂端，光導覽就吃掉快 400px 的高度。
        """
        css = CSS.read_text(encoding="utf-8")
        html = ADMIN.read_text(encoding="utf-8")

        mobile = css.split("手機版後台＝App 的形狀", 1)[1]
        nav = mobile.split(".admin-nav-links {", 1)[1].split("}", 1)[0]
        self.assertIn("position: fixed", nav)
        self.assertIn("bottom: 0", nav)
        self.assertIn("env(safe-area-inset-bottom)", nav)
        # 直向排列時只設 align-items: center 會把返回鍵推到正中間。
        bar = mobile.split(".admin-nav { ", 1)[1].split("}", 1)[0]
        self.assertIn("flex-direction: row", bar)
        # 內容不能被底部分頁列蓋住。
        main = mobile.split(".admin-main {", 1)[1].split("}", 1)[0]
        self.assertIn("64px", main)
        # 「AI 模型校調」六個字在六格分頁列裡放不下，用 data-short。
        self.assertIn('data-short="校調"', html)
        self.assertIn("content: attr(data-short)", mobile)
        # 返回前台用左箭頭。
        self.assertIn('data-lucide="arrow-left"', html)

    def test_badges_never_outsize_the_title(self):
        """標籤沒設字級會繼承瀏覽器預設的 16px，比 15px 的標題還大。"""
        css = CSS.read_text(encoding="utf-8")

        for selector in (".domain-badge", ".origin-badge"):
            # 只看行首那一條——「.knowledge-summary > .origin-badge」是另一條規則。
            rule = next(
                line for line in css.splitlines()
                if line.startswith(selector + " {")
            )
            self.assertIn("font-size: 13px", rule, selector)

    def test_mobile_admin_keeps_the_desktop_forms(self):
        """手機版不要把按鈕的字藏起來，也不要把表單拆成怪形狀。"""
        css = CSS.read_text(encoding="utf-8")

        mobile = css.split("手機版後台＝App 的形狀", 1)[1].split("@media", 1)[0]
        # 按鈕要看得到字（只有底部分頁列例外，它用 data-short）。
        self.assertNotIn(".admin-search-form .command-button span { display: none; }", css)
        # 帳號表單已經搬進彈窗，欄位一律滿版。
        field = css.split(".modal-field input, .modal-field select {", 1)[1].split("}", 1)[0]
        self.assertIn("width: 100%", field)
        # 帳號表格在手機上不要斷字（原本「jason」被拆成兩行）。
        self.assertIn(".data-table td:first-child, .data-table td:nth-child(3) { white-space: nowrap; }", mobile)
        # 校調卡的標題列維持橫排，不然還原鍵會變成滿版的空盒子。
        self.assertIn(".tuning-rule-head { flex-direction: row;", mobile)

    def test_mobile_knowledge_rows_wrap_into_two_lines(self):
        """390px 放不下一列六個欄位，改成標題＋字數一行、定位點一行。"""
        css = CSS.read_text(encoding="utf-8")

        # **這個 media query 一定要在桌機規則後面**：media query 不加權重，
        # 同分時後者勝，寫在前面會被蓋掉（實測標題被擠成一個字）。
        desktop_at = css.index(".knowledge-summary-title { flex:")
        mobile_at = css.index("知識庫的手機版")
        self.assertGreater(mobile_at, desktop_at)

        mobile = css.split("知識庫的手機版", 1)[1].split("\n}", 1)[0]
        self.assertIn("flex-wrap: wrap", mobile)
        # 定位點要真的換行，而且排在字數後面。
        locator = mobile.split(".knowledge-summary > .source-locator {", 1)[1].split("}", 1)[0]
        self.assertIn("flex: 0 0 100%", locator)
        self.assertIn("order: 3", locator)
        # 縮排用 padding：margin 會加在 100% 之外，右緣會溢出。
        self.assertIn("padding-left", locator)
        self.assertNotIn("margin-left", locator)

    def test_upload_opens_as_a_modal(self):
        """「上傳檔案」按下去是彈窗，不是頁面裡的一塊。"""
        html = ADMIN.read_text(encoding="utf-8")
        css = CSS.read_text(encoding="utf-8")
        script = ADMIN_JS.read_text(encoding="utf-8")

        # 彈窗殼：壓暗背景 + 置中卡片，比照帳號彈窗。
        self.assertIn('id="upload-modal"', html)
        self.assertIn('role="dialog"', html)
        self.assertIn('aria-modal="true"', html)
        self.assertIn('id="upload-backdrop"', html)
        modal = css.split(".upload-modal {", 1)[1].split("}", 1)[0]
        self.assertIn("position: fixed", modal)
        self.assertIn("place-items: center", modal)
        # 內容再多也不能把彈窗撐出畫面：卡片夾高度，捲動交給內層。
        panel = css.split(".upload-panel {", 1)[1].split("}", 1)[0]
        self.assertIn("max-height", panel)
        self.assertIn("overflow: hidden", panel)
        self.assertIn("overflow-y: auto", css.split(".upload-scroll {", 1)[1].split("}", 1)[0])
        # 三種關法都要有：關閉鈕、點背景、Esc。
        self.assertIn('el("upload-close").addEventListener("click", closeUpload)', script)
        # 手動輸入走同一套彈窗殼。
        self.assertIn('id="editor-modal"', html)
        self.assertIn('el("editor-backdrop").addEventListener("click", closeEditor)', script)
        self.assertIn('el("upload-backdrop").addEventListener("click", closeUpload)', script)
        self.assertIn('event.key === "Escape"', script)

    def test_touch_devices_get_no_hover_highlight(self):
        """觸控裝置點過之後 :hover 會黏住，側欄會出現兩個灰底。

        所以 hover 的視覺一律關在 `hover: hover` 裡；`.active` 與 focus 狀態
        不能跟著進去，它們代表真的狀態，在觸控裝置上也要看得到。
        """
        css = CSS.read_text(encoding="utf-8")

        block = css.split("@media (hover: hover) {", 1)[1]
        self.assertIn(".conversation-item:hover", block)
        self.assertIn(".icon-button:hover", block)
        # 區塊外面不可以再有 :hover 的樣式（註解不算）。
        outside = "\n".join(
            line for line in css.split("@media (hover: hover) {", 1)[0].splitlines()
            if not line.strip().startswith("/*") and not line.strip().startswith("*")
        )
        self.assertNotIn(":hover", outside)
        # 選中與 focus 要留在外面。
        self.assertIn(".conversation-item.active { background: var(--hover); }", outside)
        self.assertIn(":focus-within .conversation-delete", outside)
        # 觸控時刪除鈕本來就要看得見（沒有 hover 可以觸發）。
        self.assertIn("@media (hover: none) {", css)

    def test_mobile_swipe_opens_and_closes_the_sidebar(self):
        """PWA 沒有瀏覽器的返回手勢，兩個方向都要自己做。"""
        script = CHAT_JS.read_text(encoding="utf-8")

        self.assertIn('addEventListener("touchstart"', script)
        # 兩個方向都不限制起手位置：畫面任何地方往右滑展開、往左滑收起。
        self.assertNotIn("SWIPE_EDGE", script)
        # 但起手在輸入框或會橫向捲動的東西上時不接手。
        self.assertIn("function swipeBlocked(target)", script)
        self.assertIn('tag === "INPUT" || tag === "TEXTAREA"', script)
        self.assertIn("node.scrollWidth > node.clientWidth + 1", script)
        self.assertIn("if (swipeBlocked(event.target)) return;", script)
        self.assertIn("!swipe.open && deltaX > SWIPE_DISTANCE", script)
        self.assertIn("swipe.open && deltaX < -SWIPE_DISTANCE", script)
        self.assertIn("openSidebar();", script)
        self.assertIn("closeSidebar();", script)
        # 直向偏移過大就是在捲動，要取消手勢。
        self.assertIn("> SWIPE_DRIFT", script)

    def test_bootstrap_never_spins_for_ever(self):
        script = CHAT_JS.read_text(encoding="utf-8")

        # 伺服器沒回應時要看到登入頁與錯誤訊息，不是一直轉圈。
        self.assertIn("function fetchWithTimeout", script)
        self.assertIn('fetchWithTimeout("/api/health"', script)
        self.assertIn('fetchWithTimeout("/api/auth/me"', script)
        self.assertIn("載入逾時，請重新整理再試一次", script)

    def test_degraded_model_answers_say_so(self):
        script = CHAT_JS.read_text(encoding="utf-8")
        css = CSS.read_text(encoding="utf-8")

        # 使用者看到的是白話說明，內部狀態碼只放進 title。
        self.assertIn("這則沒有成功生成", script)
        self.assertNotIn("以下為知識原文", script)
        self.assertIn("model_status: ${item.modelStatus}", script)
        self.assertIn("item.modelStatus", script)
        self.assertIn(".message-status.degraded", css)

    def test_tone_setting_is_wired_through_the_chat_ui(self):
        html = INDEX.read_text(encoding="utf-8")
        script = CHAT_JS.read_text(encoding="utf-8")
        css = CSS.read_text(encoding="utf-8")

        # 語氣切換：專家（條列講深）／客服（真人聊天一句一句回）。
        self.assertIn('id="tone-toggle"', html)
        self.assertIn('data-tone="expert"', html)
        self.assertIn('data-tone="service"', html)
        self.assertIn("function setTone", script)
        self.assertIn("const tone = state.tone;", script)
        self.assertIn("history, tone }", script)
        self.assertIn("renderServiceBubbles", script)
        # 客服模式句尾不顯示引用編號（引用只給系統核對，來源列在泡泡下方）。
        self.assertIn(r'replace(/\s*\[\d{1,2}\]/g, "")', script)
        self.assertIn(".tone-toggle", css)
        self.assertIn(".message-text.bubbles", css)

    def test_service_mode_talks_like_a_real_person(self):
        script = CHAT_JS.read_text(encoding="utf-8")

        # 依語意斷句（不做字數硬拆）、標點改空白、逐句 1.5~2.5 秒發送。
        self.assertNotIn("SERVICE_BUBBLE_MAX", script)
        self.assertIn("不做字數硬拆", script)
        self.assertIn("function serviceSentences", script)
        # 超過 3 則時把中間併起來，不可直接丟掉（丟掉會吃掉範例正文）。
        self.assertIn("SERVICE_MAX_BUBBLES = 3", script)
        # 超過上限時把中間併起來，不可直接丟掉（丟掉會吃掉範例正文）。
        self.assertIn("reflowed.slice(SERVICE_MAX_BUBBLES - 2, -1).join", script)
        # 一則最多 2 行：模型忘了空行時前端自己重排。
        self.assertIn("SERVICE_MAX_LINES = 2", script)
        # 空一行才換一則：一則裡面可以有好幾行。
        self.assertIn("split(/\\n[ \\t]*\\n+/)", script)

        self.assertIn("function revealServiceMessage", script)
        self.assertIn("pendingReveal", script)
        # 一則跟下一則之間至少 3 秒（使用者指定），太快就只是一次倒完。
        self.assertIn("3000 + Math.random() * 1500", script)

    def test_account_popup_hosts_tone_and_usage_tabs(self):
        html = INDEX.read_text(encoding="utf-8")
        script = CHAT_JS.read_text(encoding="utf-8")
        css = CSS.read_text(encoding="utf-8")

        # 左下角按名字打開置中卡片彈窗：二分欄（左：設定／用量導覽，右：內容）。
        self.assertIn('id="account-menu"', html)
        self.assertIn('id="user-account"', html)
        self.assertIn('data-tab="settings"', html)
        self.assertIn('data-tab="usage"', html)
        menu = html.split('id="account-menu"', 1)[1].split('id="user-account"', 1)[0]
        self.assertIn('id="tone-toggle"', menu)
        self.assertIn('id="usage-progress"', menu)
        self.assertIn('class="account-side"', menu)
        self.assertIn('id="account-backdrop"', menu)
        self.assertIn("function toggleAccountMenu", script)
        self.assertIn(".account-modal", css)
        card_rule = css.split(".account-card {", 1)[1].split("}", 1)[0]
        self.assertIn("grid-template-columns", card_rule)

    def test_topbar_shows_the_active_reply_mode(self):
        html = INDEX.read_text(encoding="utf-8")
        script = CHAT_JS.read_text(encoding="utf-8")
        css = CSS.read_text(encoding="utf-8")

        # 右上角常駐顯示目前的回覆模式，點擊跳出切換確認彈窗。
        self.assertIn('id="tone-indicator"', html)
        self.assertIn('id="tone-confirm"', html)
        self.assertIn("是否切換為", script)
        self.assertIn("客服模式", script)
        self.assertIn("專家模式", script)
        self.assertIn(".tone-indicator", css)
        self.assertIn(".tone-confirm", css)

    def test_composer_meta_row_is_removed(self):
        html = INDEX.read_text(encoding="utf-8")

        # 使用者要求移除輸入框下方的知識範圍說明與字數統計。
        self.assertNotIn("composer-meta", html)
        self.assertNotIn('id="char-count"', html)
        self.assertNotIn('id="knowledge-scope"', html)

    def test_answers_offer_thumbs_feedback(self):
        script = CHAT_JS.read_text(encoding="utf-8")
        css = CSS.read_text(encoding="utf-8")

        # 每則回答可評分（👍👎），回饋送到 /api/feedback 供之後加強。
        self.assertIn('"thumbs-up"', script)
        self.assertIn('"thumbs-down"', script)
        self.assertIn('"/api/feedback"', script)
        self.assertIn(".feedback-button", css)

    def test_new_chat_button_has_no_permanent_highlight(self):
        html = INDEX.read_text(encoding="utf-8")

        self.assertIn('class="primary-nav-item" id="new-chat"', html)

    def test_conversation_delete_uses_in_page_confirmation(self):
        script = CHAT_JS.read_text(encoding="utf-8")
        css = CSS.read_text(encoding="utf-8")

        self.assertNotIn("window.confirm", script)
        self.assertIn('classList.contains("confirming")', script)
        self.assertIn(".conversation-delete.confirming", css)

    def test_citations_are_labelled_as_knowledge_sources(self):
        script = CHAT_JS.read_text(encoding="utf-8")
        css = CSS.read_text(encoding="utf-8")

        self.assertIn("知識來源：", script)
        self.assertIn(".citation-label", css)

    def test_composer_has_no_shadow_and_stays_compact_when_empty(self):
        css = CSS.read_text(encoding="utf-8")

        composer_rule = css.split(".composer {", 1)[1].split("}", 1)[0]
        self.assertNotIn("box-shadow", composer_rule)
        # focus（按下輸入框）也不加陰影。
        focus_rule = css.split(".composer:focus-within {", 1)[1].split("}", 1)[0]
        self.assertNotIn("box-shadow", focus_rule)
        self.assertIn(".is-empty .composer { width: min(640px, 100%); }", css)

    def test_conversation_list_shows_date_and_time(self):
        script = CHAT_JS.read_text(encoding="utf-8")
        css = CSS.read_text(encoding="utf-8")

        # 對話清單每筆顯示最後活動的日期時間，有新訊息就排到最上面。
        self.assertIn("function formatConversationTime", script)
        self.assertIn("conversation.updatedAt = new Date().toISOString()", script)
        self.assertIn(".conversation-time", css)

    def test_new_chat_reuses_the_existing_empty_conversation(self):
        script = CHAT_JS.read_text(encoding="utf-8")

        opener = script.split("function newConversation()", 1)[1].split("\n  }", 1)[0]
        self.assertIn("state.conversations.find", opener)

    def test_installed_app_keeps_a_white_status_bar(self):
        for page in (INDEX, ADMIN):
            html = page.read_text(encoding="utf-8")
            # 深色模式沒有對應的 theme-color 時，iOS 會用灰底畫狀態列。
            self.assertIn('content="#ffffff" media="(prefers-color-scheme: dark)"', html)
            self.assertIn('content="#ffffff" media="(prefers-color-scheme: light)"', html)
            self.assertIn('name="color-scheme" content="light"', html)
        self.assertIn("color-scheme: light;", CSS.read_text(encoding="utf-8"))

    def test_admin_knowledge_list_shows_total_count(self):
        script = ADMIN_JS.read_text(encoding="utf-8")

        # 知識庫清單顯示「共 N 則」，搭配後端列出全部（不再吃 200 筆上限）。
        self.assertIn("knowledge-count", script)
        # 內文照原始結構條列呈現，不壓成一整段。
        self.assertIn("function knowledgeExcerpt", script)

    def test_admin_replaces_native_selects_with_in_page_dropdowns(self):
        script = ADMIN_JS.read_text(encoding="utf-8")
        css = CSS.read_text(encoding="utf-8")

        self.assertIn("function enhanceSelect", script)
        self.assertIn("enhanceSelects()", script)
        self.assertIn('role", "listbox"', script)
        self.assertIn(".select-menu", css)
        # A hung request must not leave the panel on 載入中 for ever.
        self.assertIn("AbortController", script)
        self.assertIn("data-retry-knowledge", script)

    def test_answer_policy_requires_short_action_focused_bullets(self):
        policy = (ROOT / "config" / "designer_coach_policy.md").read_text(encoding="utf-8")

        self.assertIn("## 輸出格式", policy)
        for rule in ("第一行一句話講結論", "以動詞開頭", "不要複述他的問題"):
            self.assertIn(rule, policy)

    def test_admin_page_splits_knowledge_into_the_two_domains(self):
        html = ADMIN.read_text(encoding="utf-8")
        script = ADMIN_JS.read_text(encoding="utf-8")

        for element_id in ("domain-grid", "knowledge-domain", "editor-domain"):
            self.assertIn(f'id="{element_id}"', html)
        for label in ("店務營運管理", "設計師一對一行銷輔導"):
            self.assertIn(label, html)
            self.assertIn(label, script)
        self.assertIn("domain=${encodeURIComponent(domain)}", script)
        # 切換主題／來源走前端過濾（整包快取），不再每次重打 API。
        self.assertIn("function fetchAllKnowledge", script)

    def test_admin_token_is_kept_in_memory_only(self):
        script = ADMIN_JS.read_text(encoding="utf-8")

        self.assertNotIn("localStorage", script)
        self.assertNotIn("sessionStorage", script)

    def test_admin_page_has_system_health_controls(self):
        html = ADMIN.read_text(encoding="utf-8")
        script = ADMIN_JS.read_text(encoding="utf-8")

        for element_id in ("health", "health-overall", "health-summary", "health-grid", "health-checked-at", "refresh-health"):
            self.assertIn(f'id="{element_id}"', html)
        self.assertIn("/api/admin/health", script)
        self.assertIn("renderHealth", script)

    def test_css_has_mobile_breakpoint_and_touch_scroll_region(self):
        css = CSS.read_text(encoding="utf-8")

        self.assertIn("@media (max-width: 760px)", css)
        self.assertIn(".composer-shell", css)
        self.assertIn("touch-action: pan-y", css)
        self.assertIn("-webkit-overflow-scrolling: touch", css)
        self.assertIn("overscroll-behavior-y: contain", css)
        self.assertIn(".composer-shell { position: relative", css)

    def test_hidden_controls_cannot_be_overridden_by_component_display(self):
        css = CSS.read_text(encoding="utf-8")

        self.assertIn("[hidden] { display: none !important; }", css)

    def test_only_latest_message_uses_entry_animation(self):
        css = CSS.read_text(encoding="utf-8")

        self.assertIn(".message-row:last-child", css)

    def test_every_answer_can_be_copied_as_plain_text(self):
        js = CHAT_JS.read_text(encoding="utf-8")

        # 複製鈕與評分同一列，但不綁 traceId——離線快取的回答沒有評分 id，
        # 照樣要能複製。
        self.assertIn("copy-button", js)
        self.assertIn("function plainAnswer", js)
        # 貼到 LINE 就能用：引用編號與 Markdown 記號都要拿掉。
        self.assertIn("plainAnswer(item.content)", js)
        # navigator.clipboard 在非安全內容下是 undefined，必須留後路。
        self.assertIn("document.execCommand", js)
        self.assertIn(".feedback-button.copied", CSS.read_text(encoding="utf-8"))

    def test_pwa_pages_do_not_zoom(self):
        """PWA 要像 App：兩指放大、連點兩下放大都關掉。"""
        for page in (INDEX, ADMIN):
            html = page.read_text(encoding="utf-8")
            self.assertIn("user-scalable=no", html, page.name)
            self.assertIn("maximum-scale=1", html, page.name)
            self.assertIn('src="app.js"', html, page.name)

        # viewport 的 user-scalable=no 在 iOS Safari 的瀏覽器分頁裡會被忽略，
        # 捏合手勢要自己擋一次，否則加到主畫面之前仍然放得大。
        js = APP_JS.read_text(encoding="utf-8")
        for gesture in ("gesturestart", "gesturechange", "gestureend"):
            self.assertIn(gesture, js)
        self.assertIn("touch-action: manipulation", CSS.read_text(encoding="utf-8"))

    def test_loading_states_show_a_moving_bar(self):
        """單純寫「載入中」看不出來系統還活著，也不知道要不要繼續等。"""
        css = CSS.read_text(encoding="utf-8")
        self.assertIn(".loading-state", css)
        self.assertIn("@keyframes loading-sweep", css)

        html = ADMIN.read_text(encoding="utf-8")
        # 每個分頁的初始狀態都要是會動的載入條，不能再是靜止的「載入中」文字。
        self.assertNotIn('<div class="empty-state">載入中</div>', html)
        self.assertGreaterEqual(html.count('<div class="loading-state"></div>'), 6)
        self.assertNotIn('載入中…</div>', ADMIN_JS.read_text(encoding="utf-8"))

    def test_a_failed_panel_stops_pretending_to_load(self):
        """toast 幾秒就消失，畫面還停在「載入中」的話會一直等一個不會來的東西。"""
        js = ADMIN_JS.read_text(encoding="utf-8")
        self.assertIn("function showLoadFailure", js)
        self.assertIn("重新載入", js)
        for panel in ("tuning-groups", "quality-list", "user-results", "domain-grid"):
            self.assertIn(f'showLoadFailure("{panel}"', js)
        self.assertIn(".load-failed", CSS.read_text(encoding="utf-8"))

    def test_chat_controller_persists_conversations(self):
        script = CHAT_JS.read_text(encoding="utf-8")

        self.assertIn("localStorage", script)
        self.assertIn("persistenceSnapshot", script)
        self.assertIn("catch (_)", script)
        self.assertIn("/api/chat", script)
        self.assertIn("const asked = conversation.messages", script)
        self.assertIn("const history = asked.map", script)
        self.assertIn('item.role === "user"', script)
        self.assertIn("conversation_id: conversation.id, history", script)

    def test_chat_controller_ignores_ime_confirmation_enter(self):
        script = CHAT_JS.read_text(encoding="utf-8")

        self.assertIn("event.isComposing", script)
        self.assertIn("event.keyCode === 229", script)

    def test_expired_session_is_handled_only_inside_chat_error_path(self):
        script = CHAT_JS.read_text(encoding="utf-8")

        catch_position = script.index('} catch (error) {', script.index('async function sendMessage'))
        session_position = script.index('if (error.status === 401)', script.index('async function sendMessage'))
        finally_position = script.index('} finally {', catch_position)
        self.assertLess(catch_position, session_position)
        self.assertLess(session_position, finally_position)

    def test_chat_controller_applies_server_profile(self):
        script = CHAT_JS.read_text(encoding="utf-8")

        self.assertIn("applyProfile", script)
        self.assertIn("body.assistant_name", script)
        self.assertIn("body.welcome_prompts", script)
        # 對話紀錄要跨裝置一致：回到前景再拉一次、切走前把沒送出的補送出去。
        self.assertIn("visibilitychange", script)
        self.assertIn("function refreshFromServer", script)
        self.assertIn("keepalive: true", script)
        # 沒有本機改動就不要在關閉分頁時盲推（會蓋掉別台的新版本）。
        self.assertIn("pendingPush", script)
        # 已經同步過之後，伺服器沒有＝在別台刪掉了，不可以再推回去。
        self.assertIn("lastSyncAt", script)
        # 側欄可以下拉更新（手機上最直覺的「我要看最新的」動作）。
        self.assertIn("function setupPullToRefresh", script)
        # 側欄每一段對話前面顯示編號（最上面是 1）。
        self.assertIn("conversation-order", script)
        self.assertIn("touchstart", script)
        # 每次進到空白對話都要換一組建議問題。
        self.assertIn("function pickRandom", script)
        self.assertIn("pickRandom(state.welcomePrompts, WELCOME_PROMPT_COUNT)", script)
        self.assertIn("WELCOME_PROMPT_COUNT = 5", script)


if __name__ == "__main__":
    unittest.main()
