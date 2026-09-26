"""Слияние ответов модели с решениями правил.

Стадия проверяется на подставном клиенте: важны не сами ответы модели, а
то, что происходит с плохими — их будет достаточно на любой живой модели.
"""

import unittest

from seo_landings.domain.models import Decision, Query
from seo_landings.domain.reasons import Reason
from seo_landings.pipeline import classify as classify_module
from seo_landings.settings import Settings

SETTINGS = Settings(data={
    "models": {"classify": "test-model"},
    "site": {"base_url": "https://icons8.com/"},
    "market": {"language": "en"},
    "llm": {"batch_size": 50},
})


def query(text: str, clicks: int = 100) -> Query:
    return Query(raw=text, text=text, clicks=clicks, impressions=1000, ctr=0.1, position=5.0)


def kept(text: str) -> Decision:
    return Decision(query=query(text), keep=True, reason=Reason.OK)


def dropped(text: str, reason: Reason) -> Decision:
    return Decision(query=query(text), keep=False, reason=reason, note="rule")


class FakeClient:
    """Клиент, отдающий заранее заданные ответы."""

    def __init__(self, *answers, fail_with: Exception | None = None):
        self.answers = list(answers)
        self.fail_with = fail_with
        self.calls = 0

    def complete_json(self, **_kwargs):
        self.calls += 1
        if self.fail_with:
            raise self.fail_with
        return self.answers.pop(0)


def verdict(index: int, keep: bool, reason: str, intent: str = "transactional") -> dict:
    return {"id": index, "keep": keep, "reason": reason, "intent": intent, "note": "n"}


def run(decisions, client, **kwargs):
    return classify_module.classify(decisions, client, SETTINGS, prompts_dir="config/prompts",
                                    **kwargs)


class MergeTests(unittest.TestCase):
    def test_model_decisions_are_applied(self):
        client = FakeClient({"items": [verdict(1, True, "ok"),
                                       verdict(2, False, "third_party_brand")]})
        result = run([kept("free icons"), kept("shein")], client)
        self.assertTrue(result.used_llm)
        self.assertTrue(result.decisions[0].keep)
        self.assertFalse(result.decisions[1].keep)
        self.assertEqual(result.decisions[1].reason, Reason.THIRD_PARTY_BRAND)
        self.assertEqual(result.decisions[1].source, "llm")

    def test_answers_are_matched_by_id_not_text(self):
        """Модель склонна копировать строку целиком вместе с метриками."""
        client = FakeClient({"items": [
            {"id": 1, "keep": False, "reason": "irrelevant",
             "query": "shein | clicks 756 | impressions 516874"},
        ]})
        result = run([kept("shein")], client)
        self.assertFalse(result.decisions[0].keep)

    def test_answer_without_id_falls_back_to_text(self):
        client = FakeClient({"items": [{"query": "shein", "keep": False,
                                        "reason": "third_party_brand"}]})
        result = run([kept("shein")], client)
        self.assertFalse(result.decisions[0].keep)

    def test_rule_decisions_on_brand_and_language_win(self):
        """Бренд и язык проверяются механически — спорить тут не о чем."""
        client = FakeClient({"items": []})
        decisions = [dropped("icons8", Reason.OWN_BRAND),
                     dropped("kursor myszki", Reason.NON_ENGLISH)]
        result = run(decisions, client)
        self.assertEqual(client.calls, 0)
        self.assertEqual([d.reason for d in result.decisions],
                         [Reason.OWN_BRAND, Reason.NON_ENGLISH])

    def test_keep_flag_survives_the_model(self):
        flagged = Decision(query=query("instagram logo"), keep=True, reason=Reason.OK,
                           note="kept by flag", source="flag")
        client = FakeClient({"items": [verdict(1, False, "third_party_brand")]})
        result = run([flagged], client)
        self.assertTrue(result.decisions[0].keep)
        self.assertEqual(result.decisions[0].source, "flag")

    def test_missing_queries_keep_rule_decisions(self):
        client = FakeClient({"items": [verdict(1, False, "irrelevant")]})
        result = run([kept("free icons"), kept("icon png")], client)
        self.assertTrue(result.decisions[1].keep)
        self.assertTrue(any("не вернула решение" in w for w in result.warnings))


