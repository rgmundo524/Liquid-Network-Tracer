"""The saved shortest-path scope must stay explicit across presentation surfaces."""
import unittest

from liquid_tracer.export import legend_lines
from liquid_tracer.legend import legend_notes
from liquid_tracer.workflow_menu import _endpoint_summary


class ShortestConnectionPresentationTests(unittest.TestCase):
    def test_all_starter_legends_are_conditional_on_captured_marker(self):
        graph = {"graph_options": {"view": "starter_connections"},
                 "connections": {"connection_scope": "shortest", "max_hops": None}}
        for render in (legend_lines, legend_notes):
            with self.subTest(renderer=render.__name__):
                legacy = render(graph)
                marked = {**graph, "connections": {**graph["connections"], "includes_all_starters": True}}
                self.assertIn("All selected starting transactions remain visible", " ".join(render(marked)))
                self.assertNotIn("All selected starting transactions remain visible", " ".join(legacy))
                self.assertEqual(legacy, render(graph))

    def test_terminal_summary_retains_starters_without_inventing_connections(self):
        plot = {"goal": "connections", "query": {"connection_scope": "shortest"},
                "includes_all_starters": True, "starting_transaction_count": 3,
                "unconnected_starting_transaction_count": 3}
        text = _endpoint_summary(plot)
        self.assertIn("all 3 starting transactions shown", text)
        self.assertIn("3 without a qualifying connection", text)
        self.assertNotIn("starting transactions shown", _endpoint_summary({
            key: value for key, value in plot.items() if key != "includes_all_starters"}))

    def test_shortest_legends_describe_pairwise_selection_and_ignored_stops(self):
        graph = {"graph_options": {"view": "starter_connections"},
                 "connections": {"connection_scope": "shortest", "max_hops": None,
                                 "transaction_io": "complete"},
                 "include_fees": False, "hop_reference_name": "Investigated group"}
        for render in (legend_lines, legend_notes):
            with self.subTest(renderer=render.__name__):
                text = " ".join(render(graph))
                self.assertIn("one shortest verified saved route per connected ordered pair", text)
                self.assertIn("ordinary transaction steps", text)
                self.assertIn("equal-length routes use a stable choice", text)
                self.assertIn("Attribution stop rules and hop limits are ignored", text)
                self.assertIn("Fee flows are hidden", text)
                self.assertNotIn("STOP TRACING: an explicit", text)
                self.assertNotIn("STOP TRACING = an explicit", text)
                self.assertNotIn("still apply", text)
                self.assertNotIn("at most None", text)

    def test_terminal_saved_scope_does_not_imply_all_paths_or_included_fees(self):
        plot = {"goal": "connections", "query": {"connection_scope": "shortest", "transaction_io": "complete"},
                "layout_settings": {"include_fees": False, "group_context_inputs": True}}
        text = _endpoint_summary(plot)
        self.assertIn("shortest connections", text)
        self.assertIn("non-fee outputs included", text)
        self.assertIn("isolated inputs grouped", text)
        self.assertNotIn("all saved connections", text)


if __name__ == "__main__":
    unittest.main()
