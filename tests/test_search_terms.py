import unittest

from app import parse_search_terms


class SearchTermParsingTests(unittest.TestCase):
    def test_supports_keywords_and_quoted_phrases(self):
        phrases, keywords = parse_search_terms('warlock, titan hunter "sweet business" "mask of the quiet one"')
        self.assertEqual(phrases, ["sweet business", "mask of the quiet one"])
        self.assertEqual(keywords, ["warlock", "titan", "hunter"])


if __name__ == "__main__":
    unittest.main()
