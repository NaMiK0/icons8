"""Лексическая кластеризация — фолбэк, работающий без модели."""

import unittest

from seo_landings.domain.models import Query
from seo_landings.pipeline import cluster as cluster_module
from seo_landings.pipeline import normalize

POLICY = cluster_module.ClusterPolicy(target=10, tolerance=2, min_queries_per_cluster=2)


def query(text: str, clicks: int = 100, impressions: int = 1000, position: float = 5.0) -> Query:
    return Query(raw=text, text=text, clicks=clicks, impressions=impressions,
                 ctr=clicks / impressions, position=position)


def clusters_for(texts: list[str], policy=POLICY):
    groups = normalize.build_groups([query(text) for text in texts])
    return cluster_module.build_clusters(groups, policy)


class GenericTokenTests(unittest.TestCase):
    def test_ubiquitous_word_is_generic(self):
        """«icon» есть почти в каждом запросе — склеивать по нему нельзя."""
        groups = normalize.build_groups([
            query(text) for text in
            ["free icons", "icon png", "icon svg", "desktop icon", "folder icon", "custom cursor"]
        ])
        self.assertIn("icon", cluster_module.generic_tokens(groups))
        self.assertNotIn("cursor", cluster_module.generic_tokens(groups))


class MergingTests(unittest.TestCase):
    def test_related_wording_lands_on_one_page(self):
        selected, _ = clusters_for(["custom cursor", "cursor download", "custom cursors"])
        self.assertEqual(len(selected), 1)

    def test_clusters_do_not_chain_through_a_shared_word(self):
        """«cursor download» и «icon download» делят слово download, но это
        разные страницы. Сравнение идёт с затравкой, а не с накопленным
        набором слов, иначе половина выгрузки слипается в один кластер."""
        selected, _ = clusters_for([
            "custom cursor", "cursor download", "icon download", "free icons download",
            "favicon download", "desktop icon download",
        ])
        slugs = {c.slug for c in selected}
        cursor_page = next(c for c in selected if "cursor" in c.slug)
        self.assertGreater(len(slugs), 1)
        self.assertNotIn("icon download", {q.text for q in cursor_page.queries})


class SelectionTests(unittest.TestCase):
    def test_target_count_is_respected(self):
        texts = [f"topic{i} word{i}" for i in range(30)]
        selected, leftover = clusters_for(texts, cluster_module.ClusterPolicy(
            target=5, tolerance=2, min_queries_per_cluster=1
        ))
        self.assertEqual(len(selected), 5)
        self.assertTrue(leftover)

    def test_pages_are_ordered_by_potential(self):
        selected, _ = clusters_for(
            ["alpha thing", "beta thing", "gamma item"],
            cluster_module.ClusterPolicy(target=3, tolerance=0, min_queries_per_cluster=1),
        )
        potentials = [c.potential for c in selected]
        self.assertEqual(potentials, sorted(potentials, reverse=True))

    def test_empty_input(self):
        self.assertEqual(cluster_module.build_clusters([], POLICY), ([], []))


class SlugTests(unittest.TestCase):
    def test_slug_is_readable(self):
        self.assertEqual(cluster_module.slugify("Free SVG icons!"), "free-svg-icons")

    def test_slug_is_trimmed_on_word_boundary(self):
        slug = cluster_module.slugify("free icons for windows and mac desktop users", 20)
        self.assertLessEqual(len(slug), 20)
        self.assertFalse(slug.endswith("-"))

    def test_slugs_are_unique(self):
        selected, _ = clusters_for(["free icons", "free icon", "icons free"])
        self.assertEqual(len({c.slug for c in selected}), len(selected))


class IntentTests(unittest.TestCase):
    def test_download_intent(self):
        selected, _ = clusters_for(["cursor download", "custom cursor"])
        self.assertEqual(selected[0].intent, "transactional")

    def test_informational_intent(self):
        selected, _ = clusters_for(["how to make icons", "what is an icon"])
        self.assertEqual(selected[0].intent, "informational")


if __name__ == "__main__":
    unittest.main()
