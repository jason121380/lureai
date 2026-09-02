"""模型答案在畫面上的排版：編號清單要真的數 1、2、3。

模型幾乎都會在每個編號底下再寫一段說明，那一段會把清單收掉，下一個編號就
落在新的 `<ol>` 裡——瀏覽器預設每個 `<ol>` 都從 1 開始數，於是七個步驟在
畫面上全部變成「1.」（使用者截圖回報）。
"""
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CHAT_JS = ROOT / "static" / "chat.js"


def _extract(source: str, name: str) -> str:
    """把 chat.js 裡某個函式的原始碼整段挖出來（數大括號，不用剖析器）。"""
    start = source.index(f"function {name}(")
    depth = 0
    index = source.index("{", start)
    for index in range(index, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                break
    return source[start:index + 1]


def _numbers(html: str) -> list[int]:
    """把渲染出來的 HTML 讀成「畫面上會看到的號碼」。"""
    seen: list[int] = []
    current = 0
    inside_ordered = False
    for match in re.finditer(r'<ol(?: start="(\d+)")?>|</ol>|<ul>|<li>', html):
        token = match.group(0)
        if token.startswith("<ol"):
            inside_ordered = True
            current = int(match.group(1) or 1)
        elif token == "</ol>":
            inside_ordered = False
        elif token == "<li>" and inside_ordered:
            seen.append(current)
            current += 1
    return seen


@unittest.skipUnless(shutil.which("node"), "需要 node 才能執行 chat.js 的函式")
class OrderedListTests(unittest.TestCase):
    def render(self, content: str) -> str:
        source = CHAT_JS.read_text(encoding="utf-8")
        harness = "\n".join([
            f"const BULLET_LINE = {re.search(r'const BULLET_LINE = (.*);', source).group(1)};",
            f"const ORDERED_LINE = {re.search(r'const ORDERED_LINE = (.*);', source).group(1)};",
            f"const HEADING_LINE = {re.search(r'const HEADING_LINE = (.*);', source).group(1)};",
            # 這個測試只看清單的號碼，行內樣式原樣放行就好。
            "function inlineMarkup(value) { return String(value); }",
            _extract(source, "renderAssistantMarkup"),
            "const input = require('fs').readFileSync(process.argv[2], 'utf8');",
            "process.stdout.write(renderAssistantMarkup(input, 0));",
        ])
        with tempfile.TemporaryDirectory() as temp:
            script = Path(temp) / "render.js"
            script.write_text(harness, encoding="utf-8")
            payload = Path(temp) / "answer.txt"
            payload.write_text(content, encoding="utf-8")
            return subprocess.run(
                ["node", str(script), str(payload)],
                capture_output=True, text=True, timeout=60, check=True,
            ).stdout

    def test_説明段落夾在編號中間時號碼要接下去(self):
        """使用者回報的那一則：七句預存回覆，每一句底下都有一段範例。"""
        answer = (
            "1. 先接住訊息：\n「嗨～我有看到你的訊息」\n\n"
            "2. 確認需求：\n「想先了解一下你的髮況」\n\n"
            "3. 索取照片：\n「方便傳一張照片嗎」"
        )
        self.assertEqual(_numbers(self.render(answer)), [1, 2, 3])

    def test_模型自己每一項都寫1也要數成123(self):
        answer = "1. 第一件事\n說明一\n\n1. 第二件事\n說明二\n\n1. 第三件事"
        self.assertEqual(_numbers(self.render(answer)), [1, 2, 3])

    def test_連續編號沒有被改壞(self):
        self.assertEqual(_numbers(self.render("1. 第一點\n2. 第二點\n3. 第三點")), [1, 2, 3])

    def test_模型從別的號碼開始就照他寫的(self):
        """他從 3 開始通常是接著上一則講的，不要硬拉回 1。"""
        self.assertEqual(_numbers(self.render("3. 第三點\n說明\n\n4. 第四點")), [3, 4])

    def test_標題代表換一段_號碼重新數(self):
        answer = "1. 甲一\n說明\n\n## 另一段\n\n1. 乙一\n說明\n\n2. 乙二"
        self.assertEqual(_numbers(self.render(answer)), [1, 1, 2])

    def test_中間夾破折號子項不會打斷編號(self):
        self.assertEqual(_numbers(self.render("1. 大點一\n- 子項\n\n2. 大點二")), [1, 2])


if __name__ == "__main__":
    unittest.main()
