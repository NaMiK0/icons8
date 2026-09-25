"""Нормализация, склейка дублей и лексические группы."""

import unittest

from seo_landings.domain.models import Query
from seo_landings.pipeline import normalize


def query(text: str, clicks: int = 0, impressions: int = 0, position: float | None = None) -> Query:
    return Query(raw=text, text=text, clicks=clicks, impressions=impressions,
                 ctr=clicks / impressions if impressions else 0.0, position=position)


class NormalizeTextTests(unittest.TestCase):
    def test_case_and_spaces(self):
        self.assertEqual(normalize.normalize_text("  Free   ICONS "), "free icons")

    def test_edge_punctuation(self):
        self.assertEqual(normalize.normalize_text("«free icons»!"), "free icons")


class DedupeTests(unittest.TestCase):
    def test_metrics_are_summed(self):
        merged, duplicates = normalize.dedupe([
            query("Free Icons", clicks=100, impressions=1000, position=4.0),
            query("free icons", clicks=50, impressions=1000, position=6.0),
        ])
        self.assertEqual(len(merged), 1)
        self.assertEqual(len(duplicates), 1)
        self.assertEqual(merged[0].clicks, 150)
        self.assertEqual(merged[0].impressions, 2000)

    def test_position_is_weighted_by_impressions(self):
        merged, _ = normalize.dedupe([
            query("free icons", clicks=10, impressions=9000, position=2.0),
            query("Free icons", clicks=10, impressions=1000, position=10.0),
        ])
        self.assertAlmostEqual(merged[0].position, 2.8)

    def test_ctr_is_recomputed(self):
        merged, _ = normalize.dedupe([
            query("free icons", clicks=100, impressions=1000),
            query("FREE ICONS", clicks=100, impressions=1000),
        ])
        self.assertAlmostEqual(merged[0].ctr, 0.1)

    def test_distinct_queries_are_kept(self):
        merged, duplicates = normalize.dedupe([query("free icons"), query("custom cursor")])
        self.assertEqual(len(merged), 2)
        self.assertEqual(duplicates, [])


class LexicalKeyTests(unittest.TestCase):
    def test_word_order_does_not_matter(self):
        self.assertEqual(
            normalize.lexical_key("free icons download"),
            normalize.lexical_key("download free icons"),
        )

    def test_plural_and_singular_match(self):
        self.assertEqual(normalize.lexical_key("free icons"), normalize.lexical_key("free icon"))

    def test_stopwords_are_dropped(self):
        self.assertEqual(normalize.lexical_key("icons for website"),
                         normalize.lexical_key("website icons"))

    def test_punctuation_is_ignored(self):
        self.assertEqual(normalize.lexical_key("icon8. com"), normalize.lexical_key("icon8 com"))

    def test_all_stopwords_query_keeps_tokens(self):
        self.assertTrue(normalize.lexical_key("for the best"))


class GroupingTests(unittest.TestCase):
    def test_variants_land_in_one_group(self):
        groups = normalize.build_groups([
            query("free icons", clicks=3790, impressions=49369, position=5.0),
            query("free icon", clicks=2677, impressions=24736, position=3.7),
            query("icons free", clicks=1876, impressions=13110, position=3.2),
        ])
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].canonical, "free icons")
        self.assertEqual(groups[0].clicks, 3790 + 2677 + 1876)

    def test_typo_is_merged_into_canonical_group(self):
        groups = normalize.build_groups([
            query("custom cursor", clicks=5204, impressions=299291, position=5.2),
            query("custom curser", clicks=114, impressions=5871, position=5.7),
        ])
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0].queries), 2)

    def test_short_keys_are_not_merged_by_typo_rule(self):
        """«icon» и «icons8» различаются на один символ, но это разный спрос."""
        groups = normalize.build_groups([query("ico", clicks=94), query("ic", clicks=10)])
        self.assertEqual(len(groups), 2)

    def test_groups_are_sorted_by_potential(self):
        groups = normalize.build_groups([
            query("low potential", clicks=100, impressions=1000, position=1.5),
            query("high potential", clicks=10, impressions=1000, position=9.0),
        ])
        self.assertEqual(groups[0].canonical, "high potential")


class TruncateTests(unittest.TestCase):
    def test_keeps_top_by_potential(self):
        kept, dropped = normalize.truncate([
            query("ranked first", clicks=9999, impressions=1000, position=1.0),
            query("has room to grow", clicks=10, impressions=1000, position=9.0),
        ], limit=1)
        self.assertEqual(kept[0].text, "has room to grow")
        self.assertEqual(dropped[0].text, "ranked first")

    def test_no_limit_keeps_everything(self):
        queries = [query("a"), query("b")]
        kept, dropped = normalize.truncate(queries, limit=None)
        self.assertEqual(len(kept), 2)
        self.assertEqual(dropped, [])


if __name__ == "__main__":
    unittest.main()
