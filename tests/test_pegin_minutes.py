"""Minute offsets select the same persisted window evidence as legacy hours."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from liquid_tracer.common import save_json
from liquid_tracer.pegin_matching import HISTORY, match_pegin
from tests.test_pegin_matching import BTC, CLAIM, bitcoin
from tests.test_pegin_window import Chain, EPOCH, block_hash


class MinuteWindowIntegrationTests(unittest.TestCase):
    def test_negative_minutes_bound_discovery_and_cached_claim_verification(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            chain = Chain(300)
            chain.add(150, CLAIM)
            fixture = {"/blocks": chain.response("/blocks"),
                       "/block/" + block_hash(300) + "/status": {"in_best_chain": True}}
            fixture.update({"/blocks/" + str(height): chain.response("/blocks/" + str(height))
                            for height in chain.headers})
            fixture.update({"/block/" + block_hash(150) + "/txs": chain.response("/block/" + block_hash(150) + "/txs"),
                            "/tx/" + CLAIM: chain.response("/tx/" + CLAIM), HISTORY + "/" + CLAIM: []})
            save_json(root / "liquid.json", fixture)
            save_json(root / "bitcoin.json", {"/tx/" + BTC: bitcoin()})
            options = dict(workspace=root / "cases", bitcoin_fixture=root / "bitcoin.json",
                           liquid_fixture=root / "liquid.json", public=True)
            with patch("liquid_tracer.api.default_min_interval", return_value=.00001):
                self.assertEqual(match_pegin(None, BTC, liquid_txid=CLAIM, **options)["status"], "matched")
                outside = match_pegin(None, BTC, start_minutes=-5, end_minutes=15, **options)
                self.assertEqual(outside["status"], "no_match")
                self.assertTrue(outside["coverage"]["window"]["complete"])
                self.assertEqual(outside["coverage"]["window"]["start_time"], EPOCH - 300)
                self.assertEqual(outside["coverage"]["window"]["end_time"], EPOCH + 900)
                inside = match_pegin(None, BTC, start_minutes=-5, end_minutes=180, **options)
                self.assertEqual(inside["status"], "matched")
                self.assertEqual(inside["matches"][0]["elapsed_seconds"], 9000)
                self.assertEqual(inside["query"]["start_minutes"], -5)
                self.assertEqual(inside["query"]["end_minutes"], 180)
                self.assertNotIn("start_hours", inside["query"])


if __name__ == "__main__":
    unittest.main()
