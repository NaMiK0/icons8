"""Шаблонные тексты и правила, которыми потом будет проверяться ответ модели."""

import unittest

from seo_landings.domain.models import Query
from seo_landings.pipeline import cluster as cluster_module
from seo_landings.pipeline import content as content_module
from seo_landings.pipeline import normalize


def build(texts: list[str]):
    queries = [Query(raw=t, text=t, clicks=100, impressions=1000, ctr=0.1, position=5.0)
               for t in texts]
    groups = normalize.build_groups(queries)
    selected, _ = cluster_module.build_clusters(
        groups, cluster_module.ClusterPolicy(target=1, tolerance=0, min_queries_per_cluster=1)
    )
    return content_module.build_template_content(selected[0], "Icons8")


class LimitTests(unittest.TestCase):
    def test_title_and_description_fit_serp(self):
        page = build(["free icons download for windows and mac desktop computers"])
        self.assertLessEqual(len(page.title), content_module.TITLE_LIMIT)
        self.assertLessEqual(len(page.meta_description), content_module.DESCRIPTION_LIMIT)

    def test_clip_cuts_on_word_boundary(self):
        clipped = content_module.clip("free icons for windows desktop", 20)
        self.assertLessEqual(len(clipped), 20)
        self.assertFalse(clipped.endswith(" "))
        self.assertIn("free icons", clipped)


class StructureTests(unittest.TestCase):
    def setUp(self):
        self.page = build(["free svg icons", "free svg icon", "svg icons free"])

    def test_page_has_required_blocks(self):
        self.assertTrue(self.page.h1)
        self.assertTrue(self.page.intro)
        self.assertGreaterEqual(len(self.page.sections), 3)
        self.assertGreaterEqual(len(self.page.faq), 3)
        self.assertTrue(all(section.paragraphs for section in self.page.sections))

    def test_primary_keyword_is_in_the_heading(self):
        self.assertIn("svg", self.page.h1.casefold())

    def test_source_is_marked_as_template(self):
        self.assertEqual(self.page.source, "template")

    def test_anchor_text_is_set_for_interlinking(self):
        self.assertTrue(self.page.anchor_text)


class HonestyTests(unittest.TestCase):
    """Шаблон не должен выдумывать факты — тем же правилом проверяется LLM."""

    def test_no_invented_numbers(self):
        page = build(["free icons", "free icon", "icons free download"])
        text = " ".join(
            [page.title, page.meta_description, page.h1, page.intro]
            + [p for section in page.sections for p in section.paragraphs]
            + [item.question for item in page.faq]
            + [item.answer for item in page.faq]
        )
        self.assertEqual(content_module.suspicious_numbers(text), [])

    def test_detector_finds_invented_numbers(self):
        self.assertEqual(
            content_module.suspicious_numbers("over 1000 icons and 12 styles"), ["1000"]
        )


class TitleCaseTests(unittest.TestCase):
    def test_formats_are_uppercased(self):
        self.assertEqual(content_module.title_case("free svg icons"), "Free SVG Icons")
        self.assertEqual(content_module.title_case("icon png"), "Icon PNG")

    def test_minor_words_stay_lowercase_inside_the_heading(self):
        self.assertEqual(content_module.title_case("icons for ppt"), "Icons for PPT")

    def test_minor_word_is_capitalized_when_it_starts_the_heading(self):
        self.assertEqual(content_module.title_case("for windows icons"), "For Windows Icons")

    def test_acronyms_are_uppercased(self):
        self.assertEqual(content_module.title_case("3d icons"), "3D Icons")
        self.assertEqual(content_module.title_case("ai icon"), "AI Icon")


if __name__ == "__main__":
    unittest.main()
