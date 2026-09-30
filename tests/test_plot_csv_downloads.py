"""Two usable CSV downloads for old, new, and historical endpoint plots."""
import csv
import io
from pathlib import Path
import unittest
from unittest.mock import patch

from liquid_tracer.common import digest, read_json, save_json
from liquid_tracer.investigations import read_case, update_case
from liquid_tracer.pegouts import search_pegouts
from liquid_tracer.plots import PEGOUT_CSV_FILES, plot_files, preview_plot
from liquid_tracer.services import set_service
from tests import test_web
from tests.test_attribution_convergence import graph_state, tx
from tests.test_connections import saved_case
from tests.test_pegout_paths import add_pegout, add_unspendable, mark_unspent


class SavedPlotCSVDownloadTests(unittest.TestCase):
    setUp = test_web.LocalWebTests.setUp
    close_server = test_web.LocalWebTests.close_server
    request = test_web.LocalWebTests.request
    success = test_web.LocalWebTests.success
    create = test_web.LocalWebTests.create

    def modern(self, *, old=False):
        _, detail = self.create()
        case, _ = self.server.case(detail["id"])
        state = graph_state((("a:0", "b"), ("b:0", "c")), seeds=("a:0",))
        add_pegout(state, tx("c"))
        add_unspendable(state, tx("c"))
        mark_unspent(state, tx("c") + ":0")
        state, _ = saved_case(case, state)
        set_service(case, "SYNTHETIC-c-address", name="Saved Perp", confidence="confirmed", stop_tracing=False)
        with patch("liquid_tracer.elk_layout.optimize_graph", side_effect=lambda graph, **kwargs: graph):
            plot = preview_plot(case, "pegouts", include_unspent=True, include_unspendable=True)
        directory = Path(plot["directory"])
        if old:
            graph = read_json(directory / "graph.json")
            graph["plot"].pop("csv_export_version")
            save_json(directory / "graph.json", graph)
            save_json(directory / "plot.json", graph["plot"])
            for name in PEGOUT_CSV_FILES:
                (directory / name).unlink()
            (directory / "SHA256SUMS").write_text("".join(
                digest((directory / name).read_bytes()) + "  " + name + "\n"
                for name in sorted(plot_files(directory) - {"SHA256SUMS"})))
        return case, "/api/cases/" + detail["id"], plot

    def downloads(self, route, preview_id):
        detail = self.success(route)
        plot = next(row for row in detail["plots"] if row["preview_id"] == preview_id)
        return plot, {item["name"]: item["url"] for item in plot["artifact"]["downloads"]}

    def test_old_plot_has_full_accounting_and_sample_style_endpoint_download_without_regeneration(self):
        case, route, plot = self.modern(old=True)
        directory = Path(plot["directory"])
        before = {str(p.relative_to(case)): p.read_bytes() for p in case.rglob("*") if p.is_file()}
        _, links = self.downloads(route, plot["preview_id"])
        self.assertEqual({name for name in links if name.endswith(".csv")}, {"transactions.csv", "endpoints.csv"})
        with patch("liquid_tracer.elk_layout.optimize_graph", side_effect=AssertionError("Do not relayout")), \
                patch("liquid_tracer.api.Esplora.get", side_effect=AssertionError("Do not refetch")), \
                patch("liquid_tracer.miro.publish", side_effect=AssertionError("Do not publish")):
            self.assertEqual(self.success(links["transactions.csv"]), (directory / "transactions.csv").read_bytes())
            status, data, response = self.request(links["endpoints.csv"])
        self.assertEqual(status, 200, data)
        self.assertEqual(response.getheader("Content-Disposition"), 'attachment; filename="endpoints.csv"')
        reader = csv.DictReader(io.StringIO(data.decode()))
        self.assertEqual(reader.fieldnames[:7], ["Source", "Source Value", "Deposit/Peg-out Tx",
            "Address/Peg-out Address", "Receiving Entity", "Status", "Pegout LBTC"])
        rows = list(reader)
        self.assertEqual(len(rows), 3)
        self.assertEqual({row["Status"] for row in rows}, {"Pegout", "OP_Return", "Dormant"})
        self.assertEqual({row["Deposit/Peg-out Tx"] for row in rows}, {tx("c")})
        self.assertEqual({row["Source"] for row in rows}, {tx("a")})
        self.assertEqual({row["Hops from Seed"] for row in rows}, {"2"})
        self.assertEqual(next(row["Receiving Entity"] for row in rows if row["Status"] == "Dormant"), "Saved Perp")
        after = {str(p.relative_to(case)): p.read_bytes() for p in case.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_historical_csvs_keep_saved_attribution_after_current_rules_change(self):
        case, route, plot = self.modern()
        _, old_links = self.downloads(route, plot["preview_id"])
        original = self.success(old_links["endpoints.csv"])
        set_service(case, "SYNTHETIC-c-address", name="Changed name", confidence="suspected", stop_tracing=True)
        set_service(case, "SYNTHETIC-a-address", name="New stop", stop_tracing=True)
        saved, links = self.downloads(route, plot["preview_id"])
        self.assertFalse(saved["reviewable"])
        self.assertEqual(set(links), {"transactions.csv", "endpoints.csv"})
        self.assertEqual(self.success(links["endpoints.csv"]), original)
        self.assertEqual(self.success(links["transactions.csv"]), (Path(plot["directory"]) / "transactions.csv").read_bytes())

    def test_legacy_standalone_preview_gets_both_csvs(self):
        _, detail = self.create()
        case, _ = self.server.case(detail["id"])
        update_case(case, {"run_defaults": {"layout_attempts": 1}})
        with patch("liquid_tracer.elk_layout.optimize_graph", side_effect=lambda graph, **kwargs: graph):
            result = search_pegouts(case, max_hops=3, max_transactions=20, max_requests=100, max_outpoints=100)
        route = "/api/cases/" + detail["id"]
        search = self.success(route)["pegout_searches"][0]
        links = {item["name"]: item["url"] for item in search["artifact"]["downloads"]}
        self.assertEqual({name for name in links if name.endswith(".csv")}, {"transactions.csv", "endpoints.csv"})
        rows = list(csv.DictReader(io.StringIO(self.success(links["endpoints.csv"]).decode())))
        self.assertTrue(rows)
        self.assertTrue(all(row["Status"] == "Pegout" for row in rows))
        self.assertEqual(self.success(links["transactions.csv"]), (Path(result["directory"]) / "transactions.csv").read_bytes())
        archived = read_json(case / "pegouts" / result["search_id"] / "trace.json")
        seed_txid = read_case(case)["seeds"][0].split(":")[0]
        seed_address = archived["transactions"][seed_txid]["data"]["vout"][0]["scriptpubkey_address"]
        set_service(case, seed_address, name="New stop", stop_tracing=True)
        # Historical downloads remain discoverable without allowing stale publication.
        historical = self.success(route)["pegout_searches"][0]
        self.assertNotIn("preview_id", historical)
        self.assertNotIn("match_count", historical)
        historical_links = {item["name"]: item["url"] for item in historical["artifact"]["downloads"]}
        self.assertEqual(set(historical_links), {"transactions.csv", "endpoints.csv"})
        self.assertEqual(historical_links["endpoints.csv"], links["endpoints.csv"])
        self.assertEqual(list(csv.DictReader(io.StringIO(self.success(historical_links["endpoints.csv"]).decode()))), rows)

    def test_invalid_exports_and_changed_snapshots_are_rejected(self):
        _, route, plot = self.modern()
        _, links = self.downloads(route, plot["preview_id"])
        self.assertEqual(self.request(links["endpoints.csv"].replace("endpoints.csv", "trace.json"))[0], 404)
        self.assertEqual(self.request(route + "/plot-exports/not-a-preview/endpoints.csv")[0], 400)
        path = Path(plot["directory"]) / "graph.json"
        path.write_text(path.read_text() + " ")
        self.assertEqual(self.request(links["endpoints.csv"])[0], 400)


if __name__ == "__main__":
    unittest.main()
