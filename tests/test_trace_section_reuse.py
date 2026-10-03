"""Section scheduling remains reusable without reusing stale Trace geometry."""

import copy
import tempfile
import unittest
from pathlib import Path

from liquid_tracer.common import save_json
from liquid_tracer.elk_layout import _apply_candidate, _request_graph
from liquid_tracer.export import build_graph
from liquid_tracer.horizontal_spacing import compact_candidate
from liquid_tracer.layout_preview import LAYOUT_NOTICE, layout_notice
from liquid_tracer.layout_reuse import _complete_search, reusable_elk_preview
from liquid_tracer.layout_search import LAYOUT_SEARCH_VERSION, layout_seeds
from liquid_tracer.trace_layout import TRACE_LAYOUT_VERSION
from liquid_tracer.trace_sections import SECTION_LAYOUT_VERSION
from tests.test_elk_layout import synthetic_candidate
from tests.test_layout import chain, state_from


def search_fixture(attempts=2):
    return {"version": LAYOUT_SEARCH_VERSION, "attempt_count": attempts, "attempted_count": attempts,
            "successful_count": attempts, "failed_count": 0, "failed_attempts": [],
            "seeds": list(layout_seeds(attempts)), "selected_seed": layout_seeds(attempts)[0],
            "candidate_count": attempts, "execution": "parallel", "worker_count": 8,
            "memory_retry_count": 12, "peak_rss_mb": 200, "section_layout_version": SECTION_LAYOUT_VERSION,
            "section_count": 12, "section_worker_count": 8, "max_section_nodes": 128,
            "max_worker_ports": 1024}


class TraceSectionSearchTests(unittest.TestCase):
    def test_parallel_sections_can_outnumber_attempts_and_retry_per_section(self):
        search = search_fixture()
        self.assertTrue(_complete_search(search, search, 2))
        search["memory_retry_count"] = 16
        self.assertTrue(_complete_search(search, search, 2))
        for changes in ({"worker_count": 9}, {"worker_count": True}, {"memory_retry_count": 17},
                        {"section_layout_version": SECTION_LAYOUT_VERSION + 1},
                        {"section_layout_version": SECTION_LAYOUT_VERSION - 1}, {"section_layout_version": True},
                        {"section_count": 0}, {"section_worker_count": 13}, {"section_worker_count": -1},
                        {"max_section_nodes": 0}, {"max_section_nodes": 129},
                        {"max_worker_ports": -1}, {"max_worker_ports": 1025}):
            with self.subTest(changes=changes):
                altered = {**search, **changes}
                self.assertFalse(_complete_search(altered, altered, 2))
        for field in ("section_layout_version", "section_count", "section_worker_count",
                      "max_section_nodes", "max_worker_ports"):
            altered = dict(search)
            altered.pop(field)
            with self.subTest(missing=field):
                self.assertFalse(_complete_search(altered, altered, 2))

    def test_serial_section_retry_and_singleton_only_search_are_valid(self):
        search = {**search_fixture(), "execution": "sequential", "worker_count": 1}
        self.assertTrue(_complete_search(search, search, 2))
        search.update(section_worker_count=0, memory_retry_count=0, max_section_nodes=1,
                      max_worker_ports=0)
        self.assertTrue(_complete_search(search, search, 2))
        for changes in ({"worker_count": 2}, {"memory_retry_count": 1}):
            altered = {**search, **changes}
            with self.subTest(changes=changes):
                self.assertFalse(_complete_search(altered, altered, 2))

    def test_standard_search_does_not_acquire_section_worker_or_retry_allowance(self):
        search = {key: value for key, value in search_fixture().items()
                  if not key.startswith("section_") and key not in ("max_section_nodes", "max_worker_ports")}
        self.assertFalse(_complete_search(search, search, 2))
        search.update(worker_count=2, memory_retry_count=2)
        self.assertTrue(_complete_search(search, search, 2))
        search.update(execution="sequential", worker_count=1)
        self.assertFalse(_complete_search(search, search, 2))
        search["memory_retry_count"] = 0
        self.assertTrue(_complete_search(search, search, 2))


class TraceSectionPreviewTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        state = state_from(chain(1))
        state["run_id"] = "0123456789abcdef"
        state["ancestor_runs"] = []
        self.original = build_graph(state, merge_addresses=False)
        self.original["graph_options"]["layout_style"] = "trace"
        request, ports, fees = _request_graph(self.original)
        candidate = synthetic_candidate(request, [1])[0]
        compact_candidate(candidate)
        self.saved = _apply_candidate(self.original, candidate, ports, fees, "straight")
        self.saved["graph_options"]["layout_attempts"] = 1
        search = search_fixture(1)
        search.update(worker_count=2, section_count=3, section_worker_count=2,
                      memory_retry_count=2, max_section_nodes=1, max_worker_ports=2)
        self.saved["layout"].update(search=search, metrics=copy.deepcopy(search),
                                     trace_layout={"version": TRACE_LAYOUT_VERSION, "enabled": True})
        self.path = self.root / (state["run_id"] + "-elk-01234567")
        self.path.mkdir()
        # Reuse requires complete artifact files, but this test exercises cache
        # validation without calculating or rendering any user graph.
        (self.path / "graph.svg").write_text("<svg/>")
        (self.path / "graph.html").write_text("<html></html>")
        self.write_saved()

    def write_saved(self):
        save_json(self.path / "graph.json", self.saved)
        save_json(self.path / "layout-report.json", {
            "run_id": self.saved["run_id"], "layout": self.saved["layout"],
            "node_count": len(self.saved["nodes"]), "edge_count": len(self.saved["edges"])})

    def reuse(self):
        return reusable_elk_preview(self.original, self.root, layout_attempts=1)

    def test_current_sections_reuse_and_stale_trace_versions_do_not(self):
        self.assertEqual(self.reuse(), self.saved)
        for value in (None, {"version": TRACE_LAYOUT_VERSION - 1, "enabled": True},
                      {"version": TRACE_LAYOUT_VERSION + 1, "enabled": True},
                      {"version": TRACE_LAYOUT_VERSION, "enabled": False}):
            if value is None:
                self.saved["layout"].pop("trace_layout", None)
            else:
                self.saved["layout"]["trace_layout"] = value
            self.write_saved()
            with self.subTest(value=value):
                self.assertIsNone(self.reuse())

    def test_current_small_trace_and_historical_standard_remain_reusable(self):
        search = self.saved["layout"]["search"]
        for field in ("section_layout_version", "section_count", "section_worker_count",
                      "max_section_nodes", "max_worker_ports"):
            search.pop(field)
        search.update(execution="sequential", worker_count=1, memory_retry_count=0)
        self.saved["layout"]["metrics"] = copy.deepcopy(search)
        self.write_saved()
        self.assertEqual(self.reuse(), self.saved)
        self.original["graph_options"]["layout_style"] = "standard"
        self.saved["graph_options"]["layout_style"] = "standard"
        self.saved["layout"].pop("trace_layout")
        self.write_saved()
        self.assertEqual(self.reuse(), self.saved)

    def test_sections_cannot_outnumber_objects_or_enable_standard_reuse(self):
        self.assertEqual(self.reuse(), self.saved)
        self.saved["layout"]["search"]["section_count"] = len(self.saved["nodes"]) + 1
        self.write_saved()
        self.assertIsNone(self.reuse())
        self.saved["layout"]["search"]["section_count"] = 3
        self.original["graph_options"]["layout_style"] = "standard"
        self.saved["graph_options"]["layout_style"] = "standard"
        self.write_saved()
        self.assertIsNone(self.reuse())

    def test_notice_describes_section_routes_and_preserved_backbone(self):
        notice = layout_notice(self.saved)
        self.assertIn("3 sections around the preferred backbone", notice)
        self.assertIn("connections between sections are routed after placement", notice)
        self.assertIn("Miro routes may differ", notice)
        self.assertNotIn("ELK layout;", notice)
        for version in range(1, SECTION_LAYOUT_VERSION + 1):
            self.saved["layout"]["search"]["section_layout_version"] = version
            self.assertEqual(layout_notice(self.saved), notice)
        for version in (True, 0, SECTION_LAYOUT_VERSION + 1, "7"):
            self.saved["layout"]["search"]["section_layout_version"] = version
            self.assertNotIn("3 sections", layout_notice(self.saved))
        self.saved["graph_options"]["layout_style"] = "standard"
        self.assertEqual(layout_notice(self.saved), LAYOUT_NOTICE)


if __name__ == "__main__":
    unittest.main()
