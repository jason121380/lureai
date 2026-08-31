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
        self.assertNotIn("settings", html)

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

        for element_id in ("admin-gate", "admin-shell", "stats-grid", "knowledge-results", "audit-results", "retrieval-results"):
            self.assertIn(f'id="{element_id}"', html)
        self.assertIn('id="admin-shell" class="admin-shell" hidden', html)
        for element_id in ("users", "user-form", "user-username", "user-password", "user-results"):
            self.assertIn(f'id="{element_id}"', html)

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
        self.assertIn("const history = conversation.messages", script)
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


if __name__ == "__main__":
    unittest.main()
