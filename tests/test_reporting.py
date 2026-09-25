"""Отчёт — источник цифр для README, поэтому он тоже под тестом."""

import csv
import json
import tempfile
import unittest
from pathlib import Path

from seo_landings.domain.models import Decision, Query
from seo_landings.domain.reasons import Reason
from seo_landings.reporting.excluded import write_excluded
from seo_landings.reporting.report import RunReport


def query(text: str, clicks: int = 100, impressions: int = 1000, position: float = 5.0) -> Query:
    return Query(raw=text, text=text, clicks=clicks, impressions=impressions,
                 ctr=clicks / impressions, position=position)


def dropped(text: str, reason: Reason, clicks: int = 100) -> Decision:
    return Decision(query=query(text, clicks=clicks), keep=False, reason=reason, note="—")


def kept(text: str, clicks: int = 100) -> Decision:
    return Decision(query=query(text, clicks=clicks), keep=True, reason=Reason.OK)


class FilterStatsTests(unittest.TestCase):
    def setUp(self):
        self.report = RunReport()
        self.report.add_filter_stats([
            kept("free icons", clicks=3790),
            dropped("icons8", Reason.OWN_BRAND, clicks=26306),
            dropped("icon8", Reason.OWN_BRAND, clicks=10031),
            dropped("shein", Reason.THIRD_PARTY_BRAND, clicks=756),
        ])

    def test_counts(self):
        self.assertEqual(self.report.filters["kept"], 1)
        self.assertEqual(self.report.filters["dropped"], 3)

    def test_clicks_are_split_between_kept_and_dropped(self):
        self.assertEqual(self.report.filters["kept_clicks"], 3790)
        self.assertEqual(self.report.filters["dropped_clicks"], 26306 + 10031 + 756)

    def test_reasons_are_grouped_with_examples(self):
        brand = self.report.filters["by_reason"]["own_brand"]
        self.assertEqual(brand["queries"], 2)
        self.assertEqual(brand["examples"][0], "icons8")  # самый заметный пример первым
        self.assertTrue(brand["label"])

    def test_reasons_are_sorted_by_frequency(self):
        reasons = list(self.report.filters["by_reason"])
        self.assertEqual(reasons[0], "own_brand")


class WarningTests(unittest.TestCase):
    def test_warnings_are_unique(self):
        report = RunReport()
        report.warn("одно и то же")
        report.warn("одно и то же")
        self.assertEqual(report.warnings, ["одно и то же"])


class FileTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_excluded_csv_is_sorted_by_clicks(self):
        path = self.tmp / "nested" / "excluded.csv"
        count = write_excluded([
            dropped("small", Reason.IRRELEVANT, clicks=10),
            dropped("big", Reason.OWN_BRAND, clicks=9000),
            kept("stays"),
        ], path)

        self.assertEqual(count, 2)
        with path.open(encoding="utf-8-sig") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual([row["query"] for row in rows], ["big", "small"])
        self.assertEqual(rows[0]["reason"], "own_brand")
        self.assertNotIn("stays", path.read_text(encoding="utf-8-sig"))

    def test_report_json_is_readable(self):
        path = self.tmp / "report.json"
        report = RunReport()
        report.input = {"path": "data/gsc_queries.csv", "queries": 100}
        report.mode = {"llm": False}
        report.add_filter_stats([kept("free icons"), dropped("icons8", Reason.OWN_BRAND)])
        report.warn("стадия LLM не подключена")
        report.write(path)

        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["input"]["queries"], 100)
        self.assertEqual(data["filters"]["dropped"], 1)
        self.assertIn("generated_at", data)
        self.assertEqual(data["warnings"], ["стадия LLM не подключена"])


if __name__ == "__main__":
    unittest.main()
