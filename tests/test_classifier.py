"""Unit tests for the prompt -> trigger classifier.

Uses only stdlib (unittest) so it runs without adding a test dependency.
Run with:  python -m unittest tests.test_classifier
"""
import unittest

from alarm_mcp.classifier import classify, suggest_poll_seconds


class TestClassifier(unittest.TestCase):
    def test_cricket_player_batting(self):
        t = classify("wake me up when Rishabh Pant comes to bat")
        self.assertEqual(t.category, "cricket")
        self.assertEqual(t.source_hint, "cricket")
        self.assertTrue(t.supported)
        self.assertEqual(t.params["event"], "batsman_arrives")
        self.assertEqual(t.params["player"], "Rishabh Pant")

    def test_cricket_wicket(self):
        t = classify("alert me when Virat Kohli loses his wicket")
        self.assertEqual(t.category, "cricket")
        self.assertEqual(t.params["event"], "wicket")
        self.assertIn("Virat Kohli", t.params["all_players"])

    def test_price_btc_below(self):
        t = classify("wake me up when BTC falls below 50k")
        self.assertEqual(t.category, "price")
        self.assertEqual(t.source_hint, "price:bitcoin")
        self.assertEqual(t.params["asset"], "bitcoin")
        self.assertEqual(t.params["direction"], "below")
        self.assertEqual(t.params["threshold_usd"], 50_000)

    def test_price_eth_above_dollars(self):
        t = classify("alert me when ETH goes above $4,000")
        self.assertEqual(t.category, "price")
        self.assertEqual(t.params["asset"], "ethereum")
        self.assertEqual(t.params["direction"], "above")
        self.assertEqual(t.params["threshold_usd"], 4_000)

    def test_live_event_trump(self):
        t = classify("wake me up when Trump starts speaking tonight")
        self.assertEqual(t.category, "live_event")
        self.assertEqual(t.source_hint, "news")
        self.assertTrue(t.supported)
        self.assertEqual(t.params["subject"], "Trump")

    def test_unknown_falls_through(self):
        t = classify("notify me when my laundry is done")
        self.assertEqual(t.category, "unknown")
        self.assertFalse(t.supported)
        self.assertIsNone(t.source_hint)

    def test_empty_prompt(self):
        t = classify("   ")
        self.assertEqual(t.category, "unknown")
        self.assertFalse(t.supported)

    def test_suggest_poll_seconds(self):
        self.assertLess(
            suggest_poll_seconds(classify("Rishabh Pant comes to bat")),
            suggest_poll_seconds(classify("BTC drops below 30k")),
        )
        self.assertLess(
            suggest_poll_seconds(classify("BTC drops below 30k")),
            suggest_poll_seconds(classify("Trump speaks live")),
        )


if __name__ == "__main__":
    unittest.main()
