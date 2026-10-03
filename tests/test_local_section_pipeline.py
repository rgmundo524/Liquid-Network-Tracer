"""Section geometry through real adapters/export, using synthetic workers only."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from liquid_tracer.common import canonical
from liquid_tracer.edge_labels import validate_label_layout
from liquid_tracer.elk_layout import optimize_graph, layout_metrics
from liquid_tracer.layout_preview import export_layout
from liquid_tracer.layout_overview import navigation_files
from liquid_tracer.miro import make_plan, validate_plan
from liquid_tracer.pegout_csv import pegout_csv_rows
from liquid_tracer.pegout_paths import pegout_graph, validate_query
from liquid_tracer.trace_section_geometry import assemble_gutters
from liquid_tracer.trace_sections import SECTION_LAYOUT_VERSION
from liquid_tracer.transaction_csv import transaction_csv_rows
from tests.test_attribution_convergence import graph_state, tx
from tests.test_context_connector_integration import evidence
from tests.test_pegout_paths import add_pegout, add_unspendable, mark_unspent
from tests.test_trace_section_geometry import candidates_for
from tests.test_trace_sections import sections


class LocalSectionPipelineTests(unittest.TestCase):
    def test_pegout_evidence_and_publication_survive_sections_and_overview(self):
        state = graph_state((("a:0", "b"), ("b:0", "c"), ("a:1", "d")),
                            seeds=("a:0", "a:1"))
        state.update(run_id="0123456789abcdef", ancestor_runs=[])
        add_pegout(state, tx("c")); add_pegout(state, tx("d"))
        add_unspendable(state, tx("b"))
        mark_unspent(state, tx("c") + ":0")
        graph = pegout_graph(state, validate_query(seeds=[tx("a") + ":0", tx("a") + ":1"],
            include_unspent=True, include_unspendable=True, include_context=True))
        graph.setdefault("graph_options", {})["layout_style"] = "trace"
        before = canonical((state, graph))
        requests = []

        def worker(request, seeds):
            requests.append(deepcopy(request))
            groups = [[node["id"]] for node in request["children"]]
            result = assemble_gutters(request, groups, candidates_for(request, groups), [])
            return [{**result, "seed": seed} for seed in seeds]

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.dict("os.environ", {"XDG_CACHE_HOME": temporary, "XDG_STATE_HOME": temporary}), \
                 patch("liquid_tracer.api.Esplora.get", side_effect=AssertionError("No collection")), \
                 patch("liquid_tracer.trace_sections.MIN_SECTION_NODES", 1), \
                 patch("liquid_tracer.trace_sections.MAX_SECTION_NODES", 6), \
                 patch("liquid_tracer.trace_sections.iter_sections", side_effect=sections), \
                 patch("liquid_tracer.elk_layout._worker", side_effect=worker):
                result = optimize_graph(graph, "elbowed", layout_attempts=1)
            self.assertEqual(canonical((state, graph)), before)
            self.assertTrue(requests)
            self.assertTrue(all(len(request["children"]) <= 6 for request in requests))
            self.assertEqual(result["layout"]["search"]["section_layout_version"], SECTION_LAYOUT_VERSION)
            self.assertEqual(evidence(result), evidence(graph))
            self.assertEqual(result["pegouts"], graph["pegouts"])
            self.assertEqual(transaction_csv_rows(result, state), transaction_csv_rows(graph, state))
            self.assertEqual(pegout_csv_rows(result, state), pegout_csv_rows(graph, state))
            node_ids = {node["id"] for node in graph["nodes"]}
            self.assertEqual({node["id"] for node in result["nodes"]}, node_ids)
            membership = [key for section in result["layout"]["section_geometry"]["sections"]
                          for key in section["node_ids"]]
            self.assertEqual(len(membership), len(node_ids))
            self.assertEqual(set(membership), node_ids)
            self.assertEqual(layout_metrics(result)["node_overlaps"], 0)
            for edge in result["edges"]:
                validate_label_layout(edge)
            plan = make_plan(result); validate_plan(plan)
            self.assertEqual({item["key"] for item in plan["shapes"] if item["key"] != "legend" and item["key"] not in plan.get("presentation_items", {})}, node_ids)
            self.assertEqual({item["key"] for item in plan["connectors"]}, {e["id"] for e in graph["edges"]})
            with patch("liquid_tracer.layout_overview.MIN_NODES", 1):
                paths = export_layout(result, root / "preview")
            saved = json.loads(Path(paths["graph"]).read_text())
            self.assertEqual(saved, result)
            self.assertIn("Trace section overview", Path(paths["html"]).read_text())
            self.assertNotIn("<svg", Path(paths["html"]).read_text())
            self.assertTrue(navigation_files(root / "preview"))
            # The overview is only another view; the complete saved graph still
            # yields the exact same Miro objects and transaction records.
            self.assertEqual(make_plan(saved), plan)
            self.assertEqual(pegout_csv_rows(saved, state), pegout_csv_rows(graph, state))


if __name__ == "__main__":
    unittest.main()