class RepairTests(unittest.TestCase):
    def test_unknown_reason_becomes_irrelevant(self):
        client = FakeClient({"items": [verdict(1, False, "потому что")]})
        result = run([kept("whatever")], client)
        self.assertEqual(result.decisions[0].reason, Reason.IRRELEVANT)

    def test_keep_true_forces_reason_ok(self):
        client = FakeClient({"items": [verdict(1, True, "third_party_brand")]})
        result = run([kept("free icons")], client)
        self.assertEqual(result.decisions[0].reason, Reason.OK)

    def test_drop_with_reason_ok_is_meaningless(self):
        client = FakeClient({"items": [verdict(1, False, "ok")]})
        result = run([kept("free icons")], client)
        self.assertEqual(result.decisions[0].reason, Reason.IRRELEVANT)

    def test_unknown_intent_is_dropped(self):
        client = FakeClient({"items": [verdict(1, True, "ok", intent="весёлый")]})
        result = run([kept("free icons")], client)
        self.assertIsNone(result.decisions[0].intent)


class VariantRescueTests(unittest.TestCase):
    def test_spelling_variant_cannot_be_a_foreign_brand(self):
        """«freeicon» — это «free icon» без пробела, а не торговая марка."""
        client = FakeClient({"items": [verdict(1, True, "ok"),
                                       verdict(2, False, "third_party_brand")]})
        result = run([kept("free icon"), kept("freeicon")], client)
        rescued = result.decisions[1]
        self.assertTrue(rescued.keep)
        self.assertEqual(rescued.source, "rule")
        self.assertTrue(any("варианты написания" in w for w in result.warnings))

    def test_real_foreign_brand_stays_dropped(self):
        client = FakeClient({"items": [verdict(1, True, "ok"),
                                       verdict(2, False, "third_party_brand")]})
        result = run([kept("free icons"), kept("shein")], client)
        self.assertFalse(result.decisions[1].keep)


class DegradationTests(unittest.TestCase):
    def test_failed_batch_falls_back_to_rules(self):
        from seo_landings.llm.client import LLMError

        client = FakeClient(fail_with=LLMError("сеть недоступна"))
        result = run([kept("free icons")], client)
        self.assertFalse(result.used_llm)
        self.assertTrue(result.decisions[0].keep)
        self.assertTrue(any("не классифицирована" in w for w in result.warnings))

    def test_one_failed_batch_does_not_lose_the_others(self):
        from seo_landings.llm.client import LLMError

        class Flaky(FakeClient):
            def complete_json(self, **_kwargs):
                self.calls += 1
                if self.calls == 1:
                    raise LLMError("первая пачка не удалась")
                return {"items": [verdict(1, False, "third_party_brand")]}

        decisions = [kept("free icons"), kept("shein")]
        result = run(decisions, Flaky(), batch_size=1)
        self.assertTrue(result.used_llm)
        self.assertTrue(result.decisions[0].keep)     # пачка с ошибкой — по правилам
        self.assertFalse(result.decisions[1].keep)    # вторая пачка обработана
        self.assertTrue(any("пачка 1" in w for w in result.warnings))
        self.assertTrue(any("не вернула решение" in w for w in result.warnings))

    def test_missing_prompt_file_is_not_fatal(self):
        client = FakeClient({"items": []})
        result = classify_module.classify([kept("free icons")], client, SETTINGS,
                                          prompts_dir="config/nowhere")
        self.assertFalse(result.used_llm)
        self.assertTrue(result.decisions[0].keep)
        self.assertTrue(result.warnings)


if __name__ == "__main__":
    unittest.main()
