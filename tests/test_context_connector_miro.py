"""Aggregate display lines preserve evidence across reversible fake-board sync."""

import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from liquid_tracer import board_layout
from liquid_tracer.common import TraceError, canonical, digest, read_json
from liquid_tracer.context_groups import group_context_inputs
from liquid_tracer.export import build_graph
from liquid_tracer.miro import make_plan, sync, validate_plan
from tests.test_board_projection_safety import scope_plan
from tests.test_dense_context_miro import DenseMiro
from tests.test_input_order import child_input, input_order_state


def graphs(count=9, *, continuing=None):
    plain = build_graph(input_order_state(count + 1, continuing=(count if continuing is None else continuing,)))
    plain.pop("activity_frames", None)
    plain["run"]["ancestor_runs"] = []
    aggregate = group_context_inputs(plain, enabled=True)
    individual = copy.deepcopy(aggregate)
    individual.pop("context_connectors")
    return plain, individual, aggregate


def checksum(plan):
    plan["sha256"] = digest(canonical({key: value for key, value in plan.items() if key != "sha256"}))
    return plan


class ContextConnectorMiroTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "miro.json"
        self.remote = DenseMiro()
        self.graphs = graphs()
        self.plain, self.individual, self.aggregate = map(make_plan, self.graphs)
        self.group, = self.aggregate["context_group_items"]
        self.proof = self.aggregate["context_group_items"][self.group]
        self.summary, = self.proof["display_inputs"]

    def sync(self, plan, **options):
        options.setdefault("reorganize", not bool(plan.get("board_layout")))
        return sync(plan, "test-board", self.path, token="test-token", interval=0,
                    transport=self.remote, **options)

    def item(self, key):
        return self.remote.items[read_json(self.path)["items"][key]["id"]]

    def assert_no_stale_items(self, plan):
        mapping = read_json(self.path)["items"]
        self.assertEqual(set(mapping), {item["key"] for item in plan["shapes"] + plan["connectors"]})
        self.assertEqual(set(self.remote.items), {record["id"] for record in mapping.values()})
        for key, record in mapping.items():
            if record["endpoint"] == "connectors":
                self.assertEqual(self.item(key)["startItem"]["id"], mapping[record["source"]]["id"])
                self.assertEqual(self.item(key)["endItem"]["id"], mapping[record["target"]]["id"])

    def test_one_display_line_has_complete_canonical_proof_and_no_fake_utxo(self):
        validate_plan(self.aggregate)
        validate_plan(self.individual)
        self.assertEqual(self.proof["version"], 2)
        self.assertEqual(self.individual["context_group_items"][self.group]["version"], 1)
        self.assertEqual(self.proof["inputs"], self.individual["context_group_items"][self.group]["inputs"])
        self.assertEqual(self.proof["display_inputs"], {self.summary: sorted(self.proof["inputs"])})
        lines = [item for item in self.aggregate["connectors"] if item["source"] == self.group]
        self.assertEqual(len(lines), 1)
        self.assertNotIn("context_evidence", lines[0])
        self.assertEqual(lines[0]["context_display_evidence"], {
            "version": 1, "group": self.group, "target": self.proof["target"],
            "member_edge_ids": sorted(self.proof["inputs"])})
        self.assertEqual(lines[0]["body"]["captions"], [
            {"content": "9 context inputs · 9 addresses", "position": "50%"}])
        self.assertEqual(len(self.graphs[2]["edges"]), len(self.graphs[0]["edges"]))

    def test_changed_outpoint_blocks_initial_grouping_before_any_write(self):
        self.sync(self.plain)
        before, writes = copy.deepcopy(self.remote.items), len(self.remote.writes)
        for original in (self.individual, self.aggregate):
            with self.subTest(version=original["context_group_items"][self.group]["version"]):
                plan = copy.deepcopy(original)
                expected = plan["context_group_items"][self.group]["inputs"][child_input(0)]
                expected["outpoint"] = "changed:0"
                for connector in plan["connectors"]:
                    if connector["key"] == child_input(0):
                        connector["context_evidence"] = copy.deepcopy(expected)
                checksum(plan)
                validate_plan(plan)  # Internally consistent, but not the saved UTXO.
                with self.assertRaisesRegex(TraceError, "unproven Miro input"):
                    self.sync(plan)
                self.assertEqual(self.remote.items, before)
                self.assertEqual(len(self.remote.writes), writes)

    def test_former_traced_input_can_group_after_ordinary_sync_demotes_role(self):
        traced = copy.deepcopy(self.graphs[0])
        next(edge for edge in traced["edges"] if edge["id"] == child_input(0))["role"] = "traced_input"
        self.sync(make_plan(traced))
        self.sync(self.plain)
        self.assertEqual(read_json(self.path)["items"][child_input(0)]["context_evidence"]["role"], "traced_input")
        self.sync(self.aggregate)
        self.assert_no_stale_items(self.aggregate)

    def test_legacy_aggregate_and_plain_are_reversible_without_stale_lines(self):
        untouched = child_input(9)
        self.sync(self.individual)
        original_id = self.item(untouched)["id"]
        for plan in (self.aggregate, self.individual, self.aggregate, self.plain, self.aggregate):
            with self.subTest(summary=self.summary in {item["key"] for item in plan["connectors"]}):
                self.sync(plan)
                self.assert_no_stale_items(plan)
                self.assertEqual(self.item(untouched)["id"], original_id)
                writes = len(self.remote.writes)
                self.sync(plan)
                self.assertEqual(len(self.remote.writes), writes)

    def test_251_input_first_publication_has_only_one_context_line(self):
        plain, _, aggregate = graphs(251)
        plan = make_plan(aggregate)
        self.sync(plan)
        self.assert_no_stale_items(plan)
        self.assertEqual(len(plan["connectors"]), len(plain["edges"]) - 250)
        self.sync(make_plan(plain))
        self.assert_no_stale_items(make_plan(plain))

    def test_migration_requires_explicit_reorganization(self):
        self.sync(self.individual)
        writes = len(self.remote.writes)
        with self.assertRaisesRegex(TraceError, "Sync and reorganize"):
            sync(self.aggregate, "test-board", self.path, token="test-token", interval=0,
                 transport=self.remote, reorganize=False)
        self.assertEqual(writes, len(self.remote.writes))

    def test_manual_legacy_input_note_blocks_aggregation_before_any_write(self):
        self.sync(self.individual)
        self.item(child_input(0))["captions"] = [{"content": "Investigator note", "position": "50%"}]
        before, writes = copy.deepcopy(self.remote.items), len(self.remote.writes)
        with self.assertRaisesRegex(TraceError, "manual edits"):
            self.sync(self.aggregate)
        self.assertEqual(self.remote.items, before)
        self.assertEqual(len(self.remote.writes), writes)

    def test_manual_aggregate_note_blocks_both_expansion_modes(self):
        self.sync(self.aggregate)
        self.item(self.summary)["captions"] = [{"content": "Investigator note", "position": "50%"}]
        before, writes = copy.deepcopy(self.remote.items), len(self.remote.writes)
        for plan in (self.individual, self.plain):
            with self.subTest(grouped=bool(plan["context_group_items"])):
                with self.assertRaisesRegex(TraceError, "manual edits"):
                    self.sync(plan)
                self.assertEqual(self.remote.items, before)
                self.assertEqual(len(self.remote.writes), writes)

    def test_unmanaged_attachment_and_summary_notes_block_migration(self):
        self.sync(self.individual)
        summary = self.item(self.group)
        summary["data"]["content"] += "<p>Investigator note</p>"
        writes = len(self.remote.writes)
        with self.assertRaisesRegex(TraceError, "manual edits"):
            self.sync(self.aggregate)
        summary["data"]["content"] = self.individual["shapes"][[
            item["key"] for item in self.individual["shapes"]].index(self.group)]["body"]["data"]["content"]
        self.remote.items["manual-line"] = {"id": "manual-line", "type": "connector",
            "startItem": {"id": summary["id"]}, "endItem": {"id": self.item(self.proof["target"])["id"]}}
        with self.assertRaisesRegex(TraceError, "connector attaches"):
            self.sync(self.aggregate)
        self.assertEqual(len(self.remote.writes), writes)

    def test_lost_delete_resumes_only_same_migration_without_stale_lines(self):
        for before, after in ((self.individual, self.aggregate), (self.aggregate, self.plain)):
            self.sync(before)
            self.remote.lose_delete = True
            with self.assertRaisesRegex(TraceError, "lost"):
                self.sync(after)
            self.assertTrue(read_json(self.path)["pending_deletions"])
            writes = len(self.remote.writes)
            with self.assertRaisesRegex(TraceError, "same plan"):
                self.sync(before)
            self.assertEqual(len(self.remote.writes), writes)
            self.sync(after)
            self.assert_no_stale_items(after)
            self.assertFalse(read_json(self.path)["pending_deletions"])

    def test_uncertain_aggregate_post_is_never_replayed(self):
        self.sync(self.individual)
        original = self.remote

        def lose_connector(method, url, headers, body, timeout):
            if method == "POST" and url.endswith("/connectors"):
                original.lose_next_post = True
            return original(method, url, headers, body, timeout)

        self.remote = lose_connector
        with self.assertRaisesRegex(TraceError, "lost|uncertain"):
            self.sync(self.aggregate)
        self.remote = original
        self.assertIn(self.summary, read_json(self.path)["pending_creations"])
        writes = len(original.writes)
        with self.assertRaisesRegex(TraceError, "uncertain"):
            self.sync(self.aggregate)
        self.assertEqual(len(original.writes), writes)

    def test_rejected_connector_post_resumes_after_summary_is_acknowledged(self):
        self.sync(self.individual)
        original = self.remote

        def reject_connector(method, url, headers, body, timeout):
            if method == "POST" and url.endswith("/connectors"):
                return 400, {}, b'{"message":"synthetic rejected connector"}'
            return original(method, url, headers, body, timeout)

        self.remote = reject_connector
        with self.assertRaisesRegex(TraceError, "HTTP 400"):
            self.sync(self.aggregate)
        self.remote = original
        partial = read_json(self.path)
        self.assertEqual(partial["items"][self.group]["context_group_proof"], self.proof)
        self.assertNotIn(self.summary, partial["items"])
        self.assertFalse(partial["pending_creations"])
        shape_id = self.item(self.group)["id"]
        self.sync(self.aggregate)
        self.assert_no_stale_items(self.aggregate)
        self.assertEqual(self.item(self.group)["id"], shape_id)

    def test_membership_expansion_replaces_same_logical_aggregate_with_fresh_proof(self):
        self.sync(self.aggregate)
        old_id = self.item(self.summary)["id"]
        _, _, expanded = graphs(10, continuing=9)
        plan = make_plan(expanded)
        self.sync(plan)
        self.assert_no_stale_items(plan)
        self.assertNotEqual(self.item(self.summary)["id"], old_id)
        self.assertEqual(len(read_json(self.path)["items"][self.group]["context_group_proof"]["inputs"]), 10)

    def test_forged_display_membership_missing_proof_or_fake_utxo_is_rejected(self):
        for change in ("proof", "connector", "fake_utxo", "legacy_version", "duplicate",
                       "duplicate_elsewhere", "unexpected_display_evidence", "missing"):
            with self.subTest(change=change):
                plan = copy.deepcopy(self.aggregate)
                proof = plan["context_group_items"][self.group]
                edge = next(item for item in plan["connectors"] if item["key"] == self.summary)
                if change == "proof":
                    proof["display_inputs"][self.summary].pop()
                elif change == "connector":
                    edge["context_display_evidence"]["member_edge_ids"].pop()
                elif change == "fake_utxo":
                    edge["context_evidence"] = copy.deepcopy(next(iter(proof["inputs"].values())))
                elif change == "legacy_version":
                    proof["version"] = 1
                elif change == "duplicate":
                    plan["connectors"].append(copy.deepcopy(next(item for item in self.individual["connectors"]
                        if item["key"] == child_input(0))))
                elif change == "duplicate_elsewhere":
                    duplicate = copy.deepcopy(next(item for item in self.plain["connectors"]
                        if item["key"] == child_input(0)))
                    duplicate["source"] = next(item["source"] for item in plan["connectors"]
                                               if item["key"] == child_input(9))
                    plan["connectors"].append(duplicate)
                elif change == "unexpected_display_evidence":
                    next(item for item in plan["connectors"] if item["key"] == child_input(9))[
                        "context_display_evidence"] = copy.deepcopy(edge["context_display_evidence"])
                else:
                    plan["context_group_items"] = {}
                with self.assertRaises(TraceError):
                    validate_plan(checksum(plan))

    def test_changed_canonical_outpoint_blocks_migration(self):
        self.sync(self.aggregate)
        plan = copy.deepcopy(self.individual)
        proof = plan["context_group_items"][self.group]
        proof["inputs"][child_input(0)]["outpoint"] = "other:0"
        next(item for item in plan["connectors"] if item["key"] == child_input(0))["context_evidence"] = copy.deepcopy(
            proof["inputs"][child_input(0)])
        writes = len(self.remote.writes)
        with self.assertRaisesRegex(TraceError, "original input evidence"):
            self.sync(checksum(plan))
        self.assertEqual(len(self.remote.writes), writes)

    def test_reviewed_membership_shrink_replaces_aggregate_with_no_old_lines(self):
        initial = scope_plan(self.aggregate)
        self.sync(initial)
        plain = copy.deepcopy(self.graphs[0])
        edge = next(edge for edge in plain["edges"] if edge["id"] == child_input(0))
        plain["edges"] = [item for item in plain["edges"] if item["id"] != edge["id"]]
        plain["nodes"] = [item for item in plain["nodes"] if item["id"] != edge["source"]]
        reduced = group_context_inputs(plain, enabled=True)
        with self.assertRaisesRegex(TraceError, "original input evidence"):
            self.sync(scope_plan(make_plan(reduced)))
        snapshot = board_layout.capture("test-board", self.path, initial["namespace"],
                                       token="test-token", transport=self.remote, interval=0)
        with patch("liquid_tracer.elk_layout.optimize_graph", side_effect=lambda graph, **kwargs: graph):
            graph = board_layout.prepare_graph(reduced, snapshot)
        plan = scope_plan(make_plan(graph))
        self.sync(plan)
        self.assert_no_stale_items(plan)
        self.assertNotIn(child_input(0), plan["context_group_items"][self.group]["inputs"])


if __name__ == "__main__":
    unittest.main()
