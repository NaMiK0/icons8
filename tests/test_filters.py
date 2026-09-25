"""Правила отбора: что именно отбрасывается и что обязано уцелеть."""

import unittest

from seo_landings.domain.models import Query
from seo_landings.domain.reasons import Reason
from seo_landings.pipeline import filters
from seo_landings.settings import Settings, load_settings


def query(text: str, clicks: int = 100, impressions: int = 1000, position: float = 5.0) -> Query:
    return Query(raw=text, text=text, clicks=clicks, impressions=impressions,
                 ctr=clicks / impressions, position=position)


def policy(**overrides) -> filters.FilterPolicy:
    """Политика из настоящего config.toml — тесты проверяют и конфиг тоже."""
    return filters.FilterPolicy.from_settings(load_settings(), **overrides)


class BrandTests(unittest.TestCase):
    def test_all_spellings_from_the_export_are_caught(self):
        variants = [
            "icons8", "icon8", "icons 8", "icon 8", "8icons", "8icon", "8 icon",
            "icon8. com", "icon8 logo", "icons8 download", "icon8 free icons",
            "icons8 background remover",
        ]
        for text in variants:
            with self.subTest(query=text):
                decision = filters.decide(query(text), policy())
                self.assertFalse(decision.keep)
                self.assertEqual(decision.reason, Reason.OWN_BRAND)

    def test_generic_icons_query_survives(self):
        """«icons» отличается от «icons8» на один символ, но это самый
        ценный общий запрос выгрузки: 15257 кликов. Правило требует цифру
        именно чтобы его не задеть."""
        for text in ("icons", "icon", "free icons", "icon png"):
            with self.subTest(query=text):
                self.assertTrue(filters.decide(query(text), policy()).keep)

    def test_unrelated_query_with_a_digit_survives(self):
        for text in ("icon pack for windows 11", "3d icons", "graphic design trends 2027"):
            with self.subTest(query=text):
                self.assertTrue(filters.decide(query(text), policy()).keep)

    def test_keep_brand_flag_reverses_the_decision(self):
        decision = filters.decide(query("icons8"), policy(keep_brand=True))
        self.assertTrue(decision.keep)
        self.assertEqual(decision.source, "flag")

    def test_empty_alias_list_disables_the_rule(self):
        empty = filters.FilterPolicy.from_settings(Settings(data={"brand": {"aliases": []}}))
        self.assertTrue(filters.decide(query("icons8"), empty).keep)


class LanguageTests(unittest.TestCase):
    def test_polish_query_is_dropped(self):
        decision = filters.decide(query("kursor myszki do pobrania"), policy())
        self.assertFalse(decision.keep)
        self.assertEqual(decision.reason, Reason.NON_ENGLISH)

    def test_non_latin_alphabet_is_dropped(self):
        for text in ("иконки скачать", "アイコン", "图标"):
            with self.subTest(query=text):
                decision = filters.decide(query(text), policy())
                self.assertEqual(decision.reason, Reason.NON_ENGLISH)

    def test_english_query_with_punctuation_survives(self):
        for text in ("icons for ppt", "don't cursor", "icons & logos", "icon (free)"):
            with self.subTest(query=text):
                self.assertTrue(filters.decide(query(text), policy()).keep)


class JunkTests(unittest.TestCase):
    def test_urls_are_dropped(self):
        for text in ("https://icons8.com", "www.flaticon.com", "flaticon.com"):
            with self.subTest(query=text):
                decision = filters.decide(query(text), policy())
                self.assertFalse(decision.keep)

    def test_single_character_is_dropped(self):
        self.assertFalse(filters.decide(query("a"), policy()).keep)


class VolumeTests(unittest.TestCase):
    def test_threshold_is_off_by_default(self):
        self.assertTrue(filters.decide(query("rare icon", impressions=1), policy()).keep)

    def test_threshold_drops_small_queries(self):
        strict = filters.FilterPolicy.from_settings(
            Settings(data={"input": {"min_impressions": 500}})
        )
        decision = filters.decide(query("rare icon", impressions=100), strict)
        self.assertEqual(decision.reason, Reason.LOW_VOLUME)


class OfflineMarkerTests(unittest.TestCase):
    """Грубая подстраховка для режима без LLM."""

    def test_third_party_brands_are_dropped(self):
        for text in ("shein", "instagram logo", "apple logo copy", "verified badge copy and paste"):
            with self.subTest(query=text):
                decision = filters.decide(query(text), policy())
                self.assertEqual(decision.reason, Reason.THIRD_PARTY_BRAND)

    def test_keep_third_party_flag_reverses_the_decision(self):
        decision = filters.decide(query("instagram logo"), policy(keep_third_party=True))
        self.assertTrue(decision.keep)
        self.assertEqual(decision.source, "flag")

    def test_marker_matches_whole_words_only(self):
        """«apple» не должен ловить «pineapple icon»."""
        self.assertTrue(filters.decide(query("pineapple icon"), policy()).keep)

    def test_markers_can_be_disabled_for_the_llm_path(self):
        without = filters.FilterPolicy.from_settings(load_settings(), use_offline_markers=False)
        self.assertTrue(filters.decide(query("instagram logo"), without).keep)


class ApplyTests(unittest.TestCase):
    def test_order_is_preserved(self):
        queries = [query("icons8"), query("free icons"), query("shein")]
        decisions = filters.apply(queries, policy())
        self.assertEqual([d.query.text for d in decisions], [q.text for q in queries])
        self.assertEqual([d.keep for d in decisions], [False, True, False])


if __name__ == "__main__":
    unittest.main()
