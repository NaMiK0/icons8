"""Загрузчик обязан пережить выгрузку в любом виде, кроме отсутствия запросов."""

import tempfile
import unittest
import zipfile
from pathlib import Path

from seo_landings.ingest.loader import InputError, load_export

STANDARD = "query,clicks,impressions,ctr,position\nfree icons,3790,49369,0.0768,5.0\n"


class LoaderTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def write(self, name: str, text: str, encoding: str = "utf-8") -> Path:
        path = self.tmp / name
        path.write_text(text, encoding=encoding)
        return path


class FormatTests(LoaderTestCase):
    def test_standard_export(self):
        result = load_export(self.write("gsc.csv", STANDARD))
        self.assertEqual(result.rows_read, 1)
        query = result.queries[0]
        self.assertEqual(query.text, "free icons")
        self.assertEqual(query.clicks, 3790)
        self.assertEqual(query.impressions, 49369)
        self.assertAlmostEqual(query.ctr, 0.0768)
        self.assertEqual(query.position, 5.0)

    def test_bom_is_stripped(self):
        path = self.write("bom.csv", STANDARD, encoding="utf-8-sig")
        result = load_export(path)
        self.assertIn("query", result.columns_found)

    def test_semicolon_delimiter(self):
        text = "query;clicks;impressions;ctr;position\nfree icons;3790;49369;0,0768;5,0\n"
        result = load_export(self.write("semi.csv", text))
        self.assertEqual(result.delimiter, ";")
        self.assertEqual(result.queries[0].clicks, 3790)
        self.assertAlmostEqual(result.queries[0].ctr, 0.0768)

    def test_tab_delimiter(self):
        text = "query\tclicks\timpressions\tctr\tposition\nfree icons\t3790\t49369\t0.0768\t5.0\n"
        result = load_export(self.write("tsv.csv", text))
        self.assertEqual(result.queries[0].impressions, 49369)

    def test_zip_archive(self):
        path = self.tmp / "export.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("Queries.csv", STANDARD)
        result = load_export(path)
        self.assertEqual(result.queries[0].text, "free icons")

    def test_quoted_values_with_commas(self):
        text = 'query,clicks,impressions,ctr,position\n"free icons","3,790","49,369",7.68%,5.0\n'
        result = load_export(self.write("quoted.csv", text))
        self.assertEqual(result.queries[0].clicks, 3790)
        self.assertEqual(result.queries[0].impressions, 49369)
        self.assertAlmostEqual(result.queries[0].ctr, 0.0768)


class HeaderTests(LoaderTestCase):
    def test_localized_headers(self):
        text = "Запрос,Клики,Показы,CTR,Средняя позиция\nfree icons,3790,49369,7.68%,5.0\n"
        result = load_export(self.write("ru.csv", text))
        self.assertEqual(result.queries[0].clicks, 3790)
        self.assertAlmostEqual(result.queries[0].ctr, 0.0768)

    def test_shuffled_columns_and_extra_column(self):
        text = (
            "Position,Top queries,Page,Impressions,Clicks\n"
            "5.0,free icons,/icons,49369,3790\n"
        )
        result = load_export(self.write("shuffled.csv", text))
        query = result.queries[0]
        self.assertEqual(query.text, "free icons")
        self.assertEqual(query.clicks, 3790)
        self.assertEqual(query.position, 5.0)

    def test_missing_ctr_column_is_computed(self):
        text = "query,clicks,impressions,position\nfree icons,3790,49369,5.0\n"
        result = load_export(self.write("noctr.csv", text))
        self.assertAlmostEqual(result.queries[0].ctr, 3790 / 49369)
        self.assertIn("ctr", result.columns_missing)
        self.assertTrue(result.warnings)

    def test_query_only_export(self):
        text = "query\nfree icons\ncustom cursor\n"
        result = load_export(self.write("queries_only.csv", text))
        self.assertEqual(len(result.queries), 2)
        self.assertEqual(result.queries[0].clicks, 0)
        self.assertIsNone(result.queries[0].position)

    def test_missing_query_column_is_fatal(self):
        text = "page,clicks,impressions\n/icons,3790,49369\n"
        with self.assertRaises(InputError) as error:
            load_export(self.write("nopage.csv", text))
        self.assertIn("page", str(error.exception))


class ResilienceTests(LoaderTestCase):
    def test_empty_rows_are_dropped_before_parsing(self):
        """Полностью пустая строка — структурный мусор, а не пропущенный запрос."""
        text = STANDARD + "\n,,,,\ncustom cursor,5204,299291,0.0174,5.2\n"
        result = load_export(self.write("blanks.csv", text))
        self.assertEqual(len(result.queries), 2)
        self.assertEqual(result.rows_read, 2)
        self.assertEqual(result.rows_skipped, 0)

    def test_row_with_metrics_but_no_query_is_counted_as_skipped(self):
        text = STANDARD + ",100,200,0.5,3.0\n"
        result = load_export(self.write("noquery.csv", text))
        self.assertEqual(len(result.queries), 1)
        self.assertEqual(result.rows_skipped, 1)

    def test_unparsed_values_become_warnings(self):
        text = "query,clicks,impressions,ctr,position\nfree icons,много,49369,0.07,5.0\n"
        result = load_export(self.write("garbage.csv", text))
        self.assertEqual(result.queries[0].clicks, 0)
        self.assertTrue(any("clicks" in warning for warning in result.warnings))

    def test_ctr_given_as_percent_number(self):
        text = "query,clicks,impressions,ctr,position\nfree icons,3790,49369,7.68,5.0\n"
        result = load_export(self.write("pctnum.csv", text))
        self.assertAlmostEqual(result.queries[0].ctr, 0.0768)

    def test_zero_position_means_unknown(self):
        text = "query,clicks,impressions,ctr,position\nfree icons,3790,49369,0.07,0\n"
        result = load_export(self.write("zeropos.csv", text))
        self.assertIsNone(result.queries[0].position)

    def test_empty_file_is_fatal(self):
        with self.assertRaises(InputError):
            load_export(self.write("empty.csv", ""))

    def test_missing_file_is_fatal(self):
        with self.assertRaises(InputError):
            load_export(self.tmp / "nope.csv")

    def test_large_export_is_read_whole(self):
        """Усечение — задача следующей стадии, загрузчик читает всё."""
        rows = "".join(
            f"query {i},{i},{i * 10},0.05,{i % 10 + 1}\n" for i in range(5000)
        )
        result = load_export(self.write("big.csv", "query,clicks,impressions,ctr,position\n" + rows))
        self.assertEqual(len(result.queries), 5000)


if __name__ == "__main__":
    unittest.main()
