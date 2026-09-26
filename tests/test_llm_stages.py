"""Кластеризация и тексты моделью: что происходит с неидеальным ответом.

Модель отвечает не так, как в документации: забывает группы, придумывает
ключи, собирает одну страницу «про всё», пишет слишком длинный title.
Проверяется именно это — на подставном клиенте, без сети.
"""

import unittest

from seo_landings.domain.models import Cluster, LexicalGroup, Query
from seo_landings.llm.client import LLMError
from seo_landings.pipeline import llm_cluster, llm_content
from seo_landings.pipeline.cluster import ClusterPolicy
from seo_landings.pipeline.content import DESCRIPTION_LIMIT, TITLE_LIMIT, risky_claims
from seo_landings.settings import Settings

SETTINGS = Settings(data={
    "models": {"cluster": "test-model", "content": "test-model"},
    "site": {"name": "Icons8", "base_url": "https://icons8.com/"},
    "market": {"language": "en"},
})
POLICY = ClusterPolicy(target=4, tolerance=1, min_queries_per_cluster=1)


def group(key: str, *texts: str, clicks: int = 100, impressions: int = 1000) -> LexicalGroup:
    return LexicalGroup(key=key, queries=[
        Query(raw=t, text=t, clicks=clicks, impressions=impressions, ctr=0.1, position=5.0)
        for t in (texts or (key.replace("-", " "),))
    ])


class FakeClient:
    def __init__(self, *answers, fail_with: Exception | None = None):
        self.answers = list(answers)
        self.fail_with = fail_with
        self.calls = 0

    def complete_json(self, **_kwargs):
        self.calls += 1
        if self.fail_with:
            raise self.fail_with
        return self.answers.pop(0) if self.answers else {}


def cluster_answer(*specs) -> dict:
    return {"clusters": [
        {"primary_keyword": keyword, "slug": keyword.replace(" ", "-"),
         "intent": "transactional", "group_keys": list(keys), "rationale": "r"}
        for keyword, keys in specs
    ]}


def build(groups, client, policy=POLICY):
    return llm_cluster.build_clusters(groups, client, SETTINGS, policy,
                                      prompts_dir="config/prompts")


class ClusterAssemblyTests(unittest.TestCase):
    def test_groups_are_merged_as_the_model_says(self):
        groups = [group("cursor-custom"), group("mouse-pointer"), group("free-icon")]
        client = FakeClient(cluster_answer(
            ("custom cursor", ["cursor-custom", "mouse-pointer"]),
            ("free icons", ["free-icon"]),
        ))
        result = build(groups, client)
        self.assertTrue(result.used_llm)
        self.assertEqual(len(result.clusters), 2)
        cursor = next(c for c in result.clusters if "cursor" in c.slug)
        self.assertEqual({g.key for g in cursor.groups}, {"cursor-custom", "mouse-pointer"})

    def test_unknown_group_keys_are_reported_not_fatal(self):
        client = FakeClient(cluster_answer(("free icons", ["free-icon", "выдумка"])))
        result = build([group("free-icon")], client)
        self.assertEqual(len(result.clusters), 1)
        self.assertTrue(any("несуществующих групп" in w for w in result.warnings))

    def test_forgotten_groups_are_placed_by_shared_words(self):
        groups = [group("free-icon"), group("free-icon-png")]
        client = FakeClient(cluster_answer(("free icons", ["free-icon"])))
        result = build(groups, client)
        placed = {g.key for c in result.clusters for g in c.groups}
        self.assertEqual(placed, {"free-icon", "free-icon-png"})
        self.assertTrue(any("забыла" in w for w in result.warnings))

    def test_group_repeated_in_two_clusters_is_used_once(self):
        client = FakeClient(cluster_answer(
            ("free icons", ["free-icon"]), ("icons", ["free-icon"]),
        ))
        result = build([group("free-icon")], client)
        used = [g.key for c in result.clusters for g in c.groups]
        self.assertEqual(used, ["free-icon"])


class MegaClusterTests(unittest.TestCase):
    """Модель склонна собрать одну широкую страницу «про всё»."""

    def test_model_merge_is_not_undone_just_to_hit_the_count(self):
        """Объединение по смыслу — то, ради чего звали модель; ломать его,
        добивая число страниц, бессмысленно."""
        groups = [group("cursor-custom"), group("mouse-pointer"), group("free-icon")]
        client = FakeClient(cluster_answer(
            ("custom cursor", ["cursor-custom", "mouse-pointer"]),
            ("free icons", ["free-icon"]),
        ))
        result = build(groups, client, ClusterPolicy(target=8, tolerance=0,
                                                     min_queries_per_cluster=1))
        self.assertEqual(len(result.clusters), 2)

    def test_oversized_cluster_is_split_to_reach_the_target(self):
        groups = [group("cursor-custom"), group("favicon"), group("folder-icon"),
                  group("png-icon"), group("svg-icon")]
        client = FakeClient(cluster_answer(
            ("everything", [g.key for g in groups]),
        ))
        result = build(groups, client, ClusterPolicy(target=4, tolerance=1,
                                                     min_queries_per_cluster=1))
        self.assertGreaterEqual(len(result.clusters), 3)
        self.assertTrue(any("разделён" in w for w in result.warnings))

    def test_indivisible_data_does_not_loop_forever(self):
        groups = [group("icon")]
        client = FakeClient(cluster_answer(("icons", ["icon"])))
        result = build(groups, client, ClusterPolicy(target=10, tolerance=0,
                                                     min_queries_per_cluster=1))
        self.assertEqual(len(result.clusters), 1)
        self.assertTrue(any("не делятся" in w for w in result.warnings))


