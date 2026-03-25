import unittest

from matcher import normalize_line, similarity_score


class MatchingTests(unittest.TestCase):
    def test_normalize_line(self):
        self.assertEqual(normalize_line("  Fusion-Rifle Damage +20%! "), "fusionrifle damage 20")

    def test_fuzzy_match_is_high_for_similar_phrasing(self):
        a = normalize_line("Increased hand cannon ADS speed by 10%.")
        b = normalize_line("Hand cannons now aim down sights 10 percent faster")
        self.assertGreaterEqual(similarity_score(a, b), 40)


if __name__ == "__main__":
    unittest.main()
