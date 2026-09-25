"""Потенциал роста: почему запрос из топ-3 не заслуживает нового лендинга."""

import unittest

from seo_landings.domain import scoring


class OpportunityTests(unittest.TestCase):
    def test_top_three_has_no_room(self):
        self.assertEqual(scoring.opportunity(1.0), 0.0)
        self.assertEqual(scoring.opportunity(3.0), 0.0)

    def test_deep_positions_are_maximal(self):
        self.assertEqual(scoring.opportunity(10.0), 1.0)
        self.assertEqual(scoring.opportunity(42.0), 1.0)

    def test_middle_is_proportional(self):
        self.assertAlmostEqual(scoring.opportunity(6.5), 0.5)

    def test_unknown_position_is_neutral(self):
        self.assertEqual(scoring.opportunity(None), scoring.UNKNOWN_OPPORTUNITY)


class PotentialTests(unittest.TestCase):
    def test_brand_query_scores_zero(self):
        """icons8: 47876 показов, позиция 1.4 — расти некуда."""
        self.assertEqual(scoring.potential(47876, 1.4), 0.0)

    def test_deep_query_with_many_impressions_wins(self):
        """custom cursor: 299291 показов, позиция 5.2."""
        brand = scoring.potential(47876, 1.4)
        cursor = scoring.potential(299291, 5.2)
        self.assertGreater(cursor, brand)
        self.assertAlmostEqual(cursor, 299291 * (5.2 - 3.0) / 7.0)


if __name__ == "__main__":
    unittest.main()
