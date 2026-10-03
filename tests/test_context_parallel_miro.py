"""Repeated-input display bundles migrate without replacing address shapes."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from liquid_tracer import board_layout, context_parallel_miro
from liquid_tracer.common import TraceError, canonical, digest, read_json, save_json
from liquid_tracer.context_connectors import prepare
from liquid_tracer.context_groups import group_context_inputs
from liquid_tracer.export import build_graph
from liquid_tracer.miro import make_plan, sync, validate_plan
from tests.test_board_projection_safety import scope_plan
from tests.test_context_parallel_connectors import repeated_state
from tests.test_dense_context_miro import DenseMiro
from tests.test_input_order import child_input
from tests.test_layout import txid


def graphs(count=5):
    bundled = build_graph(repeated_state(count), group_context_inputs=True)
    bundled.pop("activity_frames", None)
    bundled["run"]["ancestor_runs"] = []
    plain = copy.deepcopy(bundled)
    plain.pop("context_connectors")
    return plain, bundled


def checksum(plan):
    plan["sha256"] = digest(canonical({key: value for key, value in plan.items() if key != "sha256"}))
    return plan


class ContextParallelMiroTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "miro.json"
        self.remote = DenseMiro()
        self.plain_graph, self.bundled_graph = graphs()
        self.plain, self.bundled = map(make_plan, (self.plain_graph, self.bundled_graph))
        self.key, = self.bundled["context_parallel_items"]
        self.proof = self.bundled["context_parallel_items"][self.key]
        self.address = self.proof["source"]

    def sync(self, plan, **kwargs):
        kwargs.setdefault("reorganize", not bool(plan.get("board_layout")))
        return sync(plan, "test-board", self.path, token="test-token", interval=0,
                    transport=self.remote, **kwargs)

    def item(self, key):
        return self.remote.items[read_json(self.path)["items"][key]["id"]]

    def assert_clean(self, plan):
        mapping = read_json(self.path)["items"]
        self.assertEqual(set(mapping), {item["key"] for item in plan["shapes"] + plan["connectors"]})
        self.assertEqual(set(self.remote.items), {record["id"] for record in mapping.values()})
        for key, record in mapping.items():
            if record["endpoint"] == "connectors":
                self.assertEqual(self.item(key)["startItem"]["id"], mapping[record["source"]]["id"])
                self.assertEqual(self.item(key)["endItem"]["id"], mapping[record["target"]]["id"])

    def test_summary_proves_canonical_inputs_with_no_fake_utxo(self):
        validate_plan(self.bundled)
        self.assertEqual(self.bundled_graph["edges"], self.plain_graph["edges"])
        self.assertEqual(set(self.proof["inputs"]), {child_input(i) for i in range(4)})
        edge = next(item for item in self.bundled["connectors"] if item["key"] == self.key)
        self.assertNotIn("context_evidence", edge)
        self.assertEqual(edge["body"]["captions"], [{"content": "4 context inputs · 1 address", "position": "50%"}])
        self.assertEqual(edge["context_parallel_evidence"]["member_edge_ids"], sorted(self.proof["inputs"]))
        self.assertIn(child_input(4), {item["key"] for item in self.bundled["connectors"]})
        self.assertEqual([item for item in self.bundled["shapes"] if not item["key"].startswith("legend")],
                         [item for item in self.plain["shapes"] if not item["key"].startswith("legend")])

    def test_isolated_group_and_parallel_bundle_migrate_in_both_directions(self):
        state = repeated_state(6, continuing=())
        state["transactions"][txid("input-order-child")]["data"]["vin"][5]["prevout"]["scriptpubkey_address"] = "SYNTHETIC-second-context"
        plain = build_graph(state)
        plain.pop("activity_frames", None)
        plain["run"]["ancestor_runs"] = []
        grouped = make_plan(group_context_inputs(plain, enabled=True))
        self.assertTrue(grouped["context_group_items"])
        next(edge for edge in plain["edges"] if edge["id"] == child_input(0))["role"] = "traced_input"
        parallel = make_plan(group_context_inputs(plain, enabled=True))
        self.assertTrue(parallel["context_parallel_items"])
        self.assertFalse(parallel["context_group_items"])
        for plan in (grouped, parallel, grouped, parallel):
            self.sync(plan)
            self.assert_clean(plan)

    def test_reversible_migration_preserves_every_shape_and_continuation_id(self):
        self.sync(self.plain)
        mapping = read_json(self.path)["items"]
        preserved = {key: record["id"] for key, record in mapping.items()
                     if record["endpoint"] == "shapes" or key == child_input(4)}
        for plan in (self.bundled, self.plain, self.bundled):
            self.sync(plan)
            self.assert_clean(plan)
            for key, item_id in preserved.items():
                self.assertEqual(self.item(key)["id"], item_id)
            writes = len(self.remote.writes)
            self.sync(plan)
            self.assertEqual(len(self.remote.writes), writes)
        self.assertEqual(read_json(self.path)["items"][self.key]["context_parallel_proof"], self.proof)
        self.assertTrue(all(url.rsplit("/", 1)[-1] not in preserved.values()
                            for method, url, _ in self.remote.writes if method == "DELETE"))

    def test_legacy_individual_mapping_without_evidence_remains_migratable(self):
        self.sync(self.plain)
        state = read_json(self.path)
        for record in state["items"].values():
            record.pop("context_evidence", None)
        save_json(self.path, state)
        self.sync(self.bundled)
        self.assert_clean(self.bundled)

    def test_membership_growth_replaces_only_bundle_and_updates_proof(self):
        self.sync(self.bundled)
        previous, address = self.item(self.key)["id"], self.item(self.address)["id"]
        plan = make_plan(graphs(6)[1])
        self.sync(plan)
        self.assert_clean(plan)
        self.assertNotEqual(self.item(self.key)["id"], previous)
        self.assertEqual(self.item(self.address)["id"], address)
        self.assertEqual(len(read_json(self.path)["items"][self.key]["context_parallel_proof"]["inputs"]), 5)

    def test_promotion_to_traced_input_retains_original_outpoint_and_address(self):
        self.sync(self.bundled)
        address = self.item(self.address)["id"]
        graph = copy.deepcopy(self.bundled_graph)
        graph.pop("context_connectors")
        next(edge for edge in graph["edges"] if edge["id"] == child_input(0))["role"] = "traced_input"
        plan = make_plan(prepare(graph))
        self.sync(plan)
        self.assert_clean(plan)
        self.assertEqual(self.item(self.address)["id"], address)
        self.assertNotIn(child_input(0), plan["context_parallel_items"][self.key]["inputs"])
        edge = next(item for item in plan["connectors"] if item["key"] == child_input(0))
        self.assertEqual(edge["context_evidence"]["outpoint"], self.proof["inputs"][child_input(0)]["outpoint"])
        self.assertEqual(edge["context_evidence"]["role"], "traced_input")

    def test_former_traced_input_can_bundle_after_ordinary_sync_demotes_its_role(self):
        traced = copy.deepcopy(self.plain_graph)
        next(edge for edge in traced["edges"] if edge["id"] == child_input(0))["role"] = "traced_input"
        self.sync(make_plan(traced))
        self.sync(self.plain)
        # Creation evidence retains the old role even though current generated
        # styling has already acknowledged the ordinary input's demotion.
        self.assertEqual(read_json(self.path)["items"][child_input(0)]["context_evidence"]["role"], "traced_input")
        address = self.item(self.address)["id"]
        self.sync(self.bundled)
        self.assert_clean(self.bundled)
        self.assertEqual(self.item(self.address)["id"], address)

    def test_former_traced_input_still_cannot_bundle_with_a_changed_outpoint(self):
        traced = copy.deepcopy(self.plain_graph)
        next(edge for edge in traced["edges"] if edge["id"] == child_input(0))["role"] = "traced_input"
        self.sync(make_plan(traced))
        self.sync(self.plain)
        plan = copy.deepcopy(self.bundled)
        plan["context_parallel_items"][self.key]["inputs"][child_input(0)]["outpoint"] = "other:0"
        writes = len(self.remote.writes)
        with self.assertRaisesRegex(TraceError, "original input evidence"):
            self.sync(checksum(plan))
        self.assertEqual(len(self.remote.writes), writes)

    def test_manual_notes_and_reattachment_block_before_writes(self):
        for initial, final, key in ((self.plain, self.bundled, child_input(0)),
                                    (self.bundled, self.plain, self.key)):
            self.sync(initial)
            for change in ("note", "attachment"):
                original = copy.deepcopy(self.item(key))
                if change == "note":
                    self.item(key)["captions"] = [{"content": "Keep my note", "position": "50%"}]
                else:
                    self.item(key)["startItem"]["id"] = self.item(self.proof["target"])["id"]
                writes = len(self.remote.writes)
                with self.assertRaises(TraceError):
                    self.sync(final)
                self.assertEqual(len(self.remote.writes), writes)
                self.remote.items[original["id"]] = original

    def test_uncaptured_membership_omission_is_not_authorized_by_projection(self):
        self.sync(scope_plan(self.bundled))
        graph = copy.deepcopy(self.bundled_graph)
        graph.pop("context_connectors")
        graph["edges"] = [edge for edge in graph["edges"] if edge["id"] != child_input(0)]
        writes = len(self.remote.writes)
        with self.assertRaisesRegex(TraceError, "original input evidence"):
            self.sync(scope_plan(make_plan(prepare(graph))))
        self.assertEqual(len(self.remote.writes), writes)

    def test_board_aware_shrink_preserves_manual_address_position_and_reports_replacement(self):
        initial = scope_plan(self.bundled)
        self.sync(initial)
        address_id = self.item(self.address)["id"]
        self.item(self.address)["position"].update(x=-500, y=1234)
        position = copy.deepcopy(self.item(self.address)["position"])
        graph = copy.deepcopy(self.bundled_graph)
        graph.pop("context_connectors")
        graph["edges"] = [edge for edge in graph["edges"] if edge["id"] != child_input(0)]
        graph = prepare(graph)
        snapshot = board_layout.capture("test-board", self.path, initial["namespace"],
                                       token="test-token", transport=self.remote, interval=0)
        with patch("liquid_tracer.elk_layout.optimize_graph", side_effect=AssertionError("No new shapes need ELK")):
            prepared = board_layout.prepare_graph(graph, snapshot)
        self.assertEqual(prepared["board_layout"]["counts"]["new_nodes"], 0)
        self.assertEqual(prepared["board_layout"]["counts"]["new_connectors"], 1)
        self.assertEqual(prepared["board_layout"]["counts"]["removed_connectors"], 1)
        plan = scope_plan(make_plan(prepared))
        self.sync(plan)
        self.assert_clean(plan)
        self.assertEqual(self.item(self.address)["id"], address_id)
        self.assertEqual(self.item(self.address)["position"], position)

    def test_changed_input_evidence_blocks_expansion_and_individual_aggregation(self):
        for initial, final, key in ((self.bundled, self.plain, child_input(0)),
                                    (self.plain, self.bundled, self.key)):
            self.sync(initial)
            plan = copy.deepcopy(final)
            if key == self.key:
                plan["context_parallel_items"][self.key]["inputs"][child_input(0)]["outpoint"] = "other:0"
            else:
                next(item for item in plan["connectors"] if item["key"] == key)["context_evidence"]["outpoint"] = "other:0"
            writes = len(self.remote.writes)
            with self.assertRaisesRegex(TraceError, "original input evidence"):
                self.sync(checksum(plan))
            self.assertEqual(len(self.remote.writes), writes)

    def test_forged_or_incomplete_proofs_fail_closed(self):
        for change in ("missing", "key", "role", "member", "duplicate", "fake_utxo", "shape", "version"):
            with self.subTest(change=change):
                plan = copy.deepcopy(self.bundled)
                proof = plan["context_parallel_items"][self.key]
                edge = next(item for item in plan["connectors"] if item["key"] == self.key)
                if change == "missing":
                    plan.pop("context_parallel_items")
                elif change == "key":
                    proof["key"] += "x"
                elif change == "role":
                    proof["inputs"][child_input(0)]["role"] = "traced_input"
                elif change == "member":
                    edge["context_parallel_evidence"]["member_edge_ids"].pop()
                elif change == "duplicate":
                    plan["connectors"].append(copy.deepcopy(next(item for item in self.plain["connectors"] if item["key"] == child_input(0))))
                elif change == "fake_utxo":
                    edge["context_evidence"] = copy.deepcopy(self.proof["inputs"][child_input(0)])
                elif change == "shape":
                    next(item for item in plan["shapes"] if item["key"] == self.address)["body"]["data"]["shape"] = "rectangle"
                else:
                    proof["version"] = True
                with self.assertRaises(TraceError):
                    validate_plan(checksum(plan))

    def test_saved_bundle_proof_tampering_blocks_all_writes(self):
        self.sync(self.bundled)
        state = read_json(self.path)
        state["items"][self.key]["context_parallel_proof"]["inputs"][child_input(0)]["outpoint"] = "forged:0"
        save_json(self.path, state)
        writes = len(self.remote.writes)
        with self.assertRaisesRegex(TraceError, "original input evidence"):
            self.sync(self.plain)
        self.assertEqual(len(self.remote.writes), writes)

    def test_restoration_rejects_physical_endpoint_that_disagrees_with_input_evidence(self):
        self.sync(self.bundled)
        plan = copy.deepcopy(self.plain)
        edge = next(item for item in plan["connectors"] if item["key"] == child_input(0))
        edge["source"] = next(item["key"] for item in plan["shapes"]
                              if item["key"] != self.address and item["body"]["data"]["shape"] == "circle")
        writes = len(self.remote.writes)
        with self.assertRaisesRegex(TraceError, "original address"):
            self.sync(checksum(plan))
        self.assertEqual(len(self.remote.writes), writes)

    def test_lost_delete_retries_same_plan_and_never_deletes_address(self):
        for before, after in ((self.plain, self.bundled), (self.bundled, self.plain)):
            self.sync(before)
            address = self.item(self.address)["id"]
            self.remote.lose_delete = True
            with self.assertRaisesRegex(TraceError, "lost"):
                self.sync(after)
            self.assertTrue(read_json(self.path)["pending_deletions"])
            writes = len(self.remote.writes)
            with self.assertRaisesRegex(TraceError, "same plan"):
                self.sync(before)
            self.assertEqual(len(self.remote.writes), writes)
            self.sync(after)
            self.assert_clean(after)
            self.assertEqual(self.item(self.address)["id"], address)

    def test_cancel_after_journaling_resumes_without_duplicated_connectors(self):
        self.sync(self.plain)
        writes = len(self.remote.writes)
        def cancel(event):
            if event["phase"] == "removing":
                raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self.sync(self.bundled, progress=cancel)
        self.assertEqual(len(self.remote.writes), writes)
        self.assertTrue(read_json(self.path)["pending_deletions"])
        self.sync(self.bundled)
        self.assert_clean(self.bundled)

    def test_lost_bundle_creation_retains_proof_and_is_never_replayed(self):
        self.sync(self.plain)
        original = self.remote
        def lose(method, url, headers, body, timeout):
            if method == "POST" and url.endswith("/connectors"):
                original.lose_next_post = True
            return original(method, url, headers, body, timeout)
        self.remote = lose
        with self.assertRaisesRegex(TraceError, "lost|uncertain"):
            self.sync(self.bundled)
        self.remote = original
        pending = read_json(self.path)["pending_creations"][self.key]
        self.assertEqual(pending["context_parallel_proof"], self.proof)
        writes = len(original.writes)
        with self.assertRaisesRegex(TraceError, "uncertain"):
            self.sync(self.bundled)
        self.assertEqual(len(original.writes), writes)

    def test_rejected_creation_can_retry_without_replacing_address(self):
        self.sync(self.plain)
        original, address = self.remote, self.item(self.address)["id"]
        def reject(method, url, headers, body, timeout):
            if method == "POST" and url.endswith("/connectors"):
                return 400, {}, b'{"message":"Synthetic connector rejection"}'
            return original(method, url, headers, body, timeout)
        self.remote = reject
        with self.assertRaisesRegex(TraceError, "HTTP 400"):
            self.sync(self.bundled)
        self.remote = original
        self.assertFalse(read_json(self.path)["pending_creations"])
        self.sync(self.bundled)
        self.assert_clean(self.bundled)
        self.assertEqual(self.item(self.address)["id"], address)


if __name__ == "__main__":
    unittest.main()
