"""Current graph preferences change presentation while archives remain intact."""

import contextlib
import copy
import csv
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from liquid_tracer.cli import csv_run, main, parser, saved_graph, verify_export
from liquid_tracer.common import read_json, save_json
from liquid_tracer.export import build_graph
from liquid_tracer.investigations import update_case
from tests.fixtures import A, X, fixture, output
from tests.test_context_groups import summaries
from tests.test_elk_layout import HAS_ELK
from tests.test_input_order import input_order_state
from tests.test_layout import txid


HUB_A = "G" + "a" * 33
HUB_B = "H" + "b" * 33
CONTEXT_C = "J" + "c" * 33


class GraphHubTests(unittest.TestCase):
    def test_exact_liquid_hubs_are_marked_before_grouping_without_evidence_changes(self):
        state = input_order_state(5, continuing=(4,))
        inputs = state["transactions"][txid("input-order-child")]["data"]["vin"]
        for index, address in enumerate((HUB_A, HUB_A.lower(), HUB_B, CONTEXT_C)):
            inputs[index]["prevout"]["scriptpubkey_address"] = address
        original = copy.deepcopy(state)
        supplied = [" " + HUB_B + " ", HUB_A, HUB_B]
        graph = build_graph(state, group_context_inputs=True, hub_addresses=supplied)
        self.assertEqual(graph["graph_options"]["hub_addresses"], [HUB_A, HUB_B])
        marked = [node for node in graph["nodes"] if node.get("layout_hub")]
        self.assertEqual({node["details"]["address"] for node in marked}, {HUB_A, HUB_B})
        group, = summaries(graph)
        self.assertEqual({node["details"]["address"] for node in group["details"]["members"]},
                         {HUB_A.lower(), CONTEXT_C})
        self.assertEqual(state, original)
        self.assertEqual(supplied, [" " + HUB_B + " ", HUB_A, HUB_B])

    def test_hub_setting_does_not_mark_bitcoin_or_change_trace_roles(self):
        state = input_order_state(3, continuing=(2,))
        inputs = state["transactions"][txid("input-order-child")]["data"]["vin"]
        for index in (0, 1):
            inputs[index]["prevout"]["scriptpubkey_address"] = HUB_A
        inputs[1]["is_pegin"] = True
        before = build_graph(state)
        graph = build_graph(state, hub_addresses=[HUB_A])
        matching = [node for node in graph["nodes"] if node.get("details", {}).get("address") == HUB_A]
        self.assertEqual(len(matching), 2)
        for node in matching:
            self.assertEqual(bool(node.get("layout_hub")), node["details"]["network"] == "liquid")
        self.assertEqual(graph["edges"], before["edges"])
        self.assertEqual({node["id"]: node.get("role") for node in graph["nodes"]},
                         {node["id"]: node.get("role") for node in before["nodes"]})

    def test_empty_hub_default_keeps_existing_graph_semantics(self):
        state = input_order_state()
        graph = build_graph(state)
        self.assertEqual(graph["graph_options"].get("hub_addresses", []), [])
        self.assertFalse(any(node.get("layout_hub") for node in graph["nodes"]))
        self.assertEqual(graph, build_graph(state, hub_addresses=[]))


class SavedGraphOptionsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.case = self.root / "case"
        data = fixture()
        data["/tx/" + A]["vin"] = [
            {"txid": X, "vout": index, "prevout": output(address),
             "is_coinbase": False, "is_pegin": False}
            for index, address in enumerate((HUB_A, HUB_B, CONTEXT_C))]
        self.fixture = self.root / "fixture.json"
        save_json(self.fixture, data)
        report = self.invoke(["trace", "--case", str(self.case), "--fixture", str(self.fixture),
                              "--seed", A + ":0", "--hops", "1"])
        self.archive = Path(report["directory"])

    def invoke(self, args):
        out, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(errors):
            status = main(args)
        self.assertEqual(status, 0, errors.getvalue() or out.getvalue())
        return json.loads(out.getvalue())

    @staticmethod
    def snapshot(directory):
        return {str(path.relative_to(directory)): path.read_bytes()
                for path in directory.rglob("*") if path.is_file()}

    def configure(self, enabled):
        update_case(self.case, {"run_defaults": {"group_context_inputs": enabled,
                                                "hub_addresses": [HUB_A]}})

    def test_saved_graph_reads_current_case_hubs_and_optional_grouping(self):
        original = self.snapshot(self.archive)
        self.configure(True)
        case_before = self.snapshot(self.case)
        with patch("liquid_tracer.cli.Esplora", side_effect=AssertionError("Must not retrace")):
            run_id, archive, graph = saved_graph(self.case)
        self.assertEqual(archive, self.archive)
        self.assertEqual(run_id, read_json(self.archive / "trace.json")["run_id"])
        group, = summaries(graph)
        self.assertEqual(group["details"]["address_count"], 2)
        self.assertEqual(graph["graph_options"]["hub_addresses"], [HUB_A])
        hub, = [node for node in graph["nodes"] if node.get("layout_hub")]
        self.assertEqual(hub["details"]["address"], HUB_A)
        self.assertEqual(self.snapshot(self.case), case_before)
        self.assertEqual(self.snapshot(self.archive), original)
        verify_export(self.archive)

    def test_action_overrides_do_not_persist_or_rewrite_archive(self):
        original = self.snapshot(self.archive)
        self.configure(True)
        metadata = (self.case / "case.json").read_bytes()
        self.assertFalse(summaries(saved_graph(self.case, group_context_inputs=False)[2]))
        self.assertTrue(summaries(saved_graph(self.case)[2]))
        self.assertEqual((self.case / "case.json").read_bytes(), metadata)
        self.configure(False)
        metadata = (self.case / "case.json").read_bytes()
        self.assertTrue(summaries(saved_graph(self.case, group_context_inputs=True)[2]))
        self.assertFalse(summaries(saved_graph(self.case)[2]))
        self.assertEqual((self.case / "case.json").read_bytes(), metadata)
        self.assertEqual(self.snapshot(self.archive), original)

    def test_csv_export_keeps_all_individual_inputs_when_grouping_is_enabled(self):
        self.configure(True)
        original = self.snapshot(self.archive)
        self.assertTrue(summaries(saved_graph(self.case)[2]))
        destination = self.root / "csv-output"
        with patch("liquid_tracer.cli.Esplora", side_effect=AssertionError("Must not retrace")):
            csv_run(self.case, out=destination)
        with (destination / "transactions.csv").open(newline="") as stream:
            rows = list(csv.DictReader(stream))
        # Address values and original indexes must survive the display setting.
        text = (destination / "transactions.csv").read_text()
        for address in (HUB_A, HUB_B, CONTEXT_C):
            self.assertIn(address, text)
        self.assertNotIn("context-group:", text)
        self.assertEqual(len(rows), len(saved_graph(self.case, group_context_inputs=False)[2]["edges"]))
        selected = [row for row in rows if row["Transaction Hash"] == A and row["Direction"] == "IN"]
        self.assertEqual([(row["Number of I/O"], row["Address Hash"]) for row in selected],
                         [(str(index), address) for index, address in enumerate((HUB_A, HUB_B, CONTEXT_C))])
        self.assertEqual(self.snapshot(self.archive), original)
        verify_export(self.archive)

    def test_cli_flags_are_three_state_and_mutually_exclusive(self):
        for command in ("layout-preview", "compact-preview", "miro-sync"):
            args = [command, "--case", str(self.case)]
            with self.subTest(command=command):
                self.assertIsNone(parser().parse_args(args).group_context_inputs)
                self.assertIs(parser().parse_args(args + ["--group-context-inputs"]).group_context_inputs, True)
                self.assertIs(parser().parse_args(args + ["--ungroup-context-inputs"]).group_context_inputs, False)
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    parser().parse_args(args + ["--group-context-inputs", "--ungroup-context-inputs"])

    @unittest.skipUnless(HAS_ELK, "Run liquid-layout-setup to install the pinned local ELK engine")
    def test_preview_cli_applies_group_and_ungroup_flags_only_to_that_export(self):
        original = self.snapshot(self.archive)
        for default, flag, expected in ((False, "--group-context-inputs", True),
                                        (True, "--ungroup-context-inputs", False)):
            self.configure(default)
            metadata = (self.case / "case.json").read_bytes()
            destination = self.root / ("grouped-preview" if expected else "individual-preview")
            with self.subTest(flag=flag), \
                    patch("liquid_tracer.cli.ensure_graph_counts", return_value={"updated": 0}), \
                    patch("liquid_tracer.cli.Esplora", side_effect=AssertionError("Must not retrace")):
                report = self.invoke(["layout-preview", "--case", str(self.case),
                                      "--out", str(destination), flag])
                graph = read_json(destination / "graph.json")
                self.assertIs(report["group_context_inputs"], expected)
                self.assertEqual(bool(summaries(graph)), expected)
                self.assertEqual(report["hub_addresses"], [HUB_A])
                self.assertEqual((self.case / "case.json").read_bytes(), metadata)
                self.assertEqual(bool(summaries(saved_graph(self.case)[2])), default)
        self.assertEqual(self.snapshot(self.archive), original)


if __name__ == "__main__":
    unittest.main()
