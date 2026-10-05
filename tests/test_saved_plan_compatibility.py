"""Older empty context catalogs stay reviewable without relaxing plan integrity."""

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from liquid_tracer.common import TraceError, canonical, digest, read_json, save_json
from liquid_tracer.investigations import create_investigation
from liquid_tracer.miro import make_plan, validate_plan
from liquid_tracer.plots import _same_saved_plan, plot_files, preview_plot, reviewed_plot
from tests.test_attribution_convergence import graph_state, tx
from tests.test_connections import saved_case
from tests.test_context_parallel_miro import graphs
from tests.test_miro_sync import graph


def checksum(plan):
    plan["sha256"] = digest(canonical({key: value for key, value in plan.items() if key != "sha256"}))
    return plan


class SavedPlanCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.current = make_plan(graph())
        self.assertEqual(self.current["context_parallel_items"], {})
        self.legacy = deepcopy(self.current)
        self.legacy.pop("context_parallel_items")
        checksum(self.legacy)

    def test_absent_and_empty_catalogs_match_in_both_directions_without_mutation(self):
        before = canonical(self.current), canonical(self.legacy)
        self.assertNotEqual(self.current["sha256"], self.legacy["sha256"])
        for first, second in ((self.current, self.legacy), (self.legacy, self.current),
                              (self.current, self.current), (self.legacy, self.legacy)):
            self.assertTrue(_same_saved_plan(first, second))
        self.assertEqual((canonical(self.current), canonical(self.legacy)), before)

    def test_nonempty_catalog_contents_are_compared_exactly(self):
        bundled = make_plan(graphs()[1])
        self.assertTrue(bundled["context_parallel_items"])
        changed = deepcopy(bundled)
        proof = next(iter(changed["context_parallel_items"].values()))
        member = next(iter(proof["inputs"].values()))
        member["outpoint"] = "f" * 64 + ":42"
        checksum(changed)
        # Both are structurally valid standalone plans. Only the captured
        # input provenance differs, so it must never be normalized away.
        validate_plan(bundled)
        validate_plan(changed)
        self.assertTrue(_same_saved_plan(bundled, deepcopy(bundled)))
        self.assertFalse(_same_saved_plan(bundled, changed))
        self.assertFalse(_same_saved_plan(changed, bundled))
        for missing in (False, True):
            invalid = deepcopy(bundled)
            if missing:
                invalid.pop("context_parallel_items")
            else:
                invalid["context_parallel_items"] = {}
            checksum(invalid)
            for first, second in ((bundled, invalid), (invalid, bundled)):
                with self.subTest(missing=missing), self.assertRaises(TraceError):
                    _same_saved_plan(first, second)

    def test_catalog_types_and_checksums_are_validated_for_both_operands(self):
        for value in (None, [], False, "", 0):
            invalid = deepcopy(self.current)
            invalid["context_parallel_items"] = value
            checksum(invalid)
            for first, second in ((invalid, self.legacy), (self.legacy, invalid), (invalid, invalid)):
                with self.subTest(catalog=value), self.assertRaises(TraceError):
                    _same_saved_plan(first, second)
        invalid = deepcopy(self.legacy)
        invalid["sha256"] = "0" * 64
        for first, second in ((invalid, self.current), (self.current, invalid), (invalid, invalid)):
            with self.assertRaisesRegex(TraceError, "checksum"):
                _same_saved_plan(first, second)

    def test_valid_shape_connector_and_other_catalog_changes_are_not_ignored(self):
        shape = deepcopy(self.legacy)
        next(item for item in shape["shapes"] if item["key"] == "addr:a")["body"]["data"]["content"] += " changed"
        connector = deepcopy(self.legacy)
        connector["connectors"][0]["body"]["captions"][0]["content"] += " changed"
        other_catalog = deepcopy(self.legacy)
        self.assertEqual(other_catalog.pop("context_group_items"), {})
        for changed in (shape, connector, other_catalog):
            checksum(changed)
            validate_plan(changed)
            self.assertFalse(_same_saved_plan(self.current, changed))
            self.assertFalse(_same_saved_plan(changed, self.current))


class ArchivedPlanCompatibilityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.case = create_investigation(Path(temporary.name), "Archived plan",
            seeds=[tx("a") + ":0"], run_defaults={"layout_attempts": 1})
        _, self.archive = saved_case(self.case, graph_state((("a:0", "b"),), seeds=("a:0",)))
        optimizer = patch("liquid_tracer.elk_layout.optimize_graph", side_effect=lambda value, **kwargs: value)
        optimizer.start()
        self.addCleanup(optimizer.stop)
        self.preview = preview_plot(self.case, "full")
        self.directory = Path(self.preview["directory"])
        self.plan = read_json(self.directory / "miro-plan.json")
        self.assertEqual(self.plan.pop("context_parallel_items"), {})
        checksum(self.plan)
        self.save_plan(self.plan)

    def save_plan(self, plan):
        save_json(self.directory / "miro-plan.json", plan)
        names = sorted(plot_files(self.directory) - {"SHA256SUMS"})
        (self.directory / "SHA256SUMS").write_text("".join(
            digest((self.directory / name).read_bytes()) + "  " + name + "\n" for name in names))

    def test_legacy_archived_plan_reviews_without_rewriting_bytes_or_replacing_its_hash(self):
        paths = [path for root in (self.directory, self.archive) for path in root.rglob("*") if path.is_file()]
        paths.append(self.case / "case.json")
        before = {path: path.read_bytes() for path in paths}
        graph_before = read_json(self.directory / "graph.json")
        for capture in (False, True):
            reviewed_graph, reviewed_plan = reviewed_plot(self.case, self.preview["preview_id"], _capture_reuse=capture)
            self.assertEqual(reviewed_graph, graph_before)
            self.assertEqual(reviewed_plan, self.plan)
            self.assertEqual(reviewed_plan["sha256"], self.plan["sha256"])
            self.assertNotIn("context_parallel_items", reviewed_plan)
        self.assertEqual(before, {path: path.read_bytes() for path in before})

    def test_legacy_catalog_compatibility_does_not_allow_rehashed_shape_or_connector_changes(self):
        for collection in ("shapes", "connectors"):
            changed = deepcopy(self.plan)
            item = changed[collection][0]
            if collection == "shapes":
                item["body"]["data"]["content"] += " changed"
            else:
                item["body"]["captions"][0]["content"] += " changed"
            checksum(changed)
            validate_plan(changed)
            self.save_plan(changed)
            with self.subTest(collection=collection), self.assertRaisesRegex(TraceError, "disagree"):
                reviewed_plot(self.case, self.preview["preview_id"])


if __name__ == "__main__":
    unittest.main()