class ClusterDegradationTests(unittest.TestCase):
    def test_api_failure_returns_nothing_so_the_lexical_path_runs(self):
        result = build([group("free-icon")], FakeClient(fail_with=LLMError("нет сети")))
        self.assertFalse(result.used_llm)
        self.assertEqual(result.clusters, [])
        self.assertTrue(result.warnings)

    def test_empty_input(self):
        result = build([], FakeClient())
        self.assertEqual(result.clusters, [])


def page_answer(**overrides) -> dict:
    answer = {
        "title": "Free Icons | Icons8",
        "meta_description": "Download free icons in SVG and PNG.",
        "h1": "Free Icons",
        "intro": "Intro paragraph.",
        "sections": [{"heading": f"Section {i}", "paragraphs": ["Text."]} for i in range(3)],
        "faq": [{"question": f"Q{i}?", "answer": "A."} for i in range(4)],
        "anchor_text": "free icons",
    }
    answer.update(overrides)
    return answer


def content_for(answer, keyword="free icons"):
    cluster = Cluster(slug="free-icons", primary_keyword=keyword,
                      groups=[group("free-icon", "free icons", "free icon")])
    client = FakeClient(answer)
    return llm_content.build_content(cluster, [cluster], client, SETTINGS,
                                     prompts_dir="config/prompts")


class ContentTests(unittest.TestCase):
    def test_model_answer_is_used(self):
        result = content_for(page_answer())
        self.assertTrue(result.used_llm)
        self.assertEqual(result.content.source, "llm")
        self.assertEqual(result.content.h1, "Free Icons")

    def test_too_long_title_is_clipped_and_reported(self):
        result = content_for(page_answer(title="Custom Cursor: Download and Install " * 3))
        self.assertLessEqual(len(result.content.title), TITLE_LIMIT)
        self.assertTrue(any("title обрезан" in w for w in result.warnings))

    def test_too_long_description_is_clipped(self):
        result = content_for(page_answer(meta_description="word " * 60))
        self.assertLessEqual(len(result.content.meta_description), DESCRIPTION_LIMIT)

    def test_missing_blocks_are_topped_up_from_the_template(self):
        result = content_for(page_answer(
            sections=[{"heading": "Only one", "paragraphs": ["Text."]}],
            faq=[{"question": "Q?", "answer": "A."}],
        ))
        self.assertGreaterEqual(len(result.content.sections), llm_content.MIN_SECTIONS)
        self.assertGreaterEqual(len(result.content.faq), llm_content.MIN_FAQ)
        self.assertTrue(any("добраны шаблонными" in w for w in result.warnings))

    def test_malformed_blocks_are_skipped(self):
        result = content_for(page_answer(
            sections=[{"heading": "Good", "paragraphs": ["Text."]}, {"heading": "No text"},
                      "строка вместо объекта"],
        ))
        headings = [s.heading for s in result.content.sections]
        self.assertIn("Good", headings)
        self.assertNotIn("No text", headings)

    def test_api_failure_falls_back_to_the_template(self):
        cluster = Cluster(slug="free-icons", primary_keyword="free icons",
                          groups=[group("free-icon")])
        result = llm_content.build_content(cluster, [cluster],
                                           FakeClient(fail_with=LLMError("нет сети")),
                                           SETTINGS, prompts_dir="config/prompts")
        self.assertFalse(result.used_llm)
        self.assertEqual(result.content.source, "template")
        self.assertTrue(result.content.sections)


class ClaimDetectionTests(unittest.TestCase):
    """Модель не знает условий каталога и заполняет пробелы догадками."""

    def test_licensing_claims_are_surfaced(self):
        result = content_for(page_answer(
            intro="No attribution is required and no account is needed.",
        ))
        self.assertTrue(any("утверждения о лицензиях" in w for w in result.warnings))

    def test_invented_numbers_are_surfaced(self):
        result = content_for(page_answer(intro="Over 1000000 icons are available."))
        self.assertTrue(any("числа" in w for w in result.warnings))

    def test_ordinary_copy_is_not_flagged(self):
        result = content_for(page_answer())
        self.assertEqual(result.warnings, [])

    def test_detector_returns_whole_sentences(self):
        found = risky_claims("Icons scale well. A subscription is required for some sets.")
        self.assertEqual(len(found), 1)
        self.assertIn("subscription", found[0])


if __name__ == "__main__":
    unittest.main()
