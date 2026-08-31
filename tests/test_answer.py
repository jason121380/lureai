import json
import os
import unittest
from unittest.mock import patch

from app.answer import AnswerEngine, responses_url
from app.retrieval import SearchHit


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class AnswerTests(unittest.TestCase):
    def test_resolves_root_responses_url(self):
        self.assertEqual(
            responses_url("https://api.example.com"),
            "https://api.example.com/v1/responses",
        )

    def test_resolves_v1_responses_url_without_duplicating_version(self):
        self.assertEqual(
            responses_url("https://api.example.com/v1/"),
            "https://api.example.com/v1/responses",
        )

    def test_preserves_full_responses_endpoint(self):
        endpoint = "https://api.example.com/custom/responses"
        self.assertEqual(responses_url(endpoint), endpoint)

    def test_calls_responses_api_and_extracts_output_text(self):
        hit = SearchHit("chunk-1", "標題", "source.md", "section-1", "段落", "核准內容", "流程", 1.0)
        api_response = {
            "usage": {
                "input_tokens": 321,
                "input_tokens_details": {"cached_tokens": 120, "cache_write_tokens": 30},
                "output_tokens": 45,
            },
            "output": [{
                "type": "message",
                "content": [{"type": "output_text", "text": "先檢查私訊承接。[1]"}],
            }],
        }
        with patch.dict(os.environ, {
            "LLM_BASE_URL": "https://api.openai.com",
            "LLM_API_KEY": "test-key",
            "LLM_MODEL": "gpt-5.6-luna",
        }), patch("urllib.request.urlopen", return_value=FakeResponse(api_response)) as urlopen:
            answer, mode, model_status, usage = AnswerEngine().answer(
                "那下一步呢？",
                [hit],
                history=[
                    {"role": "user", "content": "私訊很多但預約少。"},
                    {"role": "assistant", "content": "先檢查私訊承接。[1]"},
                ],
            )

        request = urlopen.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(request.full_url, "https://api.openai.com/v1/responses")
        self.assertEqual(payload["model"], "gpt-5.6-luna")
        self.assertEqual(payload["reasoning"]["effort"], "low")
        self.assertFalse(payload["store"])
        self.assertEqual(payload["input"][0]["content"], "私訊很多但預約少。")
        self.assertEqual(payload["input"][1]["role"], "assistant")
        self.assertIn("那下一步呢？", payload["input"][2]["content"])
        self.assertEqual(answer, "先檢查私訊承接。[1]")
        self.assertEqual(mode, "llm")
        self.assertEqual(model_status, "used")
        self.assertEqual(usage, {
            "input_tokens": 321,
            "cached_input_tokens": 120,
            "cache_write_input_tokens": 30,
            "output_tokens": 45,
        })

    def test_records_usage_even_when_model_returns_no_output_text(self):
        hit = SearchHit("chunk-1", "標題", "source.md", "section-1", "段落", "核准內容", "流程", 1.0)
        api_response = {
            "usage": {"input_tokens": 75, "output_tokens": 10},
            "output": [],
        }
        with patch.dict(os.environ, {
            "LLM_BASE_URL": "https://api.openai.com",
            "LLM_API_KEY": "test-key",
            "LLM_MODEL": "gpt-5.6-luna",
        }), patch("urllib.request.urlopen", return_value=FakeResponse(api_response)):
            _answer, mode, model_status, usage = AnswerEngine().answer("先查什麼？", [hit])

        self.assertEqual(mode, "extractive")
        self.assertEqual(model_status, "missing_citations")
        self.assertEqual(usage["input_tokens"], 75)
        self.assertEqual(usage["output_tokens"], 10)

    def test_budget_exhausted_skips_model_call(self):
        hit = SearchHit("chunk-1", "標題", "source.md", "section-1", "段落", "核准內容", "流程", 1.0)
        with patch.dict(os.environ, {
            "LLM_BASE_URL": "https://api.openai.com",
            "LLM_API_KEY": "test-key",
            "LLM_MODEL": "gpt-5.6-luna",
        }), patch("urllib.request.urlopen", side_effect=AssertionError("model must not be called")):
            answer, mode, model_status, usage = AnswerEngine().answer(
                "先查什麼？", [hit], allow_model=False
            )

        self.assertEqual(mode, "extractive")
        self.assertEqual(model_status, "budget_exhausted")
        self.assertIn("核准內容", answer)
        self.assertEqual(usage["input_tokens"], 0)

    def test_reports_model_failure_when_falling_back(self):
        hit = SearchHit("chunk-1", "標題", "source.md", "section-1", "段落", "核准內容", "流程", 1.0)
        with patch.dict(os.environ, {
            "LLM_BASE_URL": "https://api.openai.com",
            "LLM_API_KEY": "test-key",
            "LLM_MODEL": "gpt-5.6-luna",
        }), patch("urllib.request.urlopen", side_effect=TimeoutError):
            answer, mode, model_status, usage = AnswerEngine().answer("先查什麼？", [hit])

        self.assertIn("模型暫時無法完成生成", answer)
        self.assertEqual(mode, "extractive")
        self.assertEqual(model_status, "timeout")
        self.assertEqual(usage, {
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "cache_write_input_tokens": 0,
            "output_tokens": 0,
        })


if __name__ == "__main__":
    unittest.main()
