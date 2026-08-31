import unittest

from app.answer import chat_completions_url


class AnswerTests(unittest.TestCase):
    def test_resolves_root_openai_compatible_url(self):
        self.assertEqual(
            chat_completions_url("https://api.example.com"),
            "https://api.example.com/v1/chat/completions",
        )

    def test_resolves_v1_base_without_duplicating_version(self):
        self.assertEqual(
            chat_completions_url("https://api.example.com/v1/"),
            "https://api.example.com/v1/chat/completions",
        )

    def test_preserves_full_chat_completions_endpoint(self):
        endpoint = "https://api.example.com/custom/chat/completions"
        self.assertEqual(chat_completions_url(endpoint), endpoint)


if __name__ == "__main__":
    unittest.main()
