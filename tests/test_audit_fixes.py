"""Поведение, добавленное после проверки проекта на чужих выгрузках.

Каждый класс — ответ на конкретную найденную проблему: пропавшие запросы,
отсутствие запасной модели, кэш без ключа, настройки, которые ни на что не
влияли, внутренняя аналитика на страницах.
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from seo_landings.cli import options as options_module
from seo_landings.domain.models import Cluster, Decision, LexicalGroup, MergedDuplicate, Query
from seo_landings.domain.reasons import Reason
from seo_landings.llm.cache import ResponseCache
from seo_landings.llm.client import LLMClient, LLMConfig, LLMError
from seo_landings.pipeline import accounting, filters
from seo_landings.pipeline import content as content_module
from seo_landings.render.renderer import Renderer, SiteMeta
from seo_landings.settings import Settings, load_settings, model_chain


def query(text: str, clicks: int = 100) -> Query:
    return Query(raw=text, text=text, clicks=clicks, impressions=1000, ctr=0.1, position=5.0)


class AccountingTests(unittest.TestCase):
    """На выгрузке из 3000 строк 2811 запросов не попадали никуда."""

    def setUp(self):
        self.on_page = query("free icons")
        self.left_out = query("rare icon")
        self.brand = Decision(query=query("icons8"), keep=False, reason=Reason.OWN_BRAND)
        self.decisions = [
            Decision(query=self.on_page, keep=True, reason=Reason.OK),
            Decision(query=self.left_out, keep=True, reason=Reason.OK),
            self.brand,
        ]
        self.clusters = [Cluster(slug="free-icons", primary_keyword="free icons",
                                 groups=[LexicalGroup(key="free-icon", queries=[self.on_page])])]

    def finalize(self, **overrides):
        arguments = dict(duplicates=[], truncated=[],
                         uncovered=[LexicalGroup(key="icon-rare", queries=[self.left_out])],
                         limit=500, pages=1)
        arguments.update(overrides)
        return accounting.finalize(self.decisions, **arguments)

    def test_query_outside_the_page_set_is_recorded(self):
        result = self.finalize()
        left_out = next(d for d in result if d.query is self.left_out)
        self.assertFalse(left_out.keep)
        self.assertEqual(left_out.reason, Reason.UNCLUSTERED)

    def test_truncated_queries_are_recorded(self):
        tail = query("long tail")
        result = self.finalize(truncated=[tail])
        self.assertIn(Reason.TRUNCATED, {d.reason for d in result if d.query is tail})

    def test_duplicates_are_recorded_with_what_they_merged_into(self):
        absorbed = Query(raw="Free Icons", text="free icons", clicks=5, impressions=50)
        result = self.finalize(duplicates=[MergedDuplicate(kept="free icons", absorbed=absorbed)])
        duplicate = next(d for d in result if d.reason is Reason.DUPLICATE)
        self.assertIn("Free Icons", duplicate.note)

    def test_every_row_is_accounted_for(self):
        result = self.finalize()
        self.assertEqual(accounting.unaccounted(3, result, self.clusters), 0)

    def test_a_lost_row_is_detected(self):
        """Без finalize запрос, не попавший на страницы, теряется — и это видно."""
        self.assertEqual(accounting.unaccounted(3, self.decisions, self.clusters), 1)


def fake_response(content: str) -> dict:
    return {"choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10}, "_seconds": 0.1}


class ModelFallbackTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cache = ResponseCache(directory=Path(self._tmp.name))

    def client(self, answers: dict[str, list[str]], key: str = "k") -> LLMClient:
        """Клиент, у которого каждая модель отвечает своим списком ответов."""
        client = LLMClient(config=LLMConfig(api_key=key), cache=self.cache)

        def transport(payload):
            queue = answers[payload["model"]]
            answer = queue.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return fake_response(answer)

        client.transport = transport
        return client

    def test_second_model_answers_when_the_first_does_not(self):
        client = self.client({"big": ["null"] * 3, "small": ['{"items": [1]}']})
        result = client.complete_json(stage="content", model=["big", "small"],
                                      system="s", user="u")
        self.assertEqual(result, {"items": [1]})
        self.assertEqual(len(client.fallbacks), 1)
        self.assertIn("переход на small", client.fallbacks[0])

    def test_hard_api_error_moves_to_the_next_model(self):
        client = self.client({"gone": [LLMError("HTTP 404: model not found")],
                              "small": ['{"items": [1]}']})
        result = client.complete_json(stage="cluster", model=["gone", "small"],
                                      system="s", user="u")
        self.assertEqual(result, {"items": [1]})

    def test_all_models_failing_raises(self):
        client = self.client({"a": ["null"] * 3, "b": ["null"] * 3})
        with self.assertRaises(LLMError):
            client.complete_json(stage="content", model=["a", "b"], system="s", user="u")

    def test_usage_is_tracked_per_model(self):
        client = self.client({"big": ["null"] * 3, "small": ['{"items": [1]}']})
        client.complete_json(stage="content", model=["big", "small"], system="s", user="u")
        self.assertEqual(client.usage["content:big"].calls, 3)
        self.assertEqual(client.usage["content:small"].calls, 1)


class CacheWithoutKeyTests(unittest.TestCase):
    """Проверяющие без ключа должны получить ровно страницы из out/."""

    def test_cached_answer_is_served_without_a_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = ResponseCache(directory=Path(tmp))
            with_key = LLMClient(config=LLMConfig(api_key="k"), cache=cache,
                                 transport=lambda payload: fake_response('{"items": [1]}'))
            with_key.complete_json(stage="classify", model="m", system="s", user="u")

            without_key = LLMClient(config=LLMConfig(api_key=""), cache=cache)
            result = without_key.complete_json(stage="classify", model="m",
                                               system="s", user="u")
            self.assertEqual(result, {"items": [1]})

    def test_cache_miss_without_a_key_fails_quietly(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = LLMClient(config=LLMConfig(api_key=""),
                               cache=ResponseCache(directory=Path(tmp)))
            with self.assertRaises(LLMError):
                client.complete_json(stage="classify", model=["a", "b"], system="s", user="u")
            self.assertEqual(client.fallbacks, [])


class ProductNameTests(unittest.TestCase):
    def policy(self):
        return filters.FilterPolicy.from_settings(load_settings(), use_offline_markers=False)

    def test_own_product_is_navigational(self):
        for text in ("lunacy", "lunacy app", "mega creator online"):
            with self.subTest(query=text):
                decision = filters.decide(query(text), self.policy())
                self.assertEqual(decision.reason, Reason.OWN_BRAND)

    def test_product_name_matches_whole_words_only(self):
        self.assertTrue(filters.decide(query("lunacyart icons"), self.policy()).keep)


class SettingsTests(unittest.TestCase):
    def test_model_chain_accepts_string_or_list(self):
        self.assertEqual(model_chain(Settings(data={"models": {"content": "a"}}), "content"), ["a"])
        self.assertEqual(
            model_chain(Settings(data={"models": {"content": ["a", "b"]}}), "content"), ["a", "b"]
        )

    def test_env_replaces_the_primary_model_and_keeps_fallbacks(self):
        with mock.patch.dict(os.environ, {"LLM_MODEL_CONTENT": "override"}):
            chain = model_chain(load_settings(), "content")
        self.assertEqual(chain[0], "override")
        self.assertGreater(len(chain), 1)

    def test_defaults_match_the_config_file(self):
        """Потерянный конфиг не должен молча менять модели."""
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {}, clear=True):
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                defaults = load_settings()
            finally:
                os.chdir(cwd)
            configured = load_settings("config/config.toml")
        for stage in ("classify", "cluster", "content"):
            with self.subTest(stage=stage):
                self.assertEqual(model_chain(defaults, stage)[0],
                                 model_chain(configured, stage)[0])

    def test_business_description_comes_from_config(self):
        self.assertIn("icons", load_settings().get("site.business"))


class SwitchTests(unittest.TestCase):
    """Переключатели: значение из config.toml, флаг меняет его на один запуск."""

    def resolve(self, *flags):
        return options_module.resolve(["--input", "x.csv", *flags])

    def test_defaults_come_from_the_config(self):
        options = self.resolve()
        self.assertTrue(options.use_llm)
        self.assertTrue(options.use_cache)
        self.assertTrue(options.show_metrics)
        self.assertFalse(options.keep_brand)

    def test_flags_override_in_both_directions(self):
        options = self.resolve("--no-llm", "--no-cache", "--no-metrics", "--keep-brand")
        self.assertFalse(options.use_llm)
        self.assertFalse(options.use_cache)
        self.assertFalse(options.show_metrics)
        self.assertTrue(options.keep_brand)

    def test_config_value_can_be_overridden_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "c.toml"
            config.write_text("[run]\nuse_llm = false\n", encoding="utf-8")
            options = self.resolve("--config", str(config), "--llm")
        self.assertTrue(options.use_llm)


class MetricsSwitchTests(unittest.TestCase):
    def render(self, show: bool) -> tuple[str, str]:
        group = LexicalGroup(key="free-icon", queries=[query("free icons", clicks=4321)])
        cluster = Cluster(slug="free-icons", primary_keyword="free icons", groups=[group])
        cluster.content = content_module.build_template_content(cluster, "Icons8")
        meta = SiteMeta(site_name="Icons8", base_url="https://icons8.com/",
                        source_file="gsc.csv", content_source="template",
                        cluster_source="lexical", show_metrics=show)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            Renderer(meta).render_site([cluster], out, [{"label": "brand", "queries": 1,
                                                         "clicks": "10", "examples": "icons8"}])
            return ((out / "free-icons.html").read_text(encoding="utf-8"),
                    (out / "index.html").read_text(encoding="utf-8"))

    def test_metrics_are_shown_by_default(self):
        landing, hub = self.render(True)
        self.assertIn("Searches covered by this page", landing)
        self.assertIn("4 321", landing)
        self.assertIn("Queries left out", hub)

    def test_metrics_can_be_hidden_for_publishing(self):
        landing, hub = self.render(False)
        self.assertNotIn("Searches covered by this page", landing)
        self.assertNotIn("4 321", landing)
        self.assertNotIn("4 321", hub)
        self.assertNotIn("Queries left out", hub)


if __name__ == "__main__":
    unittest.main()
