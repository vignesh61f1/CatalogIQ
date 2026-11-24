"""Provider tests: the mock, output validation and the OpenAI-compatible client."""
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from catalogiq.config import CATEGORIES, Config
from catalogiq.llm import (LLMError, MockProvider, OpenAICompatibleProvider, ValidationError,
                           build_provider, parse_enrichment)


class MockProviderTests(unittest.TestCase):
    def test_returns_valid_enrichment(self):
        mock = MockProvider(latency_ms=0, failure_rate=0)
        result = parse_enrichment(mock.enrich(" AMUL  butter 500G", "pck of 2"))

        self.assertEqual(result["clean_title"], "Amul Butter 500 g (Pack of 2)")
        self.assertEqual(result["category"], "Groceries")
        self.assertEqual(result["brand"], "Amul")
        self.assertIn("butter", result["tags"])
        self.assertLessEqual(len(result["tags"]), 5)

    def test_keyword_rules_pick_categories(self):
        mock = MockProvider(latency_ms=0, failure_rate=0)
        cases = {
            "tata tea gold 250g": "Beverages",
            "DOVE soap 100g": "Personal Care",
            "surf excel detergent 1kg": "Household",
            "boAt bluetooth earphones": "Electronics",
            "mens cotton shirt blue": "Fashion",
            "prestige pressure cooker 3L": "Home & Kitchen",
            "mystery object": "Other",
        }
        for title, expected in cases.items():
            category = json.loads(mock.enrich(title, ""))["category"]
            self.assertEqual(category, expected, title)
            self.assertIn(category, CATEGORIES)

    def test_unknown_brand_is_null(self):
        mock = MockProvider(latency_ms=0, failure_rate=0)
        self.assertIsNone(json.loads(mock.enrich("loose basmati rice 1kg", ""))["brand"])

    def test_waits_for_the_configured_latency(self):
        slept = []
        MockProvider(latency_ms=200, failure_rate=0, sleep=slept.append).enrich("x", "")
        self.assertEqual(slept, [0.2])

    def test_failure_rate_one_always_fails_and_zero_never_does(self):
        with self.assertRaises(LLMError):
            MockProvider(latency_ms=0, failure_rate=1.0).enrich("x", "")
        for _ in range(50):
            MockProvider(latency_ms=0, failure_rate=0).enrich("x", "")

    def test_failure_rate_is_roughly_respected(self):
        mock = MockProvider(latency_ms=0, failure_rate=0.3)
        failures = 0
        for _ in range(2000):
            try:
                mock.enrich("x", "")
            except LLMError:
                failures += 1
        self.assertTrue(450 < failures < 750, failures)


class ValidationTests(unittest.TestCase):
    def good(self, **overrides):
        data = {"clean_title": "Amul Butter", "category": "Groceries", "brand": "Amul",
                "tags": ["butter"]}
        data.update(overrides)
        return json.dumps(data)

    def test_accepts_good_output(self):
        self.assertEqual(parse_enrichment(self.good())["category"], "Groceries")

    def test_accepts_json_inside_a_code_fence(self):
        self.assertEqual(parse_enrichment("```json\n%s\n```" % self.good())["brand"], "Amul")

    def test_normalises_tags_and_brand(self):
        result = parse_enrichment(self.good(
            brand="  ", tags=["Dairy", " butter ", "dairy", "a", "b", "c", "d"]))
        self.assertIsNone(result["brand"])
        self.assertEqual(result["tags"], ["dairy", "butter", "a", "b", "c"])

    def test_rejects_bad_output(self):
        bad_outputs = [
            "not json at all",
            "[1, 2, 3]",
            self.good(category="Toys"),
            self.good(category="groceries"),  # must match exactly
            self.good(clean_title="  "),
            self.good(tags="butter"),
            self.good(brand=42),
        ]
        for output in bad_outputs:
            with self.assertRaises(ValidationError, msg=output):
                parse_enrichment(output)


class FakeLLMServer:
    """A tiny local stand-in for an OpenAI-compatible API."""

    def __init__(self, status, payload, headers=None):
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers["Content-Length"])
                outer.requests.append({
                    "path": self.path,
                    "auth": self.headers.get("Authorization"),
                    "body": json.loads(self.rfile.read(length)),
                })
                body = json.dumps(payload).encode()
                self.send_response(status)
                for name, value in (headers or {}).items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d/v1" % self.server.server_port
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class OpenAICompatibleProviderTests(unittest.TestCase):
    def test_sends_prompt_and_returns_message_content(self):
        answer = json.dumps({"clean_title": "Tata Salt 1 kg", "category": "Groceries",
                             "brand": "Tata", "tags": ["salt"]})
        fake = FakeLLMServer(200, {"choices": [{"message": {"content": answer}}]})
        self.addCleanup(fake.close)
        provider = OpenAICompatibleProvider("groq", fake.url, "test-model", api_key="secret")

        result = parse_enrichment(provider.enrich("TATA salt 1KG", "iodised"))

        self.assertEqual(result["brand"], "Tata")
        request = fake.requests[0]
        self.assertEqual(request["path"], "/v1/chat/completions")
        self.assertEqual(request["auth"], "Bearer secret")
        self.assertEqual(request["body"]["model"], "test-model")
        self.assertIn("TATA salt 1KG", request["body"]["messages"][1]["content"])

    def test_rate_limit_becomes_a_retryable_error(self):
        fake = FakeLLMServer(429, {"error": "slow down"}, headers={"Retry-After": "2"})
        self.addCleanup(fake.close)
        provider = OpenAICompatibleProvider("groq", fake.url, "test-model", api_key="secret")

        with self.assertRaises(LLMError) as caught:
            provider.enrich("x", "")

        self.assertEqual(caught.exception.retry_after, 2.0)
        self.assertIn("429", str(caught.exception))

    def test_unreachable_server_becomes_a_retryable_error(self):
        provider = OpenAICompatibleProvider("ollama", "http://127.0.0.1:9/v1", "m", timeout_s=2)
        with self.assertRaises(LLMError):
            provider.enrich("x", "")


class BuildProviderTests(unittest.TestCase):
    def test_default_is_the_mock(self):
        self.assertIsInstance(build_provider(Config(), {}), MockProvider)

    def test_real_provider_reads_its_key_from_the_environment(self):
        provider = build_provider(Config(llm_provider="groq"), {"GROQ_API_KEY": "k"})
        self.assertEqual(provider.name, "groq")
        with self.assertRaises(ValueError):
            build_provider(Config(llm_provider="groq"), {})

    def test_unknown_provider_is_rejected(self):
        with self.assertRaises(ValueError):
            build_provider(Config(llm_provider="nope"), {})


if __name__ == "__main__":
    unittest.main()
