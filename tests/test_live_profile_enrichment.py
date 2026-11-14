"""The search-time profile enrichment joins profile API payloads to one host."""

import unittest

from scraper import CouchsurfingScraper


class LiveProfileEnrichmentTests(unittest.TestCase):
    def test_profile_api_merge_collects_sections_badges_and_paginated_references(self):
        search_card = {
            "aboutMe": "Search-card bio",
            "badges": [{"name": "Search badge"}],
            "references": [{"id": "ref-1", "text": "First page"}],
        }
        profile_response = {
            "data": {
                "profile": {
                    "handle": "alice",
                    "aboutMe": "Full profile biography",
                    "houseRules": "No shoes inside",
                    "sharedInformation": "Neighborhood information",
                    "whyOnCouchsurfing": "Cultural exchange",
                    "badges": [{"name": "Local guide"}],
                    "references": [{"id": "ref-2", "text": "Second page"}],
                },
                "recommendations": [{"handle": "bob", "aboutMe": "Do not merge this profile"}],
            }
        }

        merged = CouchsurfingScraper._merge_profile_payloads(
            search_card, [profile_response], "alice",
        )

        self.assertEqual(merged["aboutMe"], "Full profile biography")
        self.assertEqual(merged["houseRules"], "No shoes inside")
        self.assertEqual(merged["sharedInformation"], "Neighborhood information")
        self.assertEqual(merged["whyOnCouchsurfing"], "Cultural exchange")
        self.assertEqual({badge["name"] for badge in merged["badges"]}, {"Search badge", "Local guide"})
        self.assertEqual({reference["id"] for reference in merged["references"]}, {"ref-1", "ref-2"})
        self.assertNotEqual(merged.get("aboutMe"), "Do not merge this profile")


if __name__ == "__main__":
    unittest.main()
