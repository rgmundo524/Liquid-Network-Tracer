"""Evidence-backed terminal output highlighting, without address history lookup."""

import copy
import shutil
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

from liquid_tracer.api import Esplora, Limits
from liquid_tracer.common import canonical, save_json
from liquid_tracer.elk_layout import optimize_graph
from liquid_tracer.export import COLORS, build_graph, export_run, svg_graph
from liquid_tracer.layout_preview import render_svg
from liquid_tracer.mermaid import mermaid_source
from liquid_tracer.miro import make_plan
from liquid_tracer.store import Store
from liquid_tracer.trace import new_state, trace
from tests.fixtures import A, B, C, D, X, fixture, output


ROOT = Path(__file__).resolve().parents[1]
HAS_ELK = bool(shutil.which("node") and (ROOT / "layout/node_modules/elkjs/package.json").is_file())
ENDPOINT = C + ":0"


class UnspentEndpointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "case")
        self.addCleanup(self.store.close)
        self.fixture_file = self.root / "fixture.json"
        self.responses = fixture()
        self.responses["/tx/" + C + "/outspends"][0] = {"spent": False}
        save_json(self.fixture_file, self.responses)

    def run_trace(self, *, hops=3, seeds=None, parent=None, labels=None, transport=None):
        limits = Limits(max_hops=hops)
        options = {"fixture": self.fixture_file} if transport is None else {
            "base": "https://blockstream.info/liquid/api", "auth": "none", "transport": transport}
        with Esplora(self.store, "pending", limits, min_interval=0, **options) as api:
            state = new_state(seeds or [A + ":0"], api.base, limits, labels or [], parent=parent)
            api.run_id = state["run_id"]
            return trace(api, state, limits, self.root / "checkpoints" / (state["run_id"] + ".json"))

    @staticmethod
    def node(graph, outpoint=ENDPOINT):
        return next(node for node in graph["nodes"] if node["id"] == "liquid:outpoint:" + outpoint)

    @staticmethod
    def snapshot(directory):
        return {str(path.relative_to(directory)): path.read_bytes()
                for path in directory.rglob("*") if path.is_file()}

    def test_real_traced_unspent_observation_highlights_only_terminal_output(self):
        state = self.run_trace()
        original = copy.deepcopy(state)
        record = state["outputs"][ENDPOINT]
        self.assertEqual(record["status"], "unspent_at_observation")
        self.assertIs(record["observed_spend"]["spent"], False)
        self.assertIs(type(record["spend_observation_id"]), int)
        self.assertEqual(state["stats"]["requests_this_run"], 6)
        with patch.object(Esplora, "get", side_effect=AssertionError("Rendering cannot fetch API data")):
            graph = build_graph(state)
        terminal = self.node(graph)
        self.assertEqual(terminal["role"], "unspent_endpoint")
        self.assertEqual(terminal["color"], "#fdba74")
        self.assertEqual(terminal["details"]["unspent_endpoints"], [ENDPOINT])
        self.assertIn("Unspent endpoint", terminal["label"].splitlines())
        self.assertEqual({node["id"] for node in graph["nodes"] if node.get("role") == "unspent_endpoint"},
                         {terminal["id"]})
        self.assertEqual(state, original)

    def test_unspent_seed_overrides_address_seed_color_but_not_starting_transaction(self):
        self.responses["/tx/" + A + "/outspends"][0] = {"spent": False}
        save_json(self.fixture_file, self.responses)
        state = self.run_trace()
        graph = build_graph(state)
        selected = self.node(graph, A + ":0")
        self.assertEqual(selected["role"], "unspent_endpoint")
        self.assertEqual(selected["color"], COLORS["unspent_endpoint"])
        starting = next(node for node in graph["nodes"] if node["id"] == "tx:" + A)
        self.assertEqual(starting["role"], "starting_transaction")
        self.assertEqual(starting["color"], COLORS["starting_transaction"])

    def test_unselected_sibling_false_in_archived_response_is_still_context(self):
        self.responses["/tx/" + A + "/outspends"][1] = {"spent": False}
        save_json(self.fixture_file, self.responses)
        state = self.run_trace()
        self.assertNotIn(A + ":1", state["outputs"])
        graph = build_graph(state)
        sibling = self.node(graph, A + ":1")
        self.assertEqual(sibling["role"], "address")
        self.assertEqual(sibling["color"], COLORS["address"])
        self.assertFalse(sibling["details"].get("unspent_endpoints"))
        self.assertNotIn("Unspent endpoint", sibling["label"])
        self.assertNotIn(X, state["transactions"])
        self.assertEqual(state["stats"]["requests_this_run"], 6)

    def test_nonterminal_or_unchecked_status_never_uses_stale_false_observation(self):
        state = self.run_trace()
        for status in ("pending", "hop_limit", "error", "interrupted", "analyst_stop",
                       "transaction_limit", "request_limit", "unconfirmed_funding", "unconfirmed_spend", "spent"):
            with self.subTest(status=status):
                variant = copy.deepcopy(state)
                variant["outputs"][ENDPOINT]["status"] = status
                node = self.node(build_graph(variant))
                self.assertEqual(node["role"], "candidate")
                self.assertFalse(node["details"].get("unspent_endpoints"))
                self.assertNotIn("Unspent endpoint", node["label"])
        boundary = self.run_trace(hops=2)
        self.assertEqual(boundary["outputs"][ENDPOINT]["status"], "hop_limit")
        self.assertEqual(self.node(build_graph(boundary))["role"], "candidate")

    def test_unspent_label_requires_false_boolean_and_an_observation_identifier(self):
        state = self.run_trace()
        variants = [{"observed_spend": value} for value in (
            None, [], {}, {"spent": 0}, {"spent": "false"}, {"spent": True})]
        variants += [{"spend_observation_id": value} for value in (None, "", 0, -1, False, True, [], {})]
        for replacement in variants:
            with self.subTest(replacement=replacement):
                variant = copy.deepcopy(state)
                variant["outputs"][ENDPOINT].update(replacement)
                node = self.node(build_graph(variant))
                self.assertEqual(node["role"], "candidate")
                self.assertFalse(node["details"].get("unspent_endpoints"))

    def test_saved_link_overrides_stale_unspent_record(self):
        state = self.run_trace()
        state["links"][ENDPOINT] = {"outpoint": ENDPOINT, "spending_txid": D, "vin": 0}
        node = self.node(build_graph(state))
        self.assertEqual(node["role"], "candidate")
        self.assertFalse(node["details"].get("unspent_endpoints"))

    def test_saved_context_spending_input_overrides_stale_unspent_record(self):
        state = self.run_trace()
        state["transactions"][D] = {"data": fixture()["/tx/" + D], "depth": 3, "observation_id": 100}
        self.assertNotIn(ENDPOINT, state["links"])
        graph = build_graph(state)
        node = self.node(graph)
        self.assertEqual(node["role"], "candidate")
        self.assertFalse(node["details"].get("unspent_endpoints"))
        incoming = next(edge for edge in graph["edges"] if edge["id"] == "in:" + D + ":0")
        self.assertEqual(incoming["role"], "context_input")

    def test_merged_mixed_spent_and_unspent_occurrences_are_order_independent(self):
        state = self.run_trace()
        state["seeds"].append(B + ":0")
        self.assertEqual(state["outputs"][B + ":0"]["status"], "spent")
        key = "liquid:address:SYNTHETIC-branch-A"
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                variant = copy.deepcopy(state)
                if reverse:
                    variant["transactions"] = dict(reversed(list(variant["transactions"].items())))
                    variant["outputs"] = dict(reversed(list(variant["outputs"].items())))
                node = next(node for node in build_graph(variant, True)["nodes"] if node["id"] == key)
                self.assertEqual(node["role"], "unspent_endpoint")
                self.assertEqual(node["color"], COLORS["unspent_endpoint"])
                self.assertEqual(node["details"]["unspent_endpoints"], [ENDPOINT])
                self.assertEqual({item["outpoint"] for item in node["details"]["occurrences"]}, {B + ":0", ENDPOINT})
                self.assertIn("Unspent endpoint", node["label"].splitlines())

    def test_merged_multiple_endpoints_are_deduplicated_and_sorted(self):
        self.responses["/tx/" + A]["vout"][1] = output("SYNTHETIC-branch-A")
        self.responses["/tx/" + A + "/outspends"][1] = {"spent": False}
        save_json(self.fixture_file, self.responses)
        state = self.run_trace(seeds=[A + ":0", A + ":1"])
        node = next(node for node in build_graph(state, True)["nodes"]
                    if node["id"] == "liquid:address:SYNTHETIC-branch-A")
        self.assertEqual(node["role"], "unspent_endpoint")
        self.assertEqual(node["details"]["unspent_endpoints"], sorted([A + ":1", ENDPOINT]))
        self.assertIn("Unspent endpoints: 2", node["label"].splitlines())

    def test_attribution_keeps_color_and_endpoint_label_and_evidence(self):
        state = self.run_trace()
        state["labels"] = [{"kind": "outpoint", "value": ENDPOINT, "entity": "Synthetic service",
                            "source": "case-record:synthetic", "confidence": "candidate", "observed_at": "2026-09-11"}]
        for merge in (False, True):
            with self.subTest(merge=merge):
                graph = build_graph(state, merge)
                node = next(node for node in graph["nodes"] if ENDPOINT in node["details"].get("unspent_endpoints", []))
                self.assertEqual(node["role"], "attributed")
                self.assertEqual(node["color"], COLORS["attributed"])
                self.assertIn("Unspent endpoint", node["label"].splitlines())
                self.assertIn("Synthetic service (candidate)", node["label"])

    def test_event_and_bitcoin_input_colors_are_not_terminal_output_colors(self):
        state = self.run_trace()
        for outpoint in (C + ":1", C + ":2"):
            state["outputs"][outpoint].update(status="unspent_at_observation", observed_spend={"spent": False},
                                               spend_observation_id=100)
        state["transactions"][D] = {"data": fixture()["/tx/" + D], "depth": 3, "observation_id": 101}
        state["transactions"][D]["data"]["vin"][0]["is_pegin"] = True
        graph = build_graph(state, include_fees=True)
        self.assertEqual(self.node(graph)["role"], "unspent_endpoint")
        bitcoin = next(node for node in graph["nodes"] if node["id"] == "bitcoin:outpoint:" + ENDPOINT)
        self.assertEqual(bitcoin["role"], "address")
        self.assertEqual(bitcoin["color"], COLORS["address"])
        self.assertFalse(bitcoin["details"].get("unspent_endpoints"))
        for node in graph["nodes"]:
            if node["kind"] == "event":
                self.assertEqual(node["color"], COLORS["event"])
                self.assertNotIn("Unspent endpoint", node["label"])

    def test_continuation_rechecks_spend_and_preserves_previous_export(self):
        # A fake transport lets the same source observe a later spend without
        # changing a fixture's immutable source digest or making HTTP requests.
        calls = []
        def transport(method, url, headers, body, timeout):
            self.assertEqual(method, "GET")
            endpoint = url.removeprefix("https://blockstream.info/liquid/api")
            calls.append(endpoint)
            return 200, {}, canonical(self.responses[endpoint])

        first = self.run_trace(transport=transport)
        archive = self.root / "first-export"
        export_run(self.store, first, archive)
        original = copy.deepcopy(first)
        before = self.snapshot(archive)
        self.assertEqual(self.node(build_graph(first))["role"], "unspent_endpoint")
        self.responses["/tx/" + C + "/outspends"][0] = fixture()["/tx/" + C + "/outspends"][0]
        second = self.run_trace(parent=first, transport=transport)
        self.assertEqual(second["outputs"][ENDPOINT]["status"], "spent")
        self.assertNotEqual(first["outputs"][ENDPOINT]["spend_observation_id"],
                            second["outputs"][ENDPOINT]["spend_observation_id"])
        continued_node = self.node(build_graph(second))
        self.assertEqual(continued_node["role"], "candidate")
        self.assertEqual(continued_node["color"], COLORS["candidate"])
        self.assertFalse(continued_node["details"].get("unspent_endpoints"))
        self.assertEqual(calls.count("/tx/" + C + "/outspends"), 2)
        self.assertEqual(first, original)
        self.assertEqual(self.snapshot(archive), before)

    def test_all_display_products_share_terminal_color_and_label(self):
        graph = build_graph(self.run_trace())
        node = self.node(graph)
        plan = make_plan(graph)
        shape = next(item for item in plan["shapes"] if item["key"] == node["id"])
        self.assertEqual(shape["body"]["style"]["fillColor"], COLORS["unspent_endpoint"])
        self.assertIn("Unspent endpoint", shape["body"]["data"]["content"])
        source = mermaid_source(graph)
        identifier = "n" + str(next(index for index, item in enumerate(sorted(graph["nodes"], key=lambda n: n["id"]))
                                    if item["id"] == node["id"]))
        self.assertIn(f"style {identifier} fill:{COLORS['unspent_endpoint']},", source)
        self.assertIn("Unspent endpoint", source)
        for svg, attribute in ((svg_graph(graph), "data-key"), (render_svg(graph), "data-node-id")):
            document = ET.fromstring(svg)
            group = next(element for element in document.iter() if element.get(attribute) == node["id"])
            self.assertTrue(any(child.get("fill") == COLORS["unspent_endpoint"] for child in group.iter()))
            self.assertIn("Unspent endpoint", " ".join(group.itertext()))

    @unittest.skipUnless(HAS_ELK, "local Node and pinned ELK dependency are required")
    def test_real_elk_keeps_terminal_evidence_and_fill(self):
        graph = build_graph(self.run_trace())
        before = copy.deepcopy(graph)
        optimized = optimize_graph(graph)
        self.assertEqual(optimized["layout"]["algorithm"], "elk_layered_v1")
        original = self.node(graph)
        node = self.node(optimized)
        for field in ("role", "color", "label", "details"):
            self.assertEqual(node[field], original[field])
        rendered = render_svg(optimized)
        self.assertIn(b'fill="#fdba74"', rendered)
        self.assertIn(b"Unspent endpoint", rendered)
        self.assertEqual(graph, before)


if __name__ == "__main__":
    unittest.main()
