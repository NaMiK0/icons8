"""Разбор чисел: главная причина падений на чужих выгрузках."""

import unittest

from seo_landings.ingest.numbers import parse_count, parse_number


class ParseCountTests(unittest.TestCase):
    """Клики и показы — целые счётчики."""

    def test_plain_integer(self):
        self.assertEqual(parse_count("26306"), (26306.0, True))

    def test_comma_as_thousands(self):
        self.assertEqual(parse_count("26,306"), (26306.0, True))

    def test_dot_as_thousands(self):
        """Европейская локаль пишет тысячи через точку."""
        self.assertEqual(parse_count("26.306"), (26306.0, True))

    def test_space_as_thousands(self):
        self.assertEqual(parse_count("26 306")[0], 26306.0)
        self.assertEqual(parse_count("26 306")[0], 26306.0)

    def test_millions(self):
        self.assertEqual(parse_count("1,234,567"), (1234567.0, True))


class ParseDecimalTests(unittest.TestCase):
    """CTR и позиция — дробные, точка у них всегда десятичная."""

    def test_ctr_must_not_become_thousands(self):
        """0.083 — это CTR 8.3%, а не 83. Регрессия на разделитель тысяч."""
        self.assertEqual(parse_number("0.083"), (0.083, True))

    def test_decimal_comma(self):
        self.assertEqual(parse_number("0,5495"), (0.5495, True))

    def test_percent_notation(self):
        self.assertEqual(parse_number("54.95%"), (0.5495, True))

    def test_percent_with_decimal_comma(self):
        self.assertEqual(parse_number("54,95%"), (0.5495, True))

    def test_position(self):
        self.assertEqual(parse_number("4.1"), (4.1, True))

    def test_mixed_separators_right_one_wins(self):
        self.assertEqual(parse_number("26,306.5"), (26306.5, True))
        self.assertEqual(parse_number("1.234,56"), (1234.56, True))


class DegradationTests(unittest.TestCase):
    """Пустое и мусорное значение не должны ронять прогон."""

    def test_blanks_are_zero(self):
        for blank in ("", " ", "-", "—", "n/a", "null"):
            with self.subTest(blank=blank):
                self.assertEqual(parse_number(blank), (0.0, True))

    def test_none_is_zero(self):
        self.assertEqual(parse_number(None), (0.0, True))

    def test_garbage_is_reported(self):
        value, ok = parse_number("много")
        self.assertEqual(value, 0.0)
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
