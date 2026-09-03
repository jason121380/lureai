"""客服（前端）與 LINE（後端）必須用同一套規則拆則。

`static/chat.js` 的 `serviceSentences` 與 `app/humanize.py` 的 `postprocess`
是同一套規則的兩份實作，改一邊忘了改另一邊，同一段回覆在網頁與 LINE 上
就會長得不一樣（體檢 B10 實測 10 個樣本有 5 個不同）。
`tests/split_vectors.json` 是兩邊共用的向量，兩份實作都要吐出 expected。
"""
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from app.humanize import postprocess


ROOT = Path(__file__).resolve().parents[1]
VECTORS = json.loads((Path(__file__).parent / "split_vectors.json").read_text(encoding="utf-8"))
CHAT_JS = ROOT / "static" / "chat.js"
# chat.js 裡要抽出來在 node 執行的那幾個函式（依相依順序）。
JS_FUNCTIONS = ("stripAsciiDots", "cleanChatLine", "glueFragments", "wrapLine", "softenQuestions", "serviceSentences")


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


class SplitVectorTests(unittest.TestCase):
    def test_backend_matches_the_shared_vectors(self):
        for vector in VECTORS["vectors"]:
            with self.subTest(name=vector["name"]):
                self.assertEqual(postprocess(vector["input"]), vector["expected"])

    def test_a_normal_sentence_is_never_split_into_two_messages(self):
        """12 字以內的一句話就是一則，切成兩則等於打到一半按送出。"""
        for line in ("你這個月私訊大概幾則呢～", "沒關係！我來幫你分析看看～", "先看回覆率 這週抓 20 則"):
            with self.subTest(line=line):
                self.assertEqual(len(postprocess(line)), 1, line)

    def test_numbers_keep_their_dot_and_colon(self):
        joined = " ".join(postprocess("到店率 8.5% 偏低唷 晚上 10:30 之後再回"))
        self.assertIn("8.5%", joined)
        self.assertIn("10:30", joined)

    def test_the_allowed_exclamation_mark_survives(self):
        # service-12／line 語氣明文允許「！」與「～」，程式不可以再剝掉。
        self.assertIn("！", " ".join(postprocess("沒關係！我來幫你分析看看～")))

    def test_a_bare_question_keeps_a_tilde_instead_of_reading_like_a_statement(self):
        """問號不能整顆吃掉（使用者決定用「～」）。

        「你想先調哪一段？」剝掉問號、又沒有語助詞收尾，畫面上就是一句
        冷冷的陳述句；句尾本來就有「嗎」「呢」的問句聽得出來在問，照舊拿掉。"""
        self.assertTrue(postprocess("我該怎麼接？")[0].endswith("～"))
        self.assertTrue(postprocess("這樣可以嗎？")[0].endswith("嗎"))
        # 已經有「～」的不要疊出「～～」。
        self.assertTrue(postprocess("有幾個真的來店～？")[0].endswith("店～"))


@unittest.skipUnless(shutil.which("node"), "需要 node 才能執行 chat.js 的函式")
class FrontendParityTests(unittest.TestCase):
    def test_chat_js_splits_exactly_like_the_line_exit(self):
        source = CHAT_JS.read_text(encoding="utf-8")
        harness = "\n".join([
            "const SERVICE_MAX_BUBBLES = 3, SERVICE_MAX_LINES = 2, SERVICE_MAX_CHARS = 12;",
            *[_extract(source, name) for name in JS_FUNCTIONS],
            "const vectors = JSON.parse(require('fs').readFileSync(process.argv[2], 'utf8')).vectors;",
            "process.stdout.write(JSON.stringify(vectors.map((v) => serviceSentences(v.input))));",
        ])
        with tempfile.TemporaryDirectory() as temp:
            script = Path(temp) / "parity.js"
            script.write_text(harness, encoding="utf-8")
            payload = Path(temp) / "vectors.json"
            payload.write_text(json.dumps(VECTORS, ensure_ascii=False), encoding="utf-8")
            output = subprocess.run(
                ["node", str(script), str(payload)],
                capture_output=True, text=True, timeout=60, check=True,
            ).stdout
        produced = json.loads(output)
        for vector, actual in zip(VECTORS["vectors"], produced):
            with self.subTest(name=vector["name"]):
                self.assertEqual(actual, vector["expected"])

    def test_both_implementations_declare_the_same_limits(self):
        source = CHAT_JS.read_text(encoding="utf-8")
        from app import humanize

        self.assertEqual(int(re.search(r"SERVICE_MAX_BUBBLES = (\d+)", source).group(1)), humanize.MAX_PARTS)
        self.assertEqual(int(re.search(r"SERVICE_MAX_LINES = (\d+)", source).group(1)), humanize.MAX_LINES_PER_PART)
        self.assertEqual(int(re.search(r"SERVICE_MAX_CHARS = (\d+)", source).group(1)), humanize.MAX_CHARS_PER_LINE)


if __name__ == "__main__":
    unittest.main()
