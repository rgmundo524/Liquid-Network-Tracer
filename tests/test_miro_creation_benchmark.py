import copy
import types
import unittest

from liquid_tracer import miro
from scripts.benchmark_miro_creation import SyntheticMiro, encoded, measure, normalized_board_and_mapping


class MiroCreationBenchmarkTests(unittest.TestCase):
    def test_initial_publication_benchmark_verifies_bulk_mapping_and_durable_retry(self):
        options = types.SimpleNamespace(transactions=4, latency=0, interval=0, unpaced_credit=True)
        serial = measure(miro, options, 1)
        parallel = measure(miro, options, 4)
        self.assertEqual(serial["native_items"], 19)
        self.assertEqual(serial["shape_post_requests"], 1)
        self.assertEqual(serial["connector_post_requests"], 8)
        for field in ("board_sha256", "mapping_sha256", "request_counts"):
            self.assertEqual(serial[field], parallel[field])
        self.assertTrue(parallel["durable_state_complete"])
        self.assertTrue(parallel["retry_reuses_ids"])
        self.assertTrue(parallel["manual_edit_preserved"])

    def test_synthetic_bulk_is_unordered_and_logical_comparison_rejects_duplicate_ids(self):
        board = SyntheticMiro(pace_credits=False)
        bodies = [{"type": "shape", "data": {"shape": "rectangle", "content": label},
                   "position": {"x": x, "y": 0}} for x, label in enumerate(("first", "second"))]
        status, _, raw = board("POST", "https://api.miro.com/v2/boards/synthetic-benchmark/items/bulk",
                              {}, encoded(bodies), 30)
        import json
        returned = json.loads(raw)["data"]
        self.assertEqual(status, 201)
        self.assertEqual([item["data"]["content"] for item in returned], ["second", "first"])
        mapping = {item["data"]["content"]: {"id": item["id"]} for item in returned}
        expected_board, expected_mapping = normalized_board_and_mapping(board, {"items": mapping})
        remapped = SyntheticMiro(pace_credits=False)
        remapped.items = {"other-" + key: {**copy.deepcopy(value), "id": "other-" + key}
                          for key, value in board.items.items()}
        remapped_mapping = {key: {"id": "other-" + value["id"]} for key, value in mapping.items()}
        self.assertEqual(normalized_board_and_mapping(remapped, {"items": remapped_mapping}),
                         (expected_board, expected_mapping))
        remapped_mapping["first"]["id"] = remapped_mapping["second"]["id"]
        with self.assertRaisesRegex(AssertionError, "incomplete or contains duplicate"):
            normalized_board_and_mapping(remapped, {"items": remapped_mapping})


if __name__ == "__main__":
    unittest.main()
