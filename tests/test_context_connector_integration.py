"""Display-only context links retain exact saved UTXO evidence end to end."""

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from liquid_tracer.common import LBTC, canonical, read_json
from liquid_tracer.elk_layout import optimize_graph
from liquid_tracer.export import build_graph, svg_graph
from liquid_tracer.layout_preview import export_layout, render_svg
from liquid_tracer.mermaid import export_mermaid, mermaid_source
from liquid_tracer.miro import make_plan, validate_plan
from liquid_tracer.pegout_csv import pegout_csv_rows, write_pegout_csvs
from liquid_tracer.pegout_paths import pegout_graph, validate_query
from liquid_tracer.transaction_csv import transaction_csv_rows, write_transaction_csv
from tests.test_attribution_convergence import graph_state, tx
from tests.test_elk_layout import synthetic_candidate
from tests.test_input_order import input_order_state
from tests.test_layout import txid
from tests.test_pegout_paths import add_pegout, add_unspendable, mark_unspent


def evidence(graph):
    """Geometry may change; no saved input occurrence or meaning may change."""
    fields = ("id", "source", "target", "original_source", "outpoint", "role",
              "label", "quantity", "details", "change_output")
    return {edge["id"]: {key: deepcopy(edge[key]) for key in fields if key in edge}
            for edge in graph["edges"]}


def svg_edge_ids(svg):
    return {element.attrib[field] for element in ET.fromstring(svg).iter()
            for field in ("data-edge-key", "data-edge-id") if field in element.attrib}


class ContextConnectorIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.dict("os.environ", {
            "XDG_CACHE_HOME": temporary, "XDG_STATE_HOME": temporary,
        }))

    def layout(self, graph):
        from liquid_tracer.context_connectors import prepare

        requests = []

        def worker(request, seeds, **kwargs):
            requests.append(deepcopy(request))
            ordered = deepcopy(request)
            ordered["children"].sort(key=lambda node: (
                int(node["layoutOptions"]["elk.partitioning.partition"]), node["id"]))
            return synthetic_candidate(ordered, seeds, **kwargs)

        with patch("liquid_tracer.elk_layout._worker", side_effect=worker), \
                patch("liquid_tracer.api.Esplora.get", side_effect=AssertionError("Saved evidence only")):
            result = optimize_graph(prepare(graph), layout_attempts=1)
        self.assertTrue(requests)
        return result, requests

    def test_repeated_addresses_unknown_values_and_every_csv_occurrence_survive_layout(self):
        state = input_order_state(7, continuing=(6,))
        inputs = state["transactions"][txid("input-order-child")]["data"]["vin"]
        inputs[0]["vout"], inputs[1]["vout"] = 7, 9
        inputs[1]["prevout"]["scriptpubkey_address"] = inputs[0]["prevout"]["scriptpubkey_address"]
        inputs[0]["prevout"].update(value=2 ** 63 - 1, asset=LBTC)
        inputs[0]["prevout"].pop("valuecommitment")
        inputs[0]["prevout"].pop("assetcommitment")
        original = canonical(state)
        for merged in (True, False):
            with self.subTest(merge_addresses=merged):
                plain = build_graph(state, merge_addresses=merged)
                grouped = build_graph(state, merge_addresses=merged, group_context_inputs=True)
                expected = evidence(grouped)
                laid_out, _ = self.layout(grouped)
                self.assertEqual(evidence(laid_out), expected)
                rows = transaction_csv_rows(laid_out, state)
                self.assertEqual(rows, transaction_csv_rows(plain, state))
                self.assertEqual(len(rows), len(laid_out["edges"]))
                first, second = [row for row in rows if row["Direction"] == "IN"][:2]
                self.assertEqual(first["Address Hash"], second["Address Hash"])
                self.assertEqual((first["Number of I/O"], second["Number of I/O"]), (0, 1))
                self.assertEqual((first["Asset Value"], first["Asset"]), (2 ** 63 - 1, "L-BTC"))
                self.assertEqual((second["Asset Value"], second["Asset"]), ("", ""))
                self.assertIn("CONFIDENTIAL VALUE", second["Address Flags"])
                with tempfile.TemporaryDirectory() as temporary:
                    before, after = Path(temporary) / "before.csv", Path(temporary) / "after.csv"
                    write_transaction_csv(before, plain, state)
                    write_transaction_csv(after, laid_out, state)
                    self.assertEqual(after.read_bytes(), before.read_bytes())
        self.assertEqual(canonical(state), original)

    def test_one_context_port_and_link_match_worker_miro_and_both_svg_renderers(self):
        from liquid_tracer.context_connectors import display_graph

        for count in (4, 11):
            with self.subTest(input_count=count):
                state = input_order_state(count, continuing=(count - 1,))
                state["ancestor_runs"] = []
                graph = build_graph(state, group_context_inputs=True)
                group, = [node for node in graph["nodes"] if node["kind"] == "context_group"]
                hidden = set(group["details"]["input_edge_ids"])
                laid_out, requests = self.layout(graph)
                displayed = display_graph(laid_out)
                shown = {edge["id"] for edge in displayed["edges"]}
                self.assertEqual(len(shown), len(graph["edges"]) - len(hidden) + 1)
                self.assertTrue(hidden.isdisjoint(shown))
                grouped_links = [edge for edge in displayed["edges"] if edge["source"] == group["id"]]
                self.assertEqual(len(grouped_links), 1)
                self.assertEqual(grouped_links[0]["target"], group["details"]["transaction_id"])
                for request in requests:
                    self.assertEqual({edge["id"] for edge in request["edges"]}, shown)
                    source = next(node for node in request["children"] if node["id"] == group["id"])
                    target = next(node for node in request["children"]
                                  if node["id"] == group["details"]["transaction_id"])
                    self.assertEqual(len(source["ports"]), 1)
                    west_ports = [port for port in target["ports"]
                                  if port["layoutOptions"]["elk.port.side"] == "WEST"]
                    self.assertEqual(len(west_ports), 2)
                plan = make_plan(laid_out)
                validate_plan(plan)
                self.assertEqual({item["key"] for item in plan["connectors"]}, shown)
                self.assertEqual(svg_edge_ids(render_svg(laid_out)), shown)
                self.assertEqual(svg_edge_ids(svg_graph(laid_out)), shown)
                self.assertEqual(sum(" -->" in line for line in mermaid_source(laid_out).splitlines()), len(shown))
                self.assertEqual(evidence(laid_out), evidence(graph))

    def test_pegout_paths_seed_candidates_and_endpoint_exports_remain_exact(self):
        from liquid_tracer.context_connectors import display_graph

        state = graph_state((("a:0", "b"), ("b:0", "c")),
                            raw_links=(("d:0", "b"), ("e:0", "b")), seeds=("a:0",))
        add_pegout(state, tx("c"))
        add_unspendable(state, tx("c"))
        mark_unspent(state, tx("c") + ":0")
        query = validate_query(seeds=state["seeds"], transaction_io="complete",
                               include_unspent=True, include_unspendable=True)
        graph = pegout_graph(state, query, group_context_inputs=True, include_fees=False)
        original = canonical((state, graph))
        laid_out, _ = self.layout(graph)
        displayed = display_graph(laid_out)
        self.assertEqual(laid_out["pegouts"], graph["pegouts"])
        self.assertEqual(laid_out["branch_structure"], graph["branch_structure"])
        self.assertEqual(laid_out["address_convergences"], graph["address_convergences"])
        protected_roles = {"seed_output", "traced_input", "candidate_output"}
        protected = {edge["id"]: row for edge in graph["edges"]
                     if edge["role"] in protected_roles for row in [evidence(graph)[edge["id"]]]}
        self.assertEqual(set(protected_roles), {row["role"] for row in protected.values()})
        self.assertEqual({key: evidence(displayed)[key] for key in protected}, protected)
        before_nodes = {node["id"]: node for node in graph["nodes"]}
        self.assertEqual(set(before_nodes), {node["id"] for node in displayed["nodes"]})
        for node in displayed["nodes"]:
            self.assertEqual(node.get("role"), before_nodes[node["id"]].get("role"))
            self.assertEqual(node["details"], before_nodes[node["id"]]["details"])
        self.assertEqual(pegout_csv_rows(laid_out, state), pegout_csv_rows(graph, state))
        with tempfile.TemporaryDirectory() as temporary:
            before, after = Path(temporary) / "before", Path(temporary) / "after"
            before.mkdir()
            after.mkdir()
            write_pegout_csvs(before, graph, state)
            write_pegout_csvs(after, laid_out, state)
            for name in ("path-transactions.csv", "trace-endpoints.csv"):
                self.assertEqual((before / name).read_bytes(), (after / name).read_bytes())
        self.assertEqual(canonical((state, graph)), original)

    def test_offline_export_has_projected_drawing_and_complete_canonical_graph(self):
        from liquid_tracer.context_connectors import display_graph

        graph = build_graph(input_order_state(11, continuing=(10,)), group_context_inputs=True)
        laid_out, _ = self.layout(graph)
        displayed = display_graph(laid_out)
        shown = {edge["id"] for edge in displayed["edges"]}
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "preview"
            export_layout(laid_out, directory)
            saved = read_json(directory / "graph.json")
            self.assertEqual(evidence(saved), evidence(graph))
            self.assertEqual(svg_edge_ids((directory / "graph.svg").read_bytes()), shown)
            index = read_json(directory / "details.json")
            activity_edges = {key for activity in index["activities"] for key in activity["edge_ids"]}
            self.assertEqual(activity_edges, shown)
            self.assertEqual(set(index["edge_pages"]), shown)
            details = (directory / "details.html").read_text()
            for edge in graph["edges"]:
                if "original_source" in edge:
                    self.assertIn(edge["id"], details)
                    self.assertIn(edge["original_source"], details)
                    self.assertIn(edge["outpoint"], details)

    def test_reading_legacy_grouped_graph_does_not_silently_replace_its_links(self):
        legacy = build_graph(input_order_state(4, continuing=(3,)), group_context_inputs=True)
        legacy.pop("context_connectors", None)
        original = canonical(legacy)
        ids = {edge["id"] for edge in legacy["edges"]}
        self.assertEqual({item["key"] for item in make_plan(legacy)["connectors"]}, ids)
        self.assertEqual(svg_edge_ids(render_svg(legacy)), ids)
        self.assertEqual(canonical(legacy), original)

    def test_mermaid_export_draws_summary_links_but_saves_all_original_evidence(self):
        from liquid_tracer.context_connectors import display_graph

        graph = build_graph(input_order_state(5, continuing=(4,)), group_context_inputs=True)
        laid_out, _ = self.layout(graph)
        expected_count = len(display_graph(laid_out)["edges"])
        before = canonical(laid_out)

        def renderer(command, directory):
            target = Path(command[command.index("--output") + 1])
            target.write_text('<svg xmlns="http://www.w3.org/2000/svg"><text>Synthetic renderer</text></svg>')
            return 0

        with tempfile.TemporaryDirectory() as temporary, \
                patch.dict("os.environ", {"LIQUID_MERMAID_BIN": "/synthetic/mmdc"}), \
                patch("liquid_tracer.mermaid._render", side_effect=renderer):
            directory = Path(temporary) / "mermaid"
            export_mermaid(laid_out, directory)
            self.assertEqual(canonical(read_json(directory / "graph.json")), before)
            source = (directory / "graph.mmd").read_text()
            self.assertEqual(sum(" -->" in line for line in source.splitlines()), expected_count)
            self.assertIn(f"{expected_count} links", (directory / "graph.html").read_text())
        self.assertEqual(canonical(laid_out), before)


if __name__ == "__main__":
    unittest.main()
