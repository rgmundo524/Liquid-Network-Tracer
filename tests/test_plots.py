"""Shared saved-evidence plotting, offline boundaries, and reviewed artifacts."""
from copy import deepcopy
import csv
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

from liquid_tracer.common import LBTC, TraceError, canonical, digest, read_json, save_json
from liquid_tracer.investigations import create_investigation, read_case, update_case
from liquid_tracer.plots import FILES, INPUT_SNAPSHOT_FILES, PEGOUT_CSV_FILES, list_plots, plot_files, preview_plot, reviewed_plot
from liquid_tracer.services import set_service
from tests.test_attribution_convergence import graph_state, tx
from tests.test_connections import saved_case
from tests.test_pegout_paths import add_pegout


class PlotTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.case = create_investigation(self.root, "Plot workspace", seeds=[tx("f") + ":0"],
                                         run_defaults={"layout_attempts": 1})
        state = graph_state((("a:0", "c"), ("c:0", "b")), seeds=("a:0", "b:0"))
        self.endpoint = add_pegout(state, tx("b"))
        self.state, self.archive = saved_case(self.case, state)
        layout = patch("liquid_tracer.elk_layout.optimize_graph", side_effect=lambda graph, **kwargs: graph)
        self.layout = layout.start()
        self.addCleanup(layout.stop)
        for name in ("liquid_tracer.api.Esplora.get", "liquid_tracer.address_counts.ensure_counts",
                     "liquid_tracer.miro.publish", "liquid_tracer.miro.sync"):
            blocker = patch(name, side_effect=AssertionError("Plots must not fetch or publish"))
            blocker.start()
            self.addCleanup(blocker.stop)

    def bytes(self, root):
        return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}

    def rewrite_archive(self, state):
        save_json(self.archive / "trace.json", state)
        names = [line.split("  ", 1)[1] for line in (self.archive / "SHA256SUMS").read_text().splitlines()]
        (self.archive / "SHA256SUMS").write_text("".join(digest((self.archive / name).read_bytes()) + "  " + name + "\n" for name in names))

    def rehash_preview(self, directory):
        (directory / "SHA256SUMS").write_text("".join(digest((directory / name).read_bytes()) + "  " + name + "\n"
                                                    for name in sorted(plot_files(directory) - {"SHA256SUMS"})))

    def legacy_inputs(self, directory, graph):
        for key in ("input_snapshot_version", "input_snapshot_at", "inputs_sha256"):
            graph["plot"].pop(key, None)
        (directory / "inputs.json").unlink()

    def test_three_goals_share_saved_evidence_without_fetching_or_archive_mutation(self):
        before = self.bytes(self.archive)
        case_before = (self.case / "case.json").read_bytes()
        for goal in ("full", "connections", "pegouts"):
            with self.subTest(goal=goal):
                result = preview_plot(self.case, goal, max_hops=2)
                graph, plan = reviewed_plot(self.case, result["preview_id"])
                self.assertEqual(result["goal"], goal)
                self.assertEqual(graph["namespace"], plan["namespace"])
                self.assertEqual(plan["schema_version"], 2)
                self.assertEqual(graph["plot"], read_json(Path(result["directory"]) / "plot.json"))
                self.assertEqual(set(path.name for path in Path(result["directory"]).iterdir()),
                                 FILES | INPUT_SNAPSHOT_FILES | (PEGOUT_CSV_FILES if goal == "pegouts" else set()))
                self.assertTrue(result["saved_data_only"])
                self.assertEqual(result["source_max_hops"], 10)
                self.assertEqual(result["source_run_status"], "bounded_complete")
                self.assertIn("Saved-data-only", result["notice"])
        self.assertEqual(len(list_plots(self.case)), 3)
        self.assertEqual(before, self.bytes(self.archive))
        self.assertEqual(case_before, (self.case / "case.json").read_bytes())
        self.assertFalse((self.case / "miro").exists())

    def test_pegouts_use_selected_archive_seeds_not_changed_case_seeds(self):
        result = preview_plot(self.case, "pegouts", min_hops=2, max_hops=2)
        graph, _ = reviewed_plot(self.case, result["preview_id"])
        self.assertEqual(result["query"], {"seeds": sorted(self.state["seeds"]), "min_hops": 2,
                                          "max_hops": 2, "transaction_io": "complete", "attribution_hop_limits": "ignore"})
        self.assertEqual((result["min_hops"], result["max_hops"]), (2, 2))
        self.assertEqual([match["outpoint"] for match in graph["pegouts"]["matches"]], [self.endpoint])
        metadata = read_case(self.case)
        metadata["seeds"] = [tx("e") + ":0"]
        save_json(self.case / "case.json", metadata)
        reviewed_plot(self.case, result["preview_id"])

    def test_pegout_path_exports_are_scoped_manifested_and_detect_modified_bytes(self):
        state = graph_state((("a:0", "c"), ("a:1", "d")), seeds=("a:0",), raw_links=(("e:0", "c"),))
        endpoint = add_pegout(state, tx("c"))
        add_pegout(state, tx("d"))
        self.state, self.archive = saved_case(self.case, state)
        result = preview_plot(self.case, "pegouts", max_hops=1, include_context=True)
        directory = Path(result["directory"])
        reviewed_plot(self.case, result["preview_id"])
        with (directory / "path-transactions.csv").open(newline="") as stream:
            paths = list(csv.DictReader(stream))
        with (directory / "trace-endpoints.csv").open(newline="") as stream:
            endpoints = list(csv.DictReader(stream))
        self.assertEqual({row["Transaction Hash"] for row in paths}, {tx("a"), tx("c")})
        self.assertEqual([row["Outpoint"] for row in endpoints], [endpoint])
        self.assertNotIn(tx("e") + ":0", {value for row in paths for value in row.values()})
        self.assertEqual(set(line.split("  ")[1] for line in (directory / "SHA256SUMS").read_text().splitlines()),
                         (FILES | INPUT_SNAPSHOT_FILES | PEGOUT_CSV_FILES) - {"SHA256SUMS"})
        with (directory / "trace-endpoints.csv").open("a") as stream:
            stream.write("changed\n")
        with self.assertRaisesRegex(TraceError, "changed"):
            reviewed_plot(self.case, result["preview_id"])

    def test_older_saved_pegout_plots_remain_reviewable_without_new_csv_exports(self):
        result = preview_plot(self.case, "pegouts")
        directory = Path(result["directory"])
        graph = read_json(directory / "graph.json")
        self.legacy_inputs(directory, graph)
        graph["plot"].pop("csv_export_version")
        save_json(directory / "graph.json", graph)
        save_json(directory / "plot.json", graph["plot"])
        for name in PEGOUT_CSV_FILES:
            (directory / name).unlink()
        self.rehash_preview(directory)
        self.assertEqual(plot_files(directory), FILES)
        reviewed_plot(self.case, result["preview_id"])
        self.assertTrue(list_plots(self.case)[0]["reviewable"])

    def test_markerless_pegout_snapshots_keep_original_io_and_exports_after_new_plots(self):
        from liquid_tracer.plot_csv import build_plot_csv
        from liquid_tracer.plots import _query

        state = graph_state((("a:0", "b"),), seeds=("a:0",), raw_links=(("e:0", "b"), ("f:0", "b")))
        add_pegout(state, tx("b"))
        state["transactions"][tx("b")]["data"]["vout"].append(
            {"scriptpubkey": "", "scriptpubkey_type": "fee", "asset": LBTC, "value": 1})
        self.state, self.archive = saved_case(self.case, state)
        update_case(self.case, {"run_defaults": {"group_context_inputs": True}})

        def legacy_query(*args, **kwargs):
            return _query(*args, **kwargs, transaction_io=None, attribution_hop_limits=None)

        old = []
        for include_context in (False, True):
            with patch("liquid_tracer.plots._query", side_effect=legacy_query):
                result = preview_plot(self.case, "pegouts", include_context=include_context)
            graph, plan = reviewed_plot(self.case, result["preview_id"])
            self.assertNotIn("transaction_io", result["query"])
            self.assertFalse(graph["include_fees"])
            self.assertEqual(graph["graph_options"]["group_context_inputs"], include_context)
            old.append((result, graph, plan, self.bytes(Path(result["directory"])),
                        build_plot_csv(self.case, result["preview_id"], "endpoints.csv")["data"]))
        fresh = preview_plot(self.case, "pegouts", include_context=False)
        new_graph, _ = reviewed_plot(self.case, fresh["preview_id"])
        self.assertEqual(fresh["query"]["transaction_io"], "complete")
        self.assertTrue(new_graph["include_fees"])
        self.assertTrue(new_graph["graph_options"]["group_context_inputs"])
        for result, graph, plan, snapshot, endpoints in old:
            self.assertEqual(reviewed_plot(self.case, result["preview_id"]), (graph, plan))
            self.assertEqual(self.bytes(Path(result["directory"])), snapshot)
            self.assertEqual(build_plot_csv(self.case, result["preview_id"], "endpoints.csv")["data"], endpoints)
            self.assertEqual(build_plot_csv(self.case, fresh["preview_id"], "endpoints.csv")["data"], endpoints)
        self.assertTrue(all(item["reviewable"] for item in list_plots(self.case)))

    def test_new_pegout_exports_are_required_even_for_empty_results(self):
        result = preview_plot(self.case, "pegouts", max_hops=0)
        directory = Path(result["directory"])
        for name in PEGOUT_CSV_FILES:
            with (directory / name).open(newline="") as stream:
                reader = csv.DictReader(stream)
                self.assertTrue(reader.fieldnames)
                self.assertEqual(list(reader), [])
        (directory / "trace-endpoints.csv").unlink()
        with self.assertRaisesRegex(TraceError, "incomplete"):
            reviewed_plot(self.case, result["preview_id"])

    def test_endpoint_observation_time_comes_from_the_verified_run_archive(self):
        state = graph_state((("a:0", "b"),), seeds=("a:0",))
        state["outputs"][tx("b") + ":0"].update(status="unspent_at_observation",
                observed_spend={"spent": False}, spend_observation_id=77)
        self.state, self.archive = saved_case(self.case, state)
        stamp = "2026-09-30T19:00:00Z"
        save_json(self.archive / "evidence-index.json", [{"id": 77, "source": self.state["source"],
            "endpoint": "/tx/" + tx("b") + "/outspends", "fetched_at": stamp, "status": 200}])
        with (self.archive / "SHA256SUMS").open("a") as stream:
            stream.write(digest((self.archive / "evidence-index.json").read_bytes()) + "  evidence-index.json\n")
        with patch("liquid_tracer.store.Store", side_effect=AssertionError("Do not open the mutable evidence store")):
            result = preview_plot(self.case, "pegouts", include_unspent=True)
        with (Path(result["directory"]) / "trace-endpoints.csv").open(newline="") as stream:
            endpoints = list(csv.DictReader(stream))
        self.assertEqual(endpoints[0]["Status"], "Dormant")
        self.assertEqual(endpoints[0]["Spend Observed At"], stamp)
        self.assertNotEqual(endpoints[0]["Time"], stamp)

    def test_unselected_sibling_paths_and_context_spends_do_not_become_matches(self):
        state = graph_state((("a:0", "c"), ("a:1", "d")), seeds=("a:0",), raw_links=(("a:2", "e"),))
        selected = add_pegout(state, tx("c"))
        add_pegout(state, tx("d"))
        add_pegout(state, tx("e"))
        self.state, self.archive = saved_case(self.case, state)
        result = preview_plot(self.case, "pegouts", max_hops=1)
        graph, _ = reviewed_plot(self.case, result["preview_id"])
        self.assertEqual([match["outpoint"] for match in graph["pegouts"]["matches"]], [selected])
        self.assertEqual(graph["pegouts"]["outpoints"], [tx("a") + ":0"])

    def test_pegout_artifacts_share_one_address_without_collapsing_utxo_rows(self):
        state = graph_state((("a:0", "c"), ("a:1", "d")), seeds=("a:0", "a:1"),
                            raw_links=(("a:2", "c"),))
        endpoints = {add_pegout(state, tx("c")), add_pegout(state, tx("d"))}
        self.state, self.archive = saved_case(self.case, state)
        before = self.bytes(self.archive)
        result = preview_plot(self.case, "pegouts", max_hops=1)
        graph, plan = reviewed_plot(self.case, result["preview_id"])
        directory = Path(result["directory"])
        addresses = [node for node in graph["nodes"] if node["kind"] == "address"]
        self.assertEqual(len({node["details"]["address"] for node in addresses}), len(addresses))
        address, = [node for node in addresses if node["details"]["address"] == "SYNTHETIC-a-address"]
        self.assertEqual(address["id"], "liquid:address:SYNTHETIC-a-address")
        self.assertEqual({item["outpoint"] for item in address["details"]["occurrences"]},
                         {tx("a") + ":0", tx("a") + ":1", tx("a") + ":2"})
        self.assertEqual({row["outpoint"] for row in graph["pegouts"]["matches"]}, endpoints)
        self.assertEqual(graph["namespace"]["address_mode"], "merged")
        self.assertEqual(sum(item["key"] == address["id"] for item in plan["shapes"]), 1)
        self.assertEqual(len(plan["connectors"]), 10)
        with (directory / "transactions.csv").open(newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(len(rows), len(graph["edges"]))
        shared_rows = {(row["Transaction Hash"], row["Direction"], row["Number of I/O"])
                       for row in rows if row["Address Hash"] == "SYNTHETIC-a-address"}
        self.assertEqual(shared_rows, {(tx("a"), "OUT", "0"), (tx("a"), "OUT", "1"),
                                       (tx("a"), "OUT", "2"), (tx("c"), "IN", "1"),
                                       (tx("c"), "IN", "0"), (tx("d"), "IN", "0")})
        svg = ET.fromstring((directory / "graph.svg").read_bytes())
        self.assertEqual(sum(item.get("data-node-id") == address["id"] for item in svg.iter()), 1)
        self.assertIn("Sharing a circle does not establish a spend", (directory / "graph.html").read_text())
        self.assertEqual(before, self.bytes(self.archive))

    def test_empty_results_remain_reviewable_with_no_layout_or_miro_objects(self):
        self.state, self.archive = saved_case(self.case, graph_state())
        for goal in ("connections", "pegouts"):
            with self.subTest(goal=goal):
                result = preview_plot(self.case, goal, max_hops=0)
                graph, plan = reviewed_plot(self.case, result["preview_id"])
                self.assertTrue(result["empty"])
                self.assertEqual(graph["nodes"], [])
                self.assertEqual(plan["shapes"], [])
                self.assertEqual(plan["connectors"], [])
                self.assertIn("No matching activity", Path(result["html"]).read_text())
        self.layout.assert_not_called()

    def test_context_roundtrips_exports_without_expanding_paths_or_mutating_evidence(self):
        state = graph_state((("a:0", "c"), ("a:1", "d")), seeds=("a:0",), raw_links=(("e:0", "c"),))
        selected = add_pegout(state, tx("c"))
        add_pegout(state, tx("d"))
        state["transactions"][tx("c")]["data"]["vout"].extend([
            {"scriptpubkey": "6a", "scriptpubkey_type": "op_return", "asset": LBTC, "value": 10},
            {"scriptpubkey": "", "scriptpubkey_type": "fee", "asset": LBTC, "value": 1}])
        self.state, self.archive = saved_case(self.case, state)
        before = self.bytes(self.archive)
        default = preview_plot(self.case, "pegouts", min_hops=1, max_hops=1)
        original, original_plan = reviewed_plot(self.case, default["preview_id"])
        self.assertNotIn("include_context", default["query"])
        self.assertEqual(default["query"]["transaction_io"], "complete")
        self.assertEqual(default["context_edge_count"], 5)
        result = preview_plot(self.case, "pegouts", min_hops=1, max_hops=1, include_context=True)
        graph, plan = reviewed_plot(self.case, result["preview_id"])
        self.assertEqual(result["query"], {**default["query"], "include_context": True})
        self.assertEqual(result["context_edge_count"], 5)
        self.assertEqual(graph["pegouts"]["context_edge_count"], 5)
        self.assertEqual(graph["pegouts"]["matches"], original["pegouts"]["matches"])
        self.assertEqual(graph["pegouts"]["outpoints"], original["pegouts"]["outpoints"])
        self.assertEqual([item["outpoint"] for item in graph["pegouts"]["matches"]], [selected])
        self.assertEqual(result["match_count"], default["match_count"])
        self.assertEqual(result["transaction_count"], default["transaction_count"])
        self.assertEqual(plan["namespace"], original_plan["namespace"])
        self.assertFalse(graph["graph_options"]["group_context_inputs"])
        context = {edge["id"]: edge["role"] for edge in graph["edges"] if edge["role"].startswith("context")}
        self.assertEqual(context, {"out:" + tx("a") + ":1": "context_output",
                                   "in:" + tx("c") + ":1": "context_input",
                                   "out:" + tx("c") + ":0": "context_output",
                                   "out:" + tx("c") + ":2": "context_output",
                                   "out:" + tx("c") + ":3": "context_output"})
        self.assertEqual({edge["id"] for edge in graph["edges"]},
                         {edge["id"] for edge in original["edges"]})
        directory = Path(result["directory"])
        with (directory / "transactions.csv").open(newline="") as stream:
            rows = list(csv.DictReader(stream))
        context_rows = {(row["Transaction Hash"], row["Direction"], row["Number of I/O"])
                        for row in rows if "CONTEXT" in row["Address Flags"]}
        self.assertEqual(context_rows, {(tx("a"), "OUT", "1"), (tx("c"), "IN", "1"),
                                       (tx("c"), "OUT", "0"), (tx("c"), "OUT", "2"), (tx("c"), "OUT", "3")})
        self.assertEqual(len(rows), len(graph["edges"]))
        svg = ET.fromstring((directory / "graph.svg").read_bytes())
        self.assertEqual({node.get("data-node-id") for node in svg.iter() if node.get("data-node-id")},
                         {node["id"] for node in graph["nodes"]})
        self.assertEqual(before, self.bytes(self.archive))
        self.assertTrue(all(item["reviewable"] for item in list_plots(self.case)))

    def test_changed_saved_context_count_is_rejected_even_when_rehashed(self):
        result = preview_plot(self.case, "pegouts", include_context=True)
        directory = Path(result["directory"])
        graph = read_json(directory / "graph.json")
        graph["plot"]["context_edge_count"] += 1
        save_json(directory / "graph.json", graph)
        save_json(directory / "plot.json", graph["plot"])
        self.rehash_preview(directory)
        with self.assertRaisesRegex(TraceError, "endpoint options"):
            reviewed_plot(self.case, result["preview_id"])

    def test_optional_endpoints_roundtrip_snapshot_exports_and_shared_board_identity(self):
        state = graph_state((("a:0", "b"),), seeds=("a:0",))
        state["outputs"][tx("b") + ":0"].update(status="unspent_at_observation",
                observed_spend={"spent": False}, spend_observation_id=1)
        pegout = add_pegout(state, tx("b"))
        state["transactions"][tx("b")]["data"]["vout"].extend([
            {"scriptpubkey": "6a", "scriptpubkey_type": "op_return", "asset": LBTC, "value": 10},
            {"scriptpubkey": "", "scriptpubkey_type": "fee", "asset": LBTC, "value": 1}])
        self.state, self.archive = saved_case(self.case, state)
        before = self.bytes(self.archive)
        default = preview_plot(self.case, "pegouts", min_hops=1, max_hops=1)
        _, default_plan = reviewed_plot(self.case, default["preview_id"])
        self.assertNotIn("include_unspent", default["query"])
        self.assertNotIn("include_unspendable", default["query"])
        result = preview_plot(self.case, "pegouts", min_hops=1, max_hops=1,
                              include_unspent=True, include_unspendable=True)
        graph, plan = reviewed_plot(self.case, result["preview_id"])
        self.assertEqual(result["endpoint_count"], 3)
        self.assertEqual(result["endpoint_counts"], {"pegout": 1, "unspent": 1, "unspendable": 1})
        self.assertEqual(result["match_count"], 1)
        self.assertEqual(result["query"], {"seeds": state["seeds"], "min_hops": 1, "max_hops": 1,
                                          "include_unspent": True, "include_unspendable": True,
                                          "transaction_io": "complete", "attribution_hop_limits": "ignore"})
        self.assertEqual(graph["graph_options"]["pegout_query"], result["query"])
        self.assertEqual([item["outpoint"] for item in graph["pegouts"]["matches"]], [pegout])
        self.assertEqual(plan["namespace"], default_plan["namespace"])
        expected = {"out:" + tx("a") + ":0", "in:" + tx("b") + ":0",
                    *("out:" + tx("b") + ":" + str(index) for index in range(4))}
        self.assertEqual({edge["id"] for edge in graph["edges"]}, expected)
        directory = Path(result["directory"])
        self.assertEqual(read_json(directory / "plot.json"), graph["plot"])
        with (directory / "transactions.csv").open(newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(len(rows), len(expected))
        svg = ET.fromstring((directory / "graph.svg").read_bytes())
        self.assertEqual({node.get("data-node-id") for node in svg.iter() if node.get("data-node-id")},
                         {node["id"] for node in graph["nodes"]})
        self.assertEqual(before, self.bytes(self.archive))
        self.assertTrue(all(item["reviewable"] for item in list_plots(self.case)))

    def test_endpoint_only_result_is_nonempty_without_counting_a_pegout(self):
        state = graph_state((("a:0", "b"),), seeds=("a:0",))
        state["outputs"][tx("b") + ":0"].update(status="unspent_at_observation",
                observed_spend={"spent": False}, spend_observation_id=1)
        self.state, self.archive = saved_case(self.case, state)
        result = preview_plot(self.case, "pegouts", include_unspent=True)
        graph, plan = reviewed_plot(self.case, result["preview_id"])
        self.assertEqual(result["status"], "endpoints_found")
        self.assertEqual(result["match_count"], 0)
        self.assertEqual(result["endpoint_count"], 1)
        self.assertEqual(result["endpoint_counts"], {"pegout": 0, "unspent": 1, "unspendable": 0})
        self.assertFalse(result["empty"])
        self.assertEqual(graph["pegouts"]["matches"], [])
        self.assertEqual(len(plan["connectors"]), 3)

    def test_endpoint_options_reject_nonboolean_values_and_nonpegout_goals(self):
        for key in ("include_unspent", "include_unspendable", "include_context"):
            for value in (None, 0, 1, "true", [], {}):
                with self.subTest(option=key, value=value), self.assertRaises(TraceError):
                    preview_plot(self.case, "pegouts", **{key: value})
            for goal in ("full", "connections"):
                with self.subTest(option=key, goal=goal), self.assertRaises(TraceError):
                    preview_plot(self.case, goal, **{key: True})

    def test_changed_saved_endpoint_query_or_counts_are_rejected_even_when_rehashed(self):
        result = preview_plot(self.case, "pegouts", include_unspent=True)
        directory = Path(result["directory"])
        original = read_json(directory / "graph.json")
        for change in ("flag", "count", "counts", "status"):
            graph = deepcopy(original)
            if change == "flag":
                graph["plot"]["query"]["include_unspendable"] = True
            elif change == "count":
                graph["plot"]["endpoint_count"] += 1
            elif change == "counts":
                graph["plot"]["endpoint_counts"]["unspent"] += 1
            else:
                graph["plot"]["status"] = "endpoints_found"
            save_json(directory / "graph.json", graph)
            save_json(directory / "plot.json", graph["plot"])
            self.rehash_preview(directory)
            with self.subTest(change=change), self.assertRaisesRegex(TraceError, "endpoint options"):
                reviewed_plot(self.case, result["preview_id"])

    def test_partial_collection_reports_coverage_without_claiming_search_complete(self):
        state = deepcopy(self.state)
        state.update(status="paused", stop_reason="request_limit")
        state["limits"]["max_hops"] = 3
        self.rewrite_archive(state)
        result = preview_plot(self.case, "pegouts", max_hops=10)
        self.assertEqual(result["source_max_hops"], 3)
        self.assertEqual(result["source_run_status"], "paused")
        self.assertEqual(result["source_stop_reason"], "request_limit")
        self.assertIn("hop limit 3", result["coverage_notice"])
        self.assertIn("request_limit", result["coverage_notice"])
        self.assertEqual(result["max_hops"], 10)

    def test_changed_controls_apply_to_new_plots_while_saved_snapshot_stays_reviewable(self):
        result = preview_plot(self.case, "pegouts")
        original = reviewed_plot(self.case, result["preview_id"])
        set_service(self.case, "SYNTHETIC-c-address", name="Stop", stop_tracing=True)
        self.assertEqual(reviewed_plot(self.case, result["preview_id"]), original)
        listed = list_plots(self.case)
        self.assertEqual(len(listed), 1)
        self.assertTrue(listed[0]["reviewable"])
        fresh = preview_plot(self.case, "pegouts")
        self.assertTrue(fresh["empty"])

    def test_saved_layout_keeps_its_settings_after_current_preferences_change(self):
        update_case(self.case, {"run_defaults": {"include_fees": True, "group_context_inputs": True,
                                                "center_name": "Example", "connector_style": "elbowed"}})
        result = preview_plot(self.case, "full")
        graph, _ = reviewed_plot(self.case, result["preview_id"])
        self.assertTrue(graph["graph_options"]["group_context_inputs"])
        self.assertTrue(graph["include_fees"])
        self.assertEqual(graph["graph_options"]["center_name"], "Example")
        self.assertEqual(self.layout.call_args.kwargs["connector_style"], "elbowed")
        snapshot = deepcopy(result["layout_settings"])
        before = self.bytes(Path(result["directory"]))
        update_case(self.case, {"run_defaults": {"connector_style": "straight", "layout_attempts": 3,
                                                "color_attribution_arrows": True,
                                                "hub_addresses": ["H" * 34]}})
        old_graph, _ = reviewed_plot(self.case, result["preview_id"])
        self.assertEqual(old_graph, graph)
        self.assertEqual(old_graph["plot"]["layout_settings"], snapshot)
        self.assertEqual(self.bytes(Path(result["directory"])), before)
        fresh = preview_plot(self.case, "full")
        self.assertEqual(fresh["layout_settings"]["connector_style"], "straight")
        self.assertEqual(fresh["layout_settings"]["layout_attempts"], 3)
        self.assertFalse(fresh["layout_settings"]["include_fees"])
        listed = list_plots(self.case)
        self.assertEqual(len(listed), 2)
        self.assertTrue(all(item["reviewable"] for item in listed))
        self.assertEqual({item["layout_settings"]["connector_style"] for item in listed}, {"straight", "elbowed"})

    def test_filtered_layout_snapshot_masks_full_graph_options(self):
        update_case(self.case, {"run_defaults": {"include_fees": True, "group_context_inputs": True,
                                                "hub_addresses": ["H" * 34], "center_name": "Example",
                                                "color_attribution_arrows": True, "layout_attempts": 3}})
        for goal in ("connections", "pegouts"):
            with self.subTest(goal=goal):
                result = preview_plot(self.case, goal)
                graph, _ = reviewed_plot(self.case, result["preview_id"])
                settings = result["layout_settings"]
                self.assertEqual(settings["include_fees"], goal == "pegouts")
                self.assertEqual(settings["group_context_inputs"], goal == "pegouts")
                self.assertEqual(settings["hub_addresses"], [])
                self.assertEqual(settings["center_name"], "Example")
                self.assertTrue(settings["color_attribution_arrows"])
                if goal == "connections":
                    self.assertFalse(any(edge["role"].startswith("context") for edge in graph["edges"]))
                self.assertEqual(graph["include_fees"], goal == "pegouts")
                self.assertFalse(any(node["kind"] == "context_group" for node in graph["nodes"]))

    def test_context_grouping_effective_settings_follow_goal_and_saved_query(self):
        from liquid_tracer.plots import _effective_settings, _settings, _snapshot_settings

        update_case(self.case, {"run_defaults": {"include_fees": True, "group_context_inputs": True,
                                                "hub_addresses": ["H" * 34]}})
        preferences = _settings(read_case(self.case))
        for goal in ("full", "connections", "pegouts"):
            for query in (None, {}, {"include_context": False}, {"include_context": True},
                          {"transaction_io": "complete"}, {"transaction_io": "complete", "include_context": False}):
                with self.subTest(goal=goal, query=query):
                    settings = _effective_settings(preferences, goal, query)
                    complete = goal == "pegouts" and bool(query and query.get("transaction_io") == "complete")
                    expected_grouping = goal == "full" or complete or (goal == "pegouts" and bool(query and query.get("include_context")))
                    self.assertEqual(settings["group_context_inputs"], expected_grouping)
                    self.assertEqual(settings["include_fees"], goal == "full" or complete)
                    self.assertEqual(settings["hub_addresses"], ["H" * 34] if goal == "full" else [])
                    graph = {"plot": {"goal": goal, "query": query, "layout_settings": settings,
                                       "settings_sha256": digest(canonical(settings))},
                             "graph_options": deepcopy(settings), "presentation_version": settings["presentation_version"]}
                    self.assertEqual(_snapshot_settings(graph), settings)
        self.assertTrue(preferences["include_fees"])
        self.assertTrue(preferences["group_context_inputs"])
        self.assertEqual(preferences["hub_addresses"], ["H" * 34])

    def test_pegout_grouping_roundtrips_original_csv_and_preserves_older_context_layout(self):
        state = graph_state((("a:0", "c"),), seeds=("a:0",), raw_links=(("e:0", "c"), ("f:0", "c")))
        add_pegout(state, tx("c"))
        self.state, self.archive = saved_case(self.case, state)
        before = self.bytes(self.archive)
        original = preview_plot(self.case, "pegouts", min_hops=1, max_hops=1, include_context=True)
        old_graph, old_plan = reviewed_plot(self.case, original["preview_id"])
        old_directory = Path(original["directory"])
        old_bytes = self.bytes(old_directory)
        self.assertFalse(original["layout_settings"]["group_context_inputs"])
        update_case(self.case, {"run_defaults": {"group_context_inputs": True, "include_fees": True,
                                                "hub_addresses": ["H" * 34]}})
        result = preview_plot(self.case, "pegouts", min_hops=1, max_hops=1, include_context=True)
        graph, plan = reviewed_plot(self.case, result["preview_id"])
        summary, = [node for node in graph["nodes"] if node["kind"] == "context_group"]
        self.assertEqual(summary["details"]["address_count"], 2)
        self.assertTrue(result["layout_settings"]["group_context_inputs"])
        self.assertTrue(graph["graph_options"]["group_context_inputs"])
        self.assertTrue(result["layout_settings"]["include_fees"])
        self.assertEqual(result["layout_settings"]["hub_addresses"], [])
        self.assertEqual(graph["pegouts"], old_graph["pegouts"])
        self.assertEqual(plan["namespace"], old_plan["namespace"])
        self.assertEqual(len(plan["connectors"]), len(old_plan["connectors"]))
        with (Path(result["directory"]) / "transactions.csv").open(newline="") as stream:
            rows = list(csv.DictReader(stream))
        with (old_directory / "transactions.csv").open(newline="") as stream:
            self.assertEqual(rows, list(csv.DictReader(stream)))
        grouped_rows = [row for row in rows if row["Address Hash"] in {"SYNTHETIC-e-address", "SYNTHETIC-f-address"}]
        self.assertEqual({(row["Address Hash"], row["Direction"], row["Number of I/O"]) for row in grouped_rows},
                         {("SYNTHETIC-e-address", "IN", "1"), ("SYNTHETIC-f-address", "IN", "2")})
        self.assertTrue(all("CONTEXT" in row["Address Flags"] for row in grouped_rows))
        self.assertEqual(reviewed_plot(self.case, original["preview_id"]), (old_graph, old_plan))
        self.assertEqual(self.bytes(old_directory), old_bytes)
        update_case(self.case, {"run_defaults": {"group_context_inputs": False}})
        self.assertEqual(reviewed_plot(self.case, result["preview_id"]), (graph, plan))
        self.assertTrue(all(item["reviewable"] for item in list_plots(self.case)))
        self.assertEqual(self.bytes(self.archive), before)

    def test_legacy_plot_without_settings_snapshot_keeps_strict_review(self):
        result = preview_plot(self.case, "full")
        directory = Path(result["directory"])
        graph = read_json(directory / "graph.json")
        graph["plot"].pop("layout_settings")
        self.legacy_inputs(directory, graph)
        save_json(directory / "graph.json", graph)
        save_json(directory / "plot.json", graph["plot"])
        self.rehash_preview(directory)
        reviewed_plot(self.case, result["preview_id"])
        before = self.bytes(directory)
        update_case(self.case, {"run_defaults": {"connector_style": "elbowed"}})
        with self.assertRaisesRegex(TraceError, "changed"):
            reviewed_plot(self.case, result["preview_id"])
        self.assertFalse(list_plots(self.case)[0]["reviewable"])
        self.assertEqual(self.bytes(directory), before)

    def test_snapshot_settings_hash_validation_and_graph_consistency(self):
        result = preview_plot(self.case, "full")
        directory = Path(result["directory"])
        original = read_json(directory / "graph.json")
        for change in ("extra", "type", "noncanonical", "hash", "graph", "version"):
            graph = deepcopy(original)
            settings = graph["plot"]["layout_settings"]
            if change == "extra":
                settings["private_path"] = "/private/unsupported"
            elif change == "type":
                settings["layout_attempts"] = True
            elif change == "noncanonical":
                settings["hub_addresses"] = ["H" * 34, "H" * 34]
            elif change == "version":
                settings["presentation_version"] += 1
            else:
                settings["connector_style"] = "elbowed"
            if change != "hash":
                graph["plot"]["settings_sha256"] = digest(canonical(settings))
            save_json(directory / "graph.json", graph)
            save_json(directory / "plot.json", graph["plot"])
            self.rehash_preview(directory)
            with self.subTest(change=change), self.assertRaises(TraceError):
                reviewed_plot(self.case, result["preview_id"])

    def test_new_presentation_version_requires_regeneration(self):
        from liquid_tracer.export import PRESENTATION_VERSION

        result = preview_plot(self.case, "full")
        with patch("liquid_tracer.export.PRESENTATION_VERSION", PRESENTATION_VERSION + 1):
            with self.assertRaisesRegex(TraceError, "presentation version"):
                reviewed_plot(self.case, result["preview_id"])
            listed = list_plots(self.case)
            self.assertEqual(len(listed), 1)
            self.assertFalse(listed[0]["reviewable"])

    def test_attribution_changes_do_not_retarget_a_saved_layout(self):
        result = preview_plot(self.case, "full")
        original = reviewed_plot(self.case, result["preview_id"])
        update_case(self.case, {"run_defaults": {"connector_style": "elbowed"}})
        set_service(self.case, "SYNTHETIC-c-address", name="New assessment", stop_tracing=False)
        self.assertEqual(reviewed_plot(self.case, result["preview_id"]), original)

    def test_legacy_plot_without_input_snapshot_still_rejects_changed_controls(self):
        result = preview_plot(self.case, "full")
        directory = Path(result["directory"])
        graph = read_json(directory / "graph.json")
        self.legacy_inputs(directory, graph)
        save_json(directory / "graph.json", graph)
        save_json(directory / "plot.json", graph["plot"])
        self.rehash_preview(directory)
        reviewed_plot(self.case, result["preview_id"])
        set_service(self.case, "SYNTHETIC-c-address", name="New assessment", stop_tracing=False)
        with self.assertRaisesRegex(TraceError, "changed"):
            reviewed_plot(self.case, result["preview_id"])

    def test_modified_plot_and_rehashed_disagreeing_plan_are_rejected(self):
        result = preview_plot(self.case, "full")
        directory = Path(result["directory"])
        (directory / "graph.svg").write_text("changed")
        with self.assertRaisesRegex(TraceError, "changed"):
            reviewed_plot(self.case, result["preview_id"])
        result = preview_plot(self.case, "full")
        directory = Path(result["directory"])
        graph = read_json(directory / "graph.json")
        graph["nodes"][0]["label"] += " changed"
        save_json(directory / "graph.json", graph)
        self.rehash_preview(directory)
        with self.assertRaisesRegex(TraceError, "disagree"):
            reviewed_plot(self.case, result["preview_id"])
        self.assertEqual(list_plots(self.case), [])

    def test_changed_source_manifest_rejected_and_new_latest_does_not_retarget_old_plot(self):
        result = preview_plot(self.case, "full")
        metadata = read_case(self.case)
        metadata["latest_run"] = "f" * 16
        save_json(self.case / "case.json", metadata)
        reviewed_plot(self.case, result["preview_id"])
        state = deepcopy(self.state)
        state["stop_reason"] = "changed source"
        self.rewrite_archive(state)
        with self.assertRaisesRegex(TraceError, "changed"):
            reviewed_plot(self.case, result["preview_id"])

    def test_paths_symlinks_foreign_case_and_invalid_queries_rejected(self):
        for identity in (None, "../../private", "a" * 16 + "-pegouts-12345678"):
            with self.assertRaises(TraceError):
                reviewed_plot(self.case, identity)
        for goal, options in (("unknown", {}), ("pegouts", {"min_hops": 3, "max_hops": 1}),
                              ("connections", {"max_hops": True})):
            with self.assertRaises(TraceError):
                preview_plot(self.case, goal, **options)
        result = preview_plot(self.case, "full")
        directory = Path(result["directory"])
        external = self.root / "external.svg"
        external.write_bytes((directory / "graph.svg").read_bytes())
        (directory / "graph.svg").unlink()
        (directory / "graph.svg").symlink_to(external)
        with self.assertRaisesRegex(TraceError, "symbolic"):
            reviewed_plot(self.case, result["preview_id"])
        state = deepcopy(self.state)
        state["case_id"] = "foreign"
        self.rewrite_archive(state)
        with self.assertRaisesRegex(TraceError, "belong"):
            preview_plot(self.case, "full")

    def test_legacy_search_archives_are_not_sources_or_mutated(self):
        legacy = self.case / "pegouts" / ("e" * 16)
        legacy.mkdir(parents=True)
        save_json(legacy / "trace.json", self.state)
        before = self.bytes(legacy)
        with self.assertRaisesRegex(TraceError, "manifest"):
            preview_plot(self.case, "pegouts", run_id=legacy.name)
        preview_plot(self.case, "pegouts")
        self.assertEqual(before, self.bytes(legacy))

    def test_saved_counts_stay_frozen_and_new_plots_use_updated_counts(self):
        address = "SYNTHETIC-c-address"
        cache = {"schema_version": 1, "case_id": self.state["case_id"], "source": self.state["source"],
                 "counts": {address: {"address": address, "source": self.state["source"],
                            "confirmed_tx_count": 12, "mempool_tx_count": 1, "observed_at": "2026-09-25"}}}
        def save_cache():
            cache["sha256"] = digest(canonical({key: value for key, value in cache.items() if key != "sha256"}))
            save_json(self.case / "address-counts.json", cache)
        save_cache()
        result = preview_plot(self.case, "full")
        graph, _ = reviewed_plot(self.case, result["preview_id"])
        node = next(node for node in graph["nodes"] if node.get("details", {}).get("address") == address)
        self.assertEqual(node["tx_count"], 13)
        cache["counts"][address]["confirmed_tx_count"] = 14
        save_cache()
        self.assertEqual(reviewed_plot(self.case, result["preview_id"])[0], graph)
        fresh = preview_plot(self.case, "full")
        current, _ = reviewed_plot(self.case, fresh["preview_id"])
        current_node = next(node for node in current["nodes"] if node.get("details", {}).get("address") == address)
        self.assertEqual(current_node["tx_count"], 15)

    def test_listing_multiple_goals_verifies_shared_source_once(self):
        from liquid_tracer.cli import verify_export
        for goal in ("full", "connections", "pegouts"):
            preview_plot(self.case, goal)
        with patch("liquid_tracer.cli.verify_export", wraps=verify_export) as verify:
            self.assertEqual(len(list_plots(self.case)), 3)
        verify.assert_called_once_with(self.archive)

    def test_bootstrap_collection_without_transactions_has_empty_reviewable_full_plot(self):
        state = deepcopy(self.state)
        state.update(transactions={}, outputs={}, links={}, status="paused", stop_reason="interrupted")
        self.rewrite_archive(state)
        result = preview_plot(self.case, "full")
        graph, plan = reviewed_plot(self.case, result["preview_id"])
        self.assertTrue(result["empty"])
        self.assertEqual(plan["shapes"], [])
        self.assertEqual(graph["plot"]["status"], "empty")
        self.layout.assert_not_called()

    def test_failed_csv_export_reports_export_stage_without_publishing_partial_plot(self):
        events = []
        before = self.bytes(self.archive)
        with patch("liquid_tracer.transaction_csv.write_transaction_csv", side_effect=TraceError("Synthetic export failure")):
            with self.assertRaisesRegex(TraceError, "Synthetic export failure"):
                preview_plot(self.case, "full", progress=events.append)
        self.assertEqual(events[-1], {"phase": "exporting_plot", "completed": 0, "total": 1})
        self.assertEqual(list_plots(self.case), [])
        self.assertEqual(self.bytes(self.archive), before)
        result = preview_plot(self.case, "full", progress=events.append)
        self.assertEqual(events[-1], {"phase": "exporting_plot", "completed": 1, "total": 1})
        self.assertEqual([item["preview_id"] for item in list_plots(self.case)], [result["preview_id"]])
