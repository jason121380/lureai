import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "static" / "index.html"
ADMIN = ROOT / "static" / "admin.html"
CSS = ROOT / "static" / "app.css"
CHAT_JS = ROOT / "static" / "chat.js"


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

    def test_chat_page_loads_lucide_and_chat_controller(self):
        html = INDEX.read_text(encoding="utf-8")

        self.assertIn("vendor/lucide.min.js", html)
        self.assertIn("chat.js", html)

    def test_admin_page_has_operational_views(self):
        html = ADMIN.read_text(encoding="utf-8")

        for element_id in ("admin-gate", "admin-shell", "stats-grid", "knowledge-results", "audit-results", "retrieval-results"):
            self.assertIn(f'id="{element_id}"', html)
        self.assertIn('id="admin-shell" class="admin-shell" hidden', html)

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
        self.assertIn("/api/chat", script)

    def test_chat_controller_applies_server_profile(self):
        script = CHAT_JS.read_text(encoding="utf-8")

        self.assertIn("applyProfile", script)
        self.assertIn("body.assistant_name", script)
        self.assertIn("body.welcome_prompts", script)


if __name__ == "__main__":
    unittest.main()
