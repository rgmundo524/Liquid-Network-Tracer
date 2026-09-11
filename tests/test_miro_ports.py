import copy
import json
import tempfile
import unittest
from pathlib import Path

from liquid_tracer.api import Esplora, Limits
from liquid_tracer.cli import refresh_presentation
from liquid_tracer.common import TraceError, canonical, digest, read_json, save_json
from liquid_tracer.export import COLORS, PRESENTATION_VERSION, build_graph
from liquid_tracer.miro import make_plan, publish, resolve, sync, validate_plan
from liquid_tracer.store import Store
from liquid_tracer.trace import new_state, trace
from tests.fixtures import A, B, fixture
from tests.test_miro_layout import FEE_EDGE, presented
from tests.test_miro_sync import FakeMiro, graph


def fixed_graph(value=None):
    value = copy.deepcopy(value if value is not None else graph())
    value["connector_attachment"] = "transaction_sides_v1"
    return value


class PercentageMiro(FakeMiro):
    """Miro's GET response model exposes percentage positions, without snapTo."""
    def __call__(self, method, url, headers, body, timeout):
        status, response_headers, raw = super().__call__(method, url, headers, body, timeout)
        if method not in ("POST", "PATCH") or not 200 <= status < 300:
            return status, response_headers, raw
        result = json.loads(raw)
        payload = json.loads(body)
        for field in ("startItem", "endItem"):
            if field not in payload or "snapTo" not in payload[field]:
                continue
            side = payload[field]["snapTo"]
            # Deliberately make auto coincide with the requested side. Old
            # automatic endpoints still need an explicit upgrade to stay fixed.
            x = "100.0%" if side == "right" or side == "auto" and field == "startItem" else "0%"
            result[field] = {"id": payload[field]["id"], "position": {"x": x, "y": "50.00%"}}
            self.items[result["id"]][field] = copy.deepcopy(result[field])
        return status, response_headers, canonical(result)


class MiroPortTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_path = Path(self.tmp.name) / "miro.json"
        self.remote = FakeMiro()

    def sync(self, value=None, **kwargs):
        return sync(make_plan(value or fixed_graph()), "board=", self.state_path,
                    token="test-token", transport=self.remote, interval=0, **kwargs)

    def item(self, key):
        return self.remote.items[read_json(self.state_path)["items"][key]["id"]]

    def test_new_connectors_keep_transaction_sides_with_addresses_on_the_other_side(self):
        value = fixed_graph(presented(include_fees=True))
        for node in value["nodes"]:
            if node["id"] == "addr:a":
                node["x"] = 1000  # Input lies to the right of its transaction.
            if node["id"] == "addr:b":
                node["x"] = -1000  # Output returns to the left.
        plan = make_plan(value)
        self.assertEqual(plan["connector_attachment"], "transaction_sides_v1")
        self.sync(value)
        incoming, outgoing = self.item("input:1:0"), self.item("output:1:0")
        self.assertEqual(incoming["endItem"]["snapTo"], "left")
        self.assertEqual(incoming["startItem"]["snapTo"], "auto")
        self.assertEqual(outgoing["startItem"]["snapTo"], "right")
        self.assertEqual(outgoing["endItem"]["snapTo"], "auto")
        self.assertEqual(self.item(FEE_EDGE)["startItem"]["snapTo"], "right")
        self.assertEqual(self.item(FEE_EDGE)["endItem"]["snapTo"], "auto")
        for connector in plan["connectors"]:
            self.assertNotIn("startItem", connector["body"])
            self.assertNotIn("endItem", connector["body"])

    def test_ordinary_sync_preserves_existing_attachment_routing_and_manual_fields(self):
        self.sync(graph())
        incoming = self.item("input:1:0")
        incoming["shape"] = "elbowed"
        incoming["startItem"] = {"id": incoming["startItem"]["id"], "position": {"x": "75%", "y": "25%"}}
        incoming["endItem"]["snapTo"] = "top"
        incoming["captions"][0]["content"] = "Analyst evidence annotation"
        incoming["style"]["strokeColor"] = "#654321"
        before = copy.deepcopy(incoming)
        report = self.sync()
        self.assertEqual(incoming, before)
        self.assertEqual(report["reattached"], 0)
        for method, _, body in self.remote.writes:
            if method == "PATCH":
                self.assertNotIn("startItem", body)
                self.assertNotIn("endItem", body)

    def test_organize_upgrades_only_transaction_end_preserving_ids_annotations_and_routing(self):
        self.sync(graph())
        incoming = self.item("input:1:0")
        incoming["shape"] = "elbowed"
        incoming["startItem"] = {"id": incoming["startItem"]["id"], "position": {"x": "75%", "y": "25%"}}
        incoming["captions"][0]["content"] = "Analyst evidence annotation"
        incoming["style"]["strokeColor"] = "#654321"
        before = copy.deepcopy(incoming)
        ids = {key: value["id"] for key, value in read_json(self.state_path)["items"].items()}
        observed = []
        remote = self.remote

        def inspect(method, url, headers, body, timeout):
            if method == "PATCH" and any(field in json.loads(body) for field in ("startItem", "endItem")):
                observed.append(read_json(self.state_path)["layout_history"][-1])
            return remote(method, url, headers, body, timeout)

        self.remote = inspect
        report = self.sync(reorganize=True)
        self.remote = remote
        incoming = self.item("input:1:0")
        self.assertEqual(report["reattached"], 2)
        self.assertTrue(observed)
        saved = {row["key"]: row for row in report["layout_snapshot"]["attachments"]}
        self.assertEqual(saved["input:1:0"]["before"], {"endItem": before["endItem"]})
        self.assertEqual(saved["input:1:0"]["after"], {"endItem": incoming["endItem"]})
        self.assertEqual(incoming["endItem"]["snapTo"], "left")
        for field in ("startItem", "shape", "captions", "style"):
            self.assertEqual(incoming[field], before[field])
        self.assertEqual(ids, {key: value["id"] for key, value in read_json(self.state_path)["items"].items()})
        writes = len(self.remote.writes)
        repeated = self.sync(reorganize=True)
        self.assertEqual(repeated["reattached"], 0)
        self.assertEqual(len(self.remote.writes), writes)

    def test_percentage_get_results_reassert_sides_on_explicit_organization(self):
        self.remote = PercentageMiro()
        self.sync(graph())
        self.assertNotIn("snapTo", self.item("input:1:0")["endItem"])
        self.assertEqual(self.item("input:1:0")["endItem"]["position"]["x"], "0%")
        report = self.sync(reorganize=True)
        self.assertEqual(report["reattached"], 2)
        # A manual reset to auto can produce these same percentage coordinates.
        # Never infer that a prior applied marker still reflects the live mode.
        writes = len(self.remote.writes)
        self.assertEqual(self.sync()["reattached"], 0)
        self.assertEqual(len(self.remote.writes), writes)
        self.assertEqual(self.sync(reorganize=True)["reattached"], 2)
        self.item("input:1:0")["endItem"]["position"] = {"x": "50%", "y": "0%"}
        self.assertEqual(self.sync()["reattached"], 0)
        self.assertEqual(self.sync(reorganize=True)["reattached"], 2)
        for method, _, payload in self.remote.writes:
            if method == "PATCH":
                for field in ("startItem", "endItem"):
                    if field in payload:
                        self.assertNotIn("position", payload[field])
                        self.assertEqual(set(payload[field]), {"id", "snapTo"})

    def test_new_percentage_connectors_reassert_ambiguous_side_settings(self):
        self.remote = PercentageMiro()
        self.sync()
        ids = {key: value["id"] for key, value in read_json(self.state_path)["items"].items()}
        report = self.sync(reorganize=True)
        self.assertEqual(report["reattached"], 2)
        self.assertEqual(self.sync(reorganize=True)["reattached"], 2)
        self.assertEqual(ids, {key: value["id"] for key, value in read_json(self.state_path)["items"].items()})

    def test_changed_remote_endpoint_aborts_before_any_writes(self):
        self.sync(graph())
        self.item("output:1:0")["startItem"]["id"] = self.item("addr:a")["id"]
        before = self.state_path.read_bytes()
        writes = len(self.remote.writes)
        with self.assertRaisesRegex(TraceError, "connector endpoints were changed"):
            self.sync(reorganize=True)
        self.assertEqual(len(self.remote.writes), writes)
        self.assertEqual(self.state_path.read_bytes(), before)

    def test_legacy_plan_organization_keeps_automatic_connections(self):
        self.sync(graph())
        self.sync(graph(), reorganize=True)
        self.assertEqual(self.item("input:1:0")["endItem"]["snapTo"], "auto")
        self.assertEqual(self.item("output:1:0")["startItem"]["snapTo"], "auto")

    def test_snapshot_publish_uses_new_attachment_policy(self):
        value = fixed_graph()
        value.pop("namespace")
        plan = make_plan(value)
        publish(plan, "board=", self.state_path, token="test-token", transport=self.remote, interval=0)
        mapping = read_json(self.state_path)["items"]
        self.assertEqual(self.remote.items[mapping["input:1:0"]]["endItem"]["snapTo"], "left")
        self.assertEqual(self.remote.items[mapping["output:1:0"]]["startItem"]["snapTo"], "right")

    def test_malformed_attachment_cannot_supply_ids_positions_or_wrong_sides(self):
        for attachment in ({"endItem": {"snapTo": "right"}},
                           {"endItem": {"snapTo": "left", "id": "other-remote"}},
                           {"endItem": {"position": {"x": "0%", "y": "50%"}}},
                           {"startItem": {"snapTo": "left"}}, None):
            with self.subTest(attachment=attachment):
                plan = make_plan(fixed_graph())
                plan["connectors"][0]["attachment"] = attachment
                plan["sha256"] = digest(canonical({key: value for key, value in plan.items() if key != "sha256"}))
                with self.assertRaisesRegex(TraceError, "connector sides disagree"):
                    validate_plan(plan)

    def test_lost_connector_post_reconciliation_keeps_applied_sides(self):
        remote = self.remote
        failed = []

        def lose_connector(method, url, headers, body, timeout):
            if method == "POST" and url.endswith("/connectors") and not failed:
                failed.append(True)
                remote.lose_next_post = True
            return remote(method, url, headers, body, timeout)

        self.remote = lose_connector
        with self.assertRaisesRegex(TraceError, "lost response"):
            self.sync()
        pending = read_json(self.state_path)["pending"]
        self.assertEqual(pending["attachments"], {"endItem": {"snapTo": "left"}})
        resolve(self.state_path, item_id="remote-" + str(remote.counter))
        self.remote = remote
        self.sync()
        self.assertEqual(self.sync(reorganize=True)["reattached"], 0)

    def test_v3_saved_trace_refresh_then_organize_upgrades_colors_and_connections(self):
        root = Path(self.tmp.name)
        fixture_path, trace_path = root / "synthetic.json", root / "trace.json"
        save_json(fixture_path, fixture())
        store = Store(root / "case")
        self.addCleanup(store.close)
        limits = Limits(max_hops=1)
        api = Esplora(store, "pending", limits, fixture=fixture_path, min_interval=0)
        state = new_state([A + ":0", B + ":0"], api.base, limits, [])
        api.run_id = state["run_id"]
        state = trace(api, state, limits, trace_path)
        old_graph = build_graph(state)
        old_graph["presentation_version"] = 3
        old_graph.pop("connector_attachment")
        for node in old_graph["nodes"]:
            if node["kind"] == "transaction":
                node["color"] = COLORS["transaction"]
                node.pop("role", None)
        old_plan = make_plan(old_graph)
        archived = root / "v3-miro-plan.json"
        save_json(archived, old_plan)
        before = {path: path.read_bytes() for path in (trace_path, archived)}
        options = {"token": "test-token", "transport": self.remote, "interval": 0}
        sync(old_plan, "board=", self.state_path, **options)
        old_ids = {key: record["id"] for key, record in read_json(self.state_path)["items"].items()}
        refreshed = refresh_presentation(read_json(archived), trace_path)
        report = sync(refreshed, "board=", self.state_path, reorganize=True, **options)
        self.assertEqual(refreshed["presentation_version"], 6)
        self.assertEqual(refreshed["connector_attachment"], "transaction_ports_v2")
        self.assertGreater(report["reattached"], 0)
        self.assertEqual(report["created"], 0)
        for txid in (A, B):
            self.assertEqual(self.item("tx:" + txid)["style"]["fillColor"], COLORS["starting_transaction"])
        for connector in refreshed["connectors"]:
            item = self.item(connector["key"])
            for field, logical, side in (("startItem", "source", "right"), ("endItem", "target", "left")):
                self.assertEqual(item[field]["id"], old_ids[connector[logical]])
                if connector[logical].startswith("tx:"):
                    self.assertEqual(item[field]["position"]["x"], "100%" if side == "right" else "0%")
        self.assertEqual(old_ids, {key: record["id"] for key, record in read_json(self.state_path)["items"].items()})
        self.assertEqual(before, {path: path.read_bytes() for path in before})


if __name__ == "__main__":
    unittest.main()
