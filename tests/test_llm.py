"""Инфраструктура LLM: кэш, промпты, разбор ответа, повторы.

Сеть не используется: запросы подменяются заглушкой. Проверяется поведение
на плохих ответах — именно оно определяет, переживёт ли прогон живую модель.
"""

import tempfile
import unittest
from pathlib import Path

from seo_landings.llm import client as client_module
from seo_landings.llm.cache import ResponseCache, cache_key
from seo_landings.llm.client import LLMClient, LLMConfig, LLMError
from seo_landings.llm.prompts import PromptError, load_prompt


class CacheKeyTests(unittest.TestCase):
    def test_key_ignores_field_order(self):
        self.assertEqual(
            cache_key({"model": "m", "messages": [1]}),
            cache_key({"messages": [1], "model": "m"}),
        )

    def test_key_changes_with_model(self):
        self.assertNotEqual(cache_key({"model": "a"}), cache_key({"model": "b"}))


class CacheTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_roundtrip(self):
        cache = ResponseCache(directory=self.dir)
        cache.put("abc", {"items": [1]})
        self.assertEqual(cache.get("abc"), {"items": [1]})
        self.assertEqual(cache.hits, 1)

    def test_miss(self):
        cache = ResponseCache(directory=self.dir)
        self.assertIsNone(cache.get("nope"))
        self.assertEqual(cache.misses, 1)

    def test_disabled_cache_stores_nothing(self):
        cache = ResponseCache(directory=self.dir, enabled=False)
        cache.put("abc", {"items": [1]})
        self.assertIsNone(cache.get("abc"))
        self.assertFalse(list(self.dir.glob("*.json")))

    def test_corrupted_file_is_a_miss(self):
        cache = ResponseCache(directory=self.dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "abc.json").write_text("{не json", encoding="utf-8")
        self.assertIsNone(cache.get("abc"))


class ParseTests(unittest.TestCase):
    def test_plain_json(self):
        self.assertEqual(client_module._parse_json('{"a": 1}'), {"a": 1})

    def test_code_fence_is_stripped(self):
        self.assertEqual(
            client_module._parse_json('```json\n{"a": 1}\n```'), {"a": 1}
        )

    def test_null_is_rejected(self):
        """qwen3.5-27b временами отвечает литералом null вместо объекта."""
        with self.assertRaises(LLMError):
            client_module._parse_json("null")

    def test_array_is_rejected(self):
        with self.assertRaises(LLMError):
            client_module._parse_json("[1, 2]")

    def test_empty_is_rejected(self):
        with self.assertRaises(LLMError):
            client_module._parse_json("   ")


def fake_response(content: str, output_tokens: int = 10) -> dict:
    return {
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": output_tokens,
                  "completion_tokens_details": {"reasoning_tokens": 0}},
        "_seconds": 0.1,
    }


class ClientTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.client = LLMClient(
            config=LLMConfig(api_key="test-key"),
            cache=ResponseCache(directory=Path(self._tmp.name)),
        )

    def _answer(self, *contents: str) -> list[dict]:
        sent: list[dict] = []
        queue = list(contents)

        def stub(payload):
            sent.append(payload)
            return fake_response(queue.pop(0))

        self.client.transport = stub
        return sent

    def test_successful_call(self):
        self._answer('{"items": [1]}')
        result = self.client.complete_json(
            stage="classify", model="m", system="s", user="u"
        )
        self.assertEqual(result, {"items": [1]})
        self.assertEqual(self.client.usage["classify"].calls, 1)

    def test_second_call_comes_from_cache(self):
        sent = self._answer('{"items": [1]}')
        for _ in range(2):
            self.client.complete_json(stage="classify", model="m", system="s", user="u")
        self.assertEqual(len(sent), 1)
        self.assertEqual(self.client.usage["classify"].cache_hits, 1)

    def test_bad_answer_is_retried(self):
        sent = self._answer("null", '{"items": [1]}')
        result = self.client.complete_json(
            stage="classify", model="m", system="s", user="u"
        )
        self.assertEqual(result, {"items": [1]})
        self.assertEqual(len(sent), 2)
        self.assertIn("valid JSON object", sent[1]["messages"][-1]["content"])

    def test_answer_failing_validation_is_retried(self):
        sent = self._answer('{"items": []}', '{"items": [1]}')
        result = self.client.complete_json(
            stage="classify", model="m", system="s", user="u",
            validate=lambda payload: bool(payload.get("items")),
        )
        self.assertEqual(result, {"items": [1]})
        self.assertEqual(len(sent), 2)

    def test_gives_up_after_the_attempt_limit(self):
        self._answer(*["null"] * client_module.JSON_ATTEMPTS)
        with self.assertRaises(LLMError):
            self.client.complete_json(stage="classify", model="m", system="s", user="u")

    def test_bad_answers_are_not_cached(self):
        self._answer(*["null"] * client_module.JSON_ATTEMPTS)
        with self.assertRaises(LLMError):
            self.client.complete_json(stage="classify", model="m", system="s", user="u")
        self.assertFalse(list(Path(self._tmp.name).glob("*.json")))

    def test_missing_key_means_unavailable(self):
        offline = LLMClient(config=LLMConfig(api_key=""))
        self.assertFalse(offline.available)
        with self.assertRaises(LLMError):
            offline.complete_json(stage="classify", model="m", system="s", user="u")

    def test_payload_carries_json_mode_and_settings(self):
        client = LLMClient(
            config=LLMConfig(api_key="k", temperature=0, reasoning_effort="none"),
            cache=ResponseCache(enabled=False),
        )
        payload = client._payload("model-x", "system", "user")
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertEqual(payload["reasoning_effort"], "none")
        self.assertEqual(payload["temperature"], 0)
        self.assertEqual([m["role"] for m in payload["messages"]], ["system", "user"])


class PromptTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def write(self, name: str, text: str) -> None:
        (self.dir / f"{name}.md").write_text(text, encoding="utf-8")

    def test_sections_are_parsed(self):
        self.write("demo", "# Заголовок\n\n## System\n\nYou are X.\n\n## User\n\nDo {thing}.\n")
        prompt = load_prompt("demo", self.dir)
        system, user = prompt.render(thing="this")
        self.assertEqual(system, "You are X.")
        self.assertEqual(user, "Do this.")

    def test_missing_file(self):
        with self.assertRaises(PromptError):
            load_prompt("nope", self.dir)

    def test_missing_section(self):
        self.write("half", "## System\n\nonly system\n")
        with self.assertRaises(PromptError):
            load_prompt("half", self.dir)

    def test_unknown_placeholder_is_reported(self):
        self.write("demo", "## System\n\nS\n\n## User\n\nDo {thing}.\n")
        with self.assertRaises(PromptError):
            load_prompt("demo", self.dir).render(other="x")

    def test_real_classify_prompt_renders(self):
        """Промпт из репозитория должен подставляться без правок кода."""
        prompt = load_prompt("classify", "config/prompts")
        system, user = prompt.render(
            site="https://icons8.com/", business="catalog", market="en",
            count=1, queries="1. free icons | clicks 1 | impressions 1 | position 1.0",
        )
        self.assertIn("third_party_brand", system)
        self.assertIn("free icons", user)
        self.assertIn('{"items"', system)  # фигурные скобки не съедены форматированием


if __name__ == "__main__":
    unittest.main()
