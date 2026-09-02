"""文件裡寫的數字必須跟程式一致。

外部稽核兩次都點名「文件與實作漂移」：`brain.md` 說 LINE 每則之間預設 2~4 秒，
程式其實是 3~5；`STYLE.md` 的色彩表停在改色前的舊值。文件寫錯比沒有文件更糟——
下一個人會照著錯的規格改程式。這份測試把「可變參數」釘在同一個地方。

只釘數字與 token 值，不釘文字敘述：敘述本來就該隨情境改寫，數字不該。
"""
import re
import unittest
from pathlib import Path

from app import humanize, service
from app.answer import LINE_TIMEOUT_CEILING


ROOT = Path(__file__).resolve().parents[1]
BRAIN = (ROOT / "brain.md").read_text(encoding="utf-8")
CLAUDE = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
STYLE = (ROOT / "STYLE.md").read_text(encoding="utf-8")
CSS = (ROOT / "static" / "app.css").read_text(encoding="utf-8")


class BrainDocTests(unittest.TestCase):
    def test_line_message_gaps_match_the_code(self):
        low, high = humanize.MESSAGE_GAP_RANGE
        self.assertTrue(f"預設 {low}~{high} 秒" in BRAIN, "brain.md 的 LINE 每則間隔跟程式不一致")

    def test_line_first_delay_matches_the_code(self):
        low, high = humanize.DELAY_RANGE
        self.assertTrue(f"{low}~{high} 秒" in BRAIN, "brain.md 的首則停頓跟程式不一致")
        self.assertTrue(f"首則停頓 {low}-{high} 秒" in CLAUDE, "CLAUDE.md 的首則停頓跟程式不一致")

    def test_split_limits_match_the_code(self):
        for needle, doc, where in (
            (f"最多 {humanize.MAX_PARTS} 則", BRAIN, "brain.md 的則數上限"),
            (f"{humanize.MAX_PARTS} 則 × {humanize.MAX_LINES_PER_PART} 行 = "
             f"{humanize.MAX_PARTS * humanize.MAX_LINES_PER_PART} 行", BRAIN, "brain.md 的行數算式"),
            (f"夾在 **{humanize.MAX_LINES_MERGED} 行**", BRAIN, "brain.md 的併則上限"),
            (f"`MAX_LINES_MERGED`＝{humanize.MAX_LINES_MERGED}", CLAUDE, "CLAUDE.md 的併則上限"),
        ):
            with self.subTest(where=where):
                self.assertTrue(needle in doc, f"{where}跟程式不一致")

    def test_thresholds_match_the_code(self):
        self.assertTrue(
            f"WEAK_MATCH_SCORE = {service.WEAK_MATCH_SCORE:.2f}" in CLAUDE,
            "CLAUDE.md 寫的 WEAK_MATCH_SCORE 跟程式不一致",
        )

    def test_line_timeout_matches_the_code(self):
        self.assertTrue(
            f"（{int(LINE_TIMEOUT_CEILING)} 秒）" in CLAUDE,
            "CLAUDE.md 寫的 LINE timeout 跟 LINE_TIMEOUT_CEILING 不一致",
        )


class StyleDocTests(unittest.TestCase):
    """色彩 token 表要跟 app.css 一致——改了色卻沒改表，下一個人會改回去。"""

    def _css_token(self, name: str) -> str:
        match = re.search(rf"^\s*--{name}:\s*(#[0-9a-fA-F]{{3,8}});", CSS, re.M)
        self.assertIsNotNone(match, f"app.css 找不到 --{name}")
        return match.group(1).lower()

    def test_documented_colours_are_the_real_ones(self):
        for token in ("ink", "ink-soft", "muted", "line", "line-strong", "hover",
                      "bubble", "ok", "warning", "caution"):
            with self.subTest(token=token):
                self.assertIn(self._css_token(token), STYLE.lower(), f"STYLE.md 的 --{token} 過期了")


if __name__ == "__main__":
    unittest.main()
