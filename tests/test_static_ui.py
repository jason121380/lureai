import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "static" / "index.html"
ADMIN = ROOT / "static" / "admin.html"
CSS = ROOT / "static" / "app.css"
CHAT_JS = ROOT / "static" / "chat.js"
ADMIN_JS = ROOT / "static" / "admin.js"
LOGO = ROOT / "static" / "logo.svg"
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

        self.assertIn('src="logo.svg"', html)
        self.assertIn('href="favicon.png"', html)
        self.assertIn('href="app-icon.png"', html)
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

    def test_mobile_sidebar_opens_full_screen_in_white(self):
        css = CSS.read_text(encoding="utf-8")
        script = CHAT_JS.read_text(encoding="utf-8")

        # 全螢幕純白選單：沒有壓暗背景與陰影，頂端不會出現交界。
        mobile_rule = css.split("手機版選單開成全螢幕純白", 1)[1].split("}", 1)[0]
        self.assertIn("inset: 0;", mobile_rule)
        self.assertIn("width: 100%;", mobile_rule)
        self.assertIn("background: var(--surface);", mobile_rule)
        self.assertNotIn("box-shadow", mobile_rule)
        # 開選單不再叫出 overlay（來源抽屜仍使用）。
        opener = script.split("function openSidebar()", 1)[1].split("}", 1)[0]
        self.assertNotIn('el("drawer-overlay").hidden = false', opener)

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

        self.assertIn("模型未回應，以下為知識原文", script)
        self.assertIn("item.modelStatus", script)
        self.assertIn(".message-status.degraded", css)

    def test_installed_app_keeps_a_white_status_bar(self):
        for page in (INDEX, ADMIN):
            html = page.read_text(encoding="utf-8")
            # 深色模式沒有對應的 theme-color 時，iOS 會用灰底畫狀態列。
            self.assertIn('content="#ffffff" media="(prefers-color-scheme: dark)"', html)
            self.assertIn('content="#ffffff" media="(prefers-color-scheme: light)"', html)
            self.assertIn('name="color-scheme" content="light"', html)
        self.assertIn("color-scheme: light;", CSS.read_text(encoding="utf-8"))

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
        self.assertIn("domain=${domain}", script)

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
        # 每次進到空白對話都要換一組建議問題。
        self.assertIn("function pickRandom", script)
        self.assertIn("pickRandom(state.welcomePrompts, 3)", script)


if __name__ == "__main__":
    unittest.main()
