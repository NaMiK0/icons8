"""Страницы должны быть самодостаточными и правильно размеченными."""

import json
import re
import tempfile
import unittest
from pathlib import Path

from seo_landings.domain.models import Cluster, LexicalGroup, Query
from seo_landings.pipeline import content as content_module
from seo_landings.render.renderer import Renderer, SiteMeta

META = SiteMeta(
    site_name="Icons8",
    base_url="https://icons8.com/",
    source_file="gsc_queries.csv",
    content_source="template",
    cluster_source="lexical",
    generated_at="2026-09-25",
)


def cluster(primary: str, *texts: str) -> Cluster:
    queries = [
        Query(raw=text, text=text, clicks=100, impressions=1000, ctr=0.1, position=5.0)
        for text in (primary, *texts)
    ]
    group = LexicalGroup(key=primary.replace(" ", "-"), queries=queries)
    item = Cluster(slug=primary.replace(" ", "-"), primary_keyword=primary, groups=[group])
    item.content = content_module.build_template_content(item, "Icons8")
    return item


class RenderTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.out = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.clusters = [
            cluster("free icons", "free icon", "icons free"),
            cluster("custom cursor", "cursor download"),
            cluster("favicon", "favicon download"),
        ]
        self.written = Renderer(META).render_site(self.clusters, self.out)

    def read(self, name: str) -> str:
        return (self.out / name).read_text(encoding="utf-8")


class StructureTests(RenderTestCase):
    def test_one_file_per_cluster_plus_hub(self):
        self.assertEqual(len(self.written), 4)
        self.assertTrue((self.out / "index.html").exists())
        self.assertTrue((self.out / "free-icons.html").exists())

    def test_single_h1_per_page(self):
        for page in self.out.glob("*.html"):
            with self.subTest(page=page.name):
                self.assertEqual(len(re.findall(r"<h1[ >]", page.read_text(encoding="utf-8"))), 1)

    def test_head_metadata(self):
        html = self.read("free-icons.html")
        self.assertIn('<html lang="en">', html)
        self.assertIn('<meta name="description"', html)
        self.assertIn('<link rel="canonical" href="https://icons8.com/free-icons.html">', html)
        self.assertIn('property="og:title"', html)

    def test_headings_and_faq_are_marked_up(self):
        html = self.read("free-icons.html")
        self.assertGreaterEqual(len(re.findall(r"<h2[ >]", html)), 3)
        self.assertGreaterEqual(len(re.findall(r"<h3[ >]", html)), 3)

    def test_breadcrumbs_present(self):
        self.assertIn('aria-label="Breadcrumb"', self.read("free-icons.html"))


class SelfContainedTests(RenderTestCase):
    def test_no_external_requests(self):
        """Требование «открывается в браузере» понимается буквально: файл не
        должен тянуть ни шрифты, ни стили, ни картинки."""
        for page in self.out.glob("*.html"):
            html = page.read_text(encoding="utf-8")
            with self.subTest(page=page.name):
                self.assertEqual(re.findall(r'src="https?://[^"]+"', html), [])
                self.assertEqual(re.findall(r'<link rel="stylesheet"', html), [])

    def test_assets_are_inline_svg_with_labels(self):
        html = self.read("free-icons.html")
        self.assertIn("<svg", html)
        self.assertIn('role="img"', html)
        self.assertIn("aria-label=", html)


class StructuredDataTests(RenderTestCase):
    def _json_ld(self, name: str):
        match = re.search(
            r'<script type="application/ld\+json">(.*?)</script>', self.read(name), re.S
        )
        return json.loads(match.group(1))

    def test_landing_graph_types(self):
        graph = self._json_ld("free-icons.html")["@graph"]
        self.assertEqual(
            {node["@type"] for node in graph}, {"WebPage", "BreadcrumbList", "FAQPage"}
        )

    def test_faq_matches_the_page(self):
        graph = self._json_ld("free-icons.html")["@graph"]
        faq = next(node for node in graph if node["@type"] == "FAQPage")
        questions = [item["name"] for item in faq["mainEntity"]]
        for question in questions:
            self.assertIn(question, self.read("free-icons.html"))

    def test_index_lists_every_page(self):
        data = self._json_ld("index.html")
        self.assertEqual(len(data["hasPart"]), len(self.clusters))


class InterlinkingTests(RenderTestCase):
    def test_every_page_links_to_its_siblings(self):
        for item in self.clusters:
            html = self.read(f"{item.slug}.html")
            siblings = [o.slug for o in self.clusters if o.slug != item.slug]
            with self.subTest(page=item.slug):
                for slug in siblings:
                    self.assertIn(f'href="{slug}.html"', html)

    def test_hub_links_to_every_page(self):
        html = self.read("index.html")
        for item in self.clusters:
            self.assertIn(f'href="{item.slug}.html"', html)

    def test_links_are_relative(self):
        html = self.read("free-icons.html")
        self.assertIn('href="custom-cursor.html"', html)


class RerunTests(RenderTestCase):
    def test_pages_from_a_previous_run_are_removed(self):
        """Иначе в выдаче копятся страницы прошлых прогонов со ссылками в никуда."""
        stale = self.out / "obsolete-page.html"
        stale.write_text("<html></html>", encoding="utf-8")
        Renderer(META).render_site(self.clusters, self.out)
        self.assertFalse(stale.exists())
        self.assertTrue((self.out / "index.html").exists())


if __name__ == "__main__":
    unittest.main()
