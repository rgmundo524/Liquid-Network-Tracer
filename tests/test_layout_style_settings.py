"""Trace presentation opts in without retargeting evidence or old snapshots."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from liquid_tracer.common import TraceError, canonical, digest, read_json, save_json
from liquid_tracer.investigations import (create_investigation, read_case, save_plot_settings,
                                         validate_settings)
from liquid_tracer.plots import (_settings, _snapshot_settings, preview_plot,
                                  reviewed_plot, validate_layout_settings)
from liquid_tracer.web import public_graph_options
from tests.test_attribution_convergence import graph_state, tx
from tests.test_connections import saved_case
from tests.test_pegout_paths import add_pegout


class LayoutStyleSettingsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.case = create_investigation(Path(directory.name), "Style", seeds=[tx("a") + ":0"],
                                         run_defaults={"layout_attempts": 1})
        state = graph_state((("a:0", "c"), ("c:0", "b")), seeds=("a:0", "b:0"))
        add_pegout(state, tx("b"))
        self.state, self.archive = saved_case(self.case, state)
        layout = patch("liquid_tracer.elk_layout.optimize_graph", side_effect=lambda graph, **kwargs: graph)
        self.layout = layout.start()
        self.addCleanup(layout.stop)

    def test_validated_style_is_display_only_and_invalid_values_do_not_write(self):
        self.assertEqual(validate_settings({})["layout_style"], "standard")
        before = read_case(self.case)
        changed = save_plot_settings(self.case, {"layout_style": "trace"})
        self.assertEqual(changed, {**before, "run_defaults": {**before["run_defaults"], "layout_style": "trace"}})
        saved = (self.case / "case.json").read_bytes()
        for value in (None, True, 1, [], {}, "Trace", "compact", " trace "):
            with self.subTest(value=value):
                with self.assertRaisesRegex(TraceError, "layout_style"):
                    save_plot_settings(self.case, {"layout_style": value})
                self.assertEqual((self.case / "case.json").read_bytes(), saved)
                self.assertIsNone(public_graph_options({"layout_style": value}))
        self.assertNotIn("layout_style", public_graph_options({}))
        self.assertEqual(public_graph_options({"layout_style": "trace"})["layout_style"], "trace")

    def test_trace_reaches_optimizer_for_each_goal_without_changing_evidence_or_csvs(self):
        evidence = (self.archive / "trace.json").read_bytes()
        for goal in ("full", "connections", "pegouts"):
            with self.subTest(goal=goal):
                save_plot_settings(self.case, {"layout_style": "standard"})
                standard = preview_plot(self.case, goal, max_hops=3)
                original, _ = reviewed_plot(self.case, standard["preview_id"])
                save_plot_settings(self.case, {"layout_style": "trace"})
                result = preview_plot(self.case, goal, max_hops=3)
                self.assertEqual(self.layout.call_args.args[0]["graph_options"]["layout_style"], "trace")
                current, plan = reviewed_plot(self.case, result["preview_id"])
                self.assertEqual(current["nodes"], original["nodes"])
                self.assertEqual(current["edges"], original["edges"])
                self.assertEqual(current["plot"]["query"], original["plot"]["query"])
                self.assertEqual(current["plot"]["layout_settings"]["layout_style"], "trace")
                self.assertEqual(plan["graph_options"]["layout_style"], "trace")
                names = ("transactions.csv", "path-transactions.csv", "trace-endpoints.csv") if goal == "pegouts" else ("transactions.csv",)
                for name in names:
                    self.assertEqual((Path(standard["directory"]) / name).read_bytes(),
                                     (Path(result["directory"]) / name).read_bytes())
                # Saving a new default must not reinterpret the previous layout.
                self.assertEqual(reviewed_plot(self.case, standard["preview_id"])[0], original)
        self.assertEqual((self.archive / "trace.json").read_bytes(), evidence)

    def test_missing_legacy_style_keeps_exact_snapshot_hash_and_review(self):
        metadata = read_case(self.case)
        metadata["run_defaults"].pop("layout_style")
        save_json(self.case / "case.json", metadata)
        before = (self.case / "case.json").read_bytes()
        legacy = _settings(metadata)
        self.assertNotIn("layout_style", legacy)
        self.assertEqual(validate_layout_settings(legacy), legacy)
        # Submit an explicit historical snapshot. A newly generated preview
        # without settings now deliberately resolves the current Trace default.
        result = preview_plot(self.case, "full", layout_settings=legacy)
        self.assertEqual(result["settings_sha256"], digest(canonical(legacy)))
        graph, plan = reviewed_plot(self.case, result["preview_id"])
        self.assertNotIn("layout_style", graph["graph_options"])
        self.assertNotIn("layout_style", result["layout_settings"])
        self.assertEqual((self.case / "case.json").read_bytes(), before)
        snapshot = {path.name: path.read_bytes() for path in Path(result["directory"]).iterdir() if path.is_file()}
        save_plot_settings(self.case, {"layout_style": "trace"})
        self.assertEqual(reviewed_plot(self.case, result["preview_id"]), (graph, plan))
        self.assertEqual(snapshot, {path.name: path.read_bytes() for path in Path(result["directory"]).iterdir() if path.is_file()})
        # An older client can still submit a complete pre-style settings snapshot.
        again = preview_plot(self.case, "full", layout_settings=legacy)
        self.assertEqual(again["layout_settings"], legacy)
        self.assertNotIn("layout_style", reviewed_plot(self.case, again["preview_id"])[0]["graph_options"])

    def test_style_cannot_disagree_with_captured_settings_even_with_rehashed_settings(self):
        save_plot_settings(self.case, {"layout_style": "trace"})
        result = preview_plot(self.case, "full")
        original, _ = reviewed_plot(self.case, result["preview_id"])
        for change in ("style", "missing", "hash", "legacy"):
            graph = deepcopy(original)
            if change == "style":
                graph["graph_options"]["layout_style"] = "standard"
            elif change == "missing":
                graph["graph_options"].pop("layout_style")
            elif change == "hash":
                graph["plot"]["layout_settings"]["layout_style"] = "standard"
                graph["graph_options"]["layout_style"] = "standard"
            else:
                graph["plot"]["layout_settings"].pop("layout_style")
                graph["plot"]["settings_sha256"] = digest(canonical(graph["plot"]["layout_settings"]))
            with self.subTest(change=change), self.assertRaisesRegex(TraceError, "layout settings"):
                _snapshot_settings(graph)


if __name__ == "__main__":
    unittest.main()
