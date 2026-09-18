"""Server, command-line and real terminal workflows for connection-only plotting."""
import contextlib
import io
import json
import unittest
import subprocess
from unittest.mock import patch

from liquid_tracer.cli import main
from liquid_tracer.common import canonical, digest, read_json, save_json
from liquid_tracer.connections import preview_connections, reviewed_connections
from liquid_tracer.investigations import read_case
from liquid_tracer.menu import create_app
from tests import test_web, test_menu_addresses
from tests.test_connections import saved_case


class ConnectionWebTests(unittest.TestCase):
    setUp = test_web.LocalWebTests.setUp
    close_server = test_web.LocalWebTests.close_server
    request = test_web.LocalWebTests.request
    success = test_web.LocalWebTests.success
    create = test_web.LocalWebTests.create
    wait = test_web.LocalWebTests.wait

    def setup_case(self):
        _, info = self.create()
        case, _ = self.server.case(info["id"])
        state, _ = saved_case(case)
        # This synthetic graph differs from the demo used to create the case.
        # Attach a matching statistics fixture now that previews fetch counts.
        from liquid_tracer.address_counts import addresses
        data = {"/address/" + address: {"address": address,
                    "chain_stats": {"tx_count": 23}, "mempool_stats": {"tx_count": 1}}
                for address in addresses(state)}
        fixture = case / "synthetic-counts-api.json"
        save_json(fixture, data)
        state["source"] = "fixture://" + digest(canonical(data))
        save_json(case / "case.json", {**read_case(case), "fixture": str(fixture)})
        state, _ = saved_case(case, state)
        return case, "/api/cases/"+info["id"], state

    def test_offline_action_returns_files_and_reopens_selected_snapshot(self):
        case, route, state = self.setup_case()
        before = (case/"case.json").read_bytes()
        job = self.success(route+"/actions", {"action": "connections", "run_id": state["run_id"], "connection_hops": 2}, status=202)
        result = self.wait(job)
        self.assertEqual(result["connection_count"], 1)
        self.assertEqual(result["max_hops"], 2)
        self.assertEqual(result["address_counts"]["remaining"], 0)
        self.assertGreater(result["address_counts"]["known"], 0)
        self.assertEqual({f["name"] for f in result["downloads"] if f["name"].endswith(".csv")}, {"transactions.csv"})
        csv_url = next(f["url"] for f in result["downloads"] if f["name"] == "transactions.csv")
        self.assertIn(b"Block,Time,Transaction Label,Transaction Hash", self.success(csv_url))
        self.assertNotIn("directory", result)
        self.assertIn("/connections-", "/"+result["preview_id"][17:])
        page = self.success(result["preview_url"])
        self.assertIn(b"Starter-to-starter", page)
        detail = self.success(route)
        self.assertEqual(detail["artifacts"][state["run_id"]]["connections"]["connection_count"], 1)
        self.assertEqual((case/"case.json").read_bytes(), before)

    def test_invalid_hops_and_unconfirmed_publish_do_not_start_worker(self):
        case, route, _ = self.setup_case()
        with patch.object(self.server, "start_job") as worker:
            for value in (True, -1, "10", 1.5):
                self.assertEqual(self.request(route+"/actions", {"action": "connections", "connection_hops": value})[0], 400)
            self.assertEqual(self.request(route+"/actions", {"action": "miro-connections"})[0], 400)
            worker.assert_not_called()
        self.assertEqual(self.request(route+"/actions", {"action": "connections"}, headers={"X-Liquid-CSRF":"wrong"})[0], 403)

    def test_publish_uses_fixed_command_and_separate_board(self):
        case, route, state = self.setup_case()
        result = preview_connections(case, max_hops=2)
        with patch.object(self.server, "start_job", return_value={"id":"test"}) as worker:
            self.success(route+"/actions", {"action":"miro-connections", "run_id":state["run_id"],
                "preview_id":result["preview_id"], "confirm_connections":True, "board":"separate-board"}, status=202)
            args = worker.call_args.args[0]
            self.assertEqual(args[:3], ["connections-publish", "--case", str(case)])
            self.assertIn(result["preview_id"], args)
            self.assertTrue(worker.call_args.kwargs["live"])

    def test_cli_empty_chart_and_no_network(self):
        case, _, _ = self.setup_case()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(["connections", "--case", str(case), "--hops", "1"]), 0)
        result = json.loads(output.getvalue())
        graph, plan = reviewed_connections(case, result["preview_id"])
        self.assertEqual(graph["nodes"], [])
        self.assertEqual(plan["shapes"], [])
        self.assertEqual(plan["connectors"], [])


class ConnectionMenuTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = test_menu_addresses.AddressMenuTests.asyncSetUp
    click = test_menu_addresses.AddressMenuTests.click

    async def test_terminal_hop_form_uses_offline_worker(self):
        from textual.widgets import Input
        saved_case(self.case)
        app = create_app(self.root)
        with patch("liquid_tracer.menu._OfflineCalculation.run", return_value=subprocess.CompletedProcess([], 0, json.dumps({"connection_count": 1,"html":"example.html"}), "")) as worker:
            async with app.run_test(size=(120, 70)) as pilot:
                app.created(self.case); await pilot.pause()
                await self.click(app, pilot, "#connections")
                app.screen.query_one("#connection-hops", Input).value = "10"
                await self.click(app, pilot, "#connection-go")
                await pilot.pause()
                self.assertTrue(worker.called)
                args = worker.call_args.args[0]
                self.assertIn("connections", args)
                self.assertIn("10", args)
                self.assertIn("--open", args)
