"""New generation preferences use Trace without reinterpreting saved evidence."""

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from liquid_tracer.common import TraceError, canonical, digest, read_json, save_json
from liquid_tracer.investigations import (
    DEFAULTS, create_investigation, generation_settings, list_investigations,
    load_settings, read_case, save_collection_reference, save_plot_settings,
    save_settings, update_case, validate_settings,
)
from liquid_tracer.plots import _settings, preview_plot, reviewed_plot, validate_layout_settings
from tests.test_attribution_convergence import graph_state, tx
from tests.test_connections import saved_case
from tests.test_pegout_paths import add_pegout


class GenerationDefaultsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "cases"

    def legacy_case(self, *, style=None, grouping=False):
        case = create_investigation(self.root, "Existing investigation")
        metadata = read_case(case)
        metadata["run_defaults"] = {"group_context_inputs": grouping, "hops": 7}
        if style is not None:
            metadata["run_defaults"]["layout_style"] = style
        save_json(case / "case.json", metadata)
        archive = case / "runs" / ("a" * 16)
        archive.mkdir(parents=True)
        save_json(archive / "trace.json", {"saved_evidence": True})
        return case, archive

    def test_new_default_does_not_change_historical_validation_or_input(self):
        original_defaults = deepcopy(DEFAULTS)
        supplied = {"group_context_inputs": True, "hub_addresses": ["G" * 34, "G" * 34]}
        original = deepcopy(supplied)
        generated = generation_settings(supplied)
        self.assertEqual(generated["layout_style"], "trace")
        self.assertTrue(generated["group_context_inputs"])
        self.assertEqual(generated["hub_addresses"], ["G" * 34])
        self.assertEqual(supplied, original)
        self.assertEqual(DEFAULTS, original_defaults)
        self.assertEqual(validate_settings({})["layout_style"], "standard")
        self.assertEqual(DEFAULTS["layout_style"], "standard")
        self.assertFalse(generation_settings({})["group_context_inputs"])
        for grouping in (False, True):
            with self.subTest(grouping=grouping):
                chosen = generation_settings({"layout_style": "standard", "group_context_inputs": grouping})
                self.assertEqual(chosen["layout_style"], "standard")
                self.assertIs(chosen["group_context_inputs"], grouping)

    def test_generation_uses_the_same_strict_validation(self):
        for settings in (None, [], True, {"unknown": 1}, {"layout_style": None},
                         {"layout_style": "Trace"}, {"group_context_inputs": 1},
                         {"hops": -1}, {"hub_addresses": ["short"]}):
            with self.subTest(settings=settings):
                with self.assertRaises(TraceError) as historical:
                    validate_settings(settings)
                with self.assertRaises(TraceError) as generated:
                    generation_settings(settings)
                self.assertEqual(str(generated.exception), str(historical.exception))
        for settings in (False, [], {"layout_style": "Trace"}):
            with self.subTest(creation_settings=settings), self.assertRaises(TraceError):
                create_investigation(self.root, "Invalid defaults", run_defaults=settings)
        self.assertFalse(self.root.exists())

    def test_new_global_settings_and_cases_default_to_trace(self):
        self.assertEqual(load_settings(self.root)["layout_style"], "trace")
        self.assertFalse(self.root.exists())
        supplied = {"group_context_inputs": False}
        self.assertEqual(save_settings(self.root, supplied)["layout_style"], "trace")
        self.assertEqual(read_json(self.root / "settings.json")["layout_style"], "trace")
        self.assertEqual(supplied, {"group_context_inputs": False})
        for settings in (None, {}, {"group_context_inputs": True}, load_settings(self.root)):
            with self.subTest(settings=settings):
                case = create_investigation(self.root, "New investigation", run_defaults=settings)
                saved = read_case(case)["run_defaults"]
                self.assertEqual(saved["layout_style"], "trace")
                self.assertIs(saved["group_context_inputs"], bool(settings and settings.get("group_context_inputs")))

    def test_explicit_standard_global_and_case_preferences_are_preserved(self):
        for grouping in (False, True):
            with self.subTest(grouping=grouping):
                supplied = {"layout_style": "standard", "group_context_inputs": grouping}
                saved = save_settings(self.root, supplied)
                self.assertEqual(load_settings(self.root), saved)
                self.assertEqual(saved["layout_style"], "standard")
                self.assertIs(saved["group_context_inputs"], grouping)
                case = create_investigation(self.root, "Standard investigation", run_defaults=saved)
                self.assertEqual(read_case(case)["run_defaults"], saved)

    def test_loading_legacy_preferences_does_not_rewrite_case_or_archive(self):
        case, archive = self.legacy_case()
        legacy = {"group_context_inputs": True}
        save_json(self.root / "settings.json", legacy)
        # This is an older preview's complete display-settings fingerprint.
        layout = {key: value for key, value in validate_settings({}).items()
                  if key in ("layout_attempts", "connector_style", "include_fees",
                             "color_attribution_arrows", "group_context_inputs", "center_name", "hub_addresses")}
        layout["presentation_version"] = 1
        fingerprint = digest(canonical(layout))
        save_json(archive / "preview-settings.json", {"layout_settings": layout, "settings_sha256": fingerprint})
        before = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(load_settings(self.root)["layout_style"], "trace")
        self.assertNotIn("layout_style", read_case(case)["run_defaults"])
        self.assertNotIn("layout_style", dict(list_investigations(self.root))[case]["run_defaults"])
        self.assertEqual(validate_settings(read_case(case)["run_defaults"])["layout_style"], "standard")
        self.assertEqual(validate_layout_settings(layout), layout)
        self.assertEqual(digest(canonical(layout)), fingerprint)
        self.assertEqual(before, {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()})

    def test_explicit_case_default_updates_use_trace_without_changing_archives(self):
        case, archive = self.legacy_case()
        before = {path: path.read_bytes() for path in archive.iterdir()}
        original = read_case(case)["run_defaults"]
        update_case(case, {"name": "Renamed only"})
        self.assertEqual(read_case(case)["run_defaults"], original)
        supplied = {"group_context_inputs": True, "hops": 9}
        changed = update_case(case, {"run_defaults": supplied})["run_defaults"]
        self.assertEqual(changed["layout_style"], "trace")
        self.assertTrue(changed["group_context_inputs"])
        self.assertEqual(supplied, {"group_context_inputs": True, "hops": 9})
        chosen = update_case(case, {"run_defaults": {"layout_style": "standard", "group_context_inputs": False}})
        self.assertEqual(chosen["run_defaults"]["layout_style"], "standard")
        self.assertFalse(chosen["run_defaults"]["group_context_inputs"])
        self.assertEqual(before, {path: path.read_bytes() for path in archive.iterdir()})

    def test_partial_plot_and_collection_saves_preserve_explicit_choices(self):
        for style in (None, "standard"):
            for grouping in (False, True):
                for save in (lambda case: save_plot_settings(case, {"include_fees": True}),
                             lambda case: save_collection_reference(case, "Treasury")):
                    with self.subTest(style=style, grouping=grouping, save=save):
                        case, archive = self.legacy_case(style=style, grouping=grouping)
                        before = {path: path.read_bytes() for path in archive.iterdir()}
                        result = save(case)["run_defaults"]
                        self.assertEqual(result["layout_style"], style or "trace")
                        self.assertIs(result["group_context_inputs"], grouping)
                        self.assertEqual(result["hops"], 7)
                        self.assertEqual(before, {path: path.read_bytes() for path in archive.iterdir()})

    def test_empty_plot_save_does_not_migrate_legacy_case(self):
        case, _ = self.legacy_case()
        before = (case / "case.json").read_bytes()
        self.assertNotIn("layout_style", save_plot_settings(case, {})["run_defaults"])
        self.assertEqual((case / "case.json").read_bytes(), before)


class GenerationPlotDefaultsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.case = create_investigation(Path(temporary.name), "Generation boundary",
            seeds=[tx("a") + ":0", tx("b") + ":0"], run_defaults={"layout_attempts": 1})
        state = graph_state((("a:0", "c"), ("c:0", "b")), seeds=("a:0", "b:0"))
        add_pegout(state, tx("b"))
        _, self.archive = saved_case(self.case, state)
        optimizer = patch("liquid_tracer.elk_layout.optimize_graph", side_effect=lambda graph, **kwargs: graph)
        self.optimizer = optimizer.start()
        self.addCleanup(optimizer.stop)

    def set_style(self, style):
        metadata = read_case(self.case)
        if style is None:
            metadata["run_defaults"].pop("layout_style", None)
        else:
            metadata["run_defaults"]["layout_style"] = style
        save_json(self.case / "case.json", metadata)
        return metadata

    def test_new_previews_use_trace_for_missing_style_and_keep_explicit_standard(self):
        for style in (None, "standard"):
            self.set_style(style)
            metadata_bytes = (self.case / "case.json").read_bytes()
            for goal in ("full", "connections", "pegouts"):
                with self.subTest(style=style, goal=goal):
                    preview = preview_plot(self.case, goal, max_hops=3)
                    graph, plan = reviewed_plot(self.case, preview["preview_id"])
                    expected = style or "trace"
                    self.assertEqual(preview["layout_settings"]["layout_style"], expected)
                    self.assertEqual(graph["graph_options"]["layout_style"], expected)
                    self.assertEqual(plan["graph_options"]["layout_style"], expected)
                    self.assertEqual(self.optimizer.call_args.args[0]["graph_options"]["layout_style"], expected)
                    self.assertFalse(graph["graph_options"]["group_context_inputs"])
            self.assertEqual((self.case / "case.json").read_bytes(), metadata_bytes)

    def test_new_default_generation_does_not_reinterpret_or_rewrite_archived_previews(self):
        for archived_style in (None, "standard"):
            with self.subTest(archived_style=archived_style):
                captured = _settings(self.set_style(archived_style))
                archived = preview_plot(self.case, "full", layout_settings=captured)
                reviewed = reviewed_plot(self.case, archived["preview_id"])
                archived_path = Path(archived["directory"])
                protected = {path: path.read_bytes() for directory in (self.archive, archived_path)
                             for path in directory.rglob("*") if path.is_file()}
                fingerprint = archived["settings_sha256"]
                self.assertEqual(fingerprint, digest(canonical(captured)))
                if archived_style is None:
                    self.assertNotIn("layout_style", reviewed[0]["graph_options"])
                self.set_style(None)
                metadata_bytes = (self.case / "case.json").read_bytes()
                generated = preview_plot(self.case, "full")
                self.assertEqual(generated["layout_settings"]["layout_style"], "trace")
                self.assertNotEqual(generated["settings_sha256"], fingerprint)
                self.assertEqual(reviewed_plot(self.case, archived["preview_id"]), reviewed)
                self.assertEqual(protected, {path: path.read_bytes() for path in protected})
                self.assertEqual((self.case / "case.json").read_bytes(), metadata_bytes)


if __name__ == "__main__":
    unittest.main()
