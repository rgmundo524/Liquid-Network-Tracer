"""ELK presentation contracts at the Miro API boundary; no live API calls."""
import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from liquid_tracer.common import TraceError, canonical, digest, read_json
from liquid_tracer.miro import _bounds, _overlap, make_plan, resolve, sync, validate_plan
from tests.test_miro_sync import FakeMiro, graph


def elk_graph(run="one", extended=False):
    value = graph(run, extended)
    value["connector_attachment"] = "transaction_ports_v2"
    value["graph_options"] = {"connector_style": "straight", "include_fees": False}
    value["layout"] = {"algorithm": "elk_layered_v1", "direction": "left_to_right",
                       "metrics": {"estimated": True, "before": {"crossings": 3}, "after": {"crossings": 0}}}
    for edge in value["edges"]:
        edge["connector_shape"] = "straight"
        edge["attachment"] = {
            "startItem": {"position": {"x": "100%", "y": "50%"}},
            "endItem": {"position": {"x": "0%", "y": "50%"}},
        }
        edge["route"] = [{"x": 10, "y": 20}, {"x": 30, "y": 40}]
    return value


def resigned(plan):
    plan["sha256"] = digest(canonical({key: value for key, value in plan.items() if key != "sha256"}))
    return plan


class PositionMiro(FakeMiro):
    """Connection updates replace alternate snapTo/position attachment modes."""
    def __call__(self, method, url, headers, body, timeout):
        if method == "PATCH":
            payload = json.loads(body)
            remote = self.items[url.rsplit("/", 1)[-1]]
            for field in ("startItem", "endItem"):
                if field in payload and "position" in payload[field]:
                    remote[field].pop("snapTo", None)
        return super().__call__(method, url, headers, body, timeout)


class ElkMiroTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_path = Path(self.tmp.name) / "miro.json"
        self.remote = PositionMiro()

    def sync(self, value=None, **kwargs):
        return sync(make_plan(value or elk_graph()), "synthetic-board=", self.state_path,
                    token="synthetic-token", transport=self.remote, interval=0, **kwargs)

    def item(self, key):
        return self.remote.items[read_json(self.state_path)["items"][key]["id"]]

    def test_plan_and_new_connectors_use_explicit_ports_without_publishing_local_routes(self):
        value = elk_graph()
        value["edges"][0]["attachment"]["endItem"]["position"]["y"] = "25%"
        value["edges"][1]["connector_shape"] = "elbowed"
        plan = make_plan(value)
        validate_plan(plan)
        self.assertEqual(plan["connector_attachment"], "transaction_ports_v2")
        self.assertEqual(plan["graph_options"]["connector_style"], "straight")
        report = self.sync(value)
        self.assertEqual(report["layout_algorithm"], "elk_layered_v1")
        self.assertTrue(report["layout_metrics"]["estimated"])
        incoming, outgoing = self.item("input:1:0"), self.item("output:1:0")
        self.assertEqual(incoming["shape"], "straight")
        self.assertEqual(incoming["endItem"]["position"], {"x": "0%", "y": "25%"})
        self.assertEqual(outgoing["shape"], "elbowed")
        for item in (incoming, outgoing):
            self.assertNotIn("route", item)
            for field in ("startItem", "endItem"):
                self.assertEqual(set(item[field]), {"id", "position"})
        self.assertNotIn("route", plan["connectors"][0])
        self.assertNotIn("startItem", plan["connectors"][0]["body"])

    def test_return_link_keeps_logical_direction_and_event_ports_stay_on_diamond(self):
        value = elk_graph()
        event = value["nodes"][2]
        event.update(kind="event", x=-500, y=400)
        outgoing = value["edges"][1]
        outgoing["connector_shape"] = "elbowed"
        outgoing["attachment"]["endItem"]["position"] = {"x": "75%", "y": "25%"}
        validate_plan(make_plan(value))
        self.sync(value)
        connector = self.item("output:1:0")
        self.assertEqual(connector["startItem"]["id"], self.item("tx:1")["id"])
        self.assertEqual(connector["endItem"]["id"], self.item("addr:b")["id"])
        self.assertEqual(connector["startItem"]["position"]["x"], "100%")
        self.assertEqual(connector["shape"], "elbowed")
        invalid = make_plan(value)
        invalid["connectors"][1]["attachment"]["endItem"]["position"] = {"x": "50%", "y": "50%"}
        with self.assertRaisesRegex(TraceError, "layout ports"):
            validate_plan(resigned(invalid))

    def test_invalid_ports_and_topology_fail_before_any_board_writes(self):
        mutations = [
            lambda item: item["attachment"]["endItem"]["position"].update(x="100%"),
            lambda item: item["attachment"]["endItem"]["position"].update(y="101%"),
            lambda item: item["attachment"]["endItem"]["position"].update(y="nan%"),
            lambda item: item["attachment"]["endItem"]["position"].update(y=50),
            lambda item: item["attachment"]["endItem"].update(id="unrelated-remote"),
            lambda item: item["attachment"]["endItem"].update(snapTo="auto"),
            lambda item: item["attachment"]["startItem"]["position"].update(x="50%"),
            lambda item: item["attachment"].pop("startItem"),
            lambda item: item["body"].update(shape="orthogonal"),
            lambda item: item["body"].update(startItem={"id": "unrelated-remote"}),
            lambda item: item.update(target="addr:b"),
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                plan = make_plan(elk_graph())
                mutation(plan["connectors"][0])
                with self.assertRaisesRegex(TraceError, "layout ports or connector topology"):
                    sync(resigned(plan), "synthetic-board=", self.state_path,
                         token="synthetic-token", transport=self.remote, interval=0)
                self.assertFalse(self.remote.calls)
                self.assertFalse(self.state_path.exists())

    def test_continuation_reuses_ids_preserves_manual_positions_ports_and_routing(self):
        self.sync()
        previous = read_json(self.state_path)["items"]
        self.item("addr:a")["position"].update(x=6000, y=800)
        connector = self.item("input:1:0")
        connector["shape"] = "curved"
        connector["endItem"]["position"] = {"x": "50%", "y": "0%"}
        connector["captions"][0]["content"] = "Analyst observation"
        connector["style"]["strokeColor"] = "#123456"
        old_shapes = {key: copy.deepcopy(self.item(key)) for key in ("addr:a", "tx:1", "addr:b")}
        old_connector = copy.deepcopy(connector)
        report = self.sync(elk_graph("two", True))
        self.assertEqual(report["created"], 5)
        self.assertEqual(report["moved"], 0)
        self.assertEqual(report["reattached"], 0)
        self.assertEqual(connector, old_connector)
        self.assertEqual(old_shapes, {key: self.item(key) for key in old_shapes})
        mapping = read_json(self.state_path)["items"]
        for key in previous:
            self.assertEqual(previous[key]["id"], mapping[key]["id"])
        count = len(self.remote.writes)
        self.assertEqual(self.sync(elk_graph("two", True))["created"], 0)
        self.assertEqual(len(self.remote.writes), count)

    def test_reorganize_applies_elk_ports_and_connector_style_preserving_annotations(self):
        self.sync(graph())
        connector = self.item("input:1:0")
        connector["captions"][0]["content"] = "Analyst observation"
        connector["style"]["strokeColor"] = "#123456"
        node = self.item("addr:a")
        node["position"].update(x=10000, y=500)
        node["geometry"].update(width=420, height=160, rotation=15)
        before = copy.deepcopy(node)
        report = self.sync(reorganize=True)
        connector = self.item("input:1:0")
        self.assertEqual(connector["shape"], "straight")
        self.assertEqual(connector["endItem"]["position"], {"x": "0%", "y": "50%"})
        self.assertNotIn("snapTo", connector["endItem"])
        self.assertEqual(connector["captions"][0]["content"], "Analyst observation")
        self.assertEqual(connector["style"]["strokeColor"], "#123456")
        self.assertEqual(node["geometry"], before["geometry"])
        self.assertIn("connector_shapes", report["layout_snapshot"])
        self.assertEqual(report["reattached"], 2)
        self.assertGreater(report["moved"], 0)
        boxes = [_bounds(self.item(key), key) for key in ("addr:a", "tx:1", "addr:b")]
        self.assertFalse(any(_overlap(a, b) for index, a in enumerate(boxes) for b in boxes[index + 1:]))

    def test_reorganize_keeps_elk_coordinates_for_variable_widths_in_one_layer(self):
        value = elk_graph()
        value["nodes"][1]["width"] = 200
        value["nodes"].append({**value["nodes"][1], "id": "tx:other", "width": 160, "x": 420, "y": 400})
        value["edges"].append({**copy.deepcopy(value["edges"][0]), "id": "input:other:0", "target": "tx:other"})
        self.sync(value)
        self.item("tx:1")["position"]["x"] = 5000
        self.item("tx:other")["position"]["x"] = 6000
        self.sync(value, reorganize=True)
        self.assertEqual(self.item("tx:1")["position"]["x"], 400)
        self.assertEqual(self.item("tx:other")["position"]["x"], 420)
        self.assertEqual(self.item("tx:other")["position"]["y"], 400)

    @unittest.skipUnless(shutil.which("node") and (Path(__file__).resolve().parents[1] /
                         "layout/node_modules/elkjs/package.json").is_file(), "local ELK installation unavailable")
    def test_real_elk_cumulative_run_preserves_mapping_and_survives_manual_resizes(self):
        from liquid_tracer.elk_layout import optimize_graph
        from liquid_tracer.export import build_graph
        from tests.test_layout import chain, state_from

        first = state_from(chain(2))
        first.update(run_id="one", ancestor_runs=[])
        first_graph = optimize_graph(build_graph(first))
        self.sync(first_graph)
        old_mapping = read_json(self.state_path)["items"]
        address = next(node["id"] for node in first_graph["nodes"] if node["kind"] == "address")
        self.item(address)["position"].update(x=15000, y=8000)
        self.item(address)["geometry"].update(width=470, height=220, rotation=10)
        geometry = copy.deepcopy(self.item(address)["geometry"])
        second = state_from(chain(3))
        second.update(run_id="two", parent_run="one", ancestor_runs=["one"])
        second_graph = optimize_graph(build_graph(second))
        report = self.sync(second_graph, reorganize=True)
        self.assertGreater(report["created"], 0)
        self.assertEqual(self.item(address)["geometry"], geometry)
        mapping = read_json(self.state_path)["items"]
        self.assertTrue(all(old_mapping[key]["id"] == mapping[key]["id"] for key in old_mapping))
        shapes = [_bounds(self.item(node["id"]), node["id"]) for node in second_graph["nodes"]]
        self.assertFalse(any(_overlap(a, b) for index, a in enumerate(shapes) for b in shapes[index + 1:]))
        before = len(self.remote.writes)
        repeated = self.sync(second_graph, reorganize=True)
        self.assertEqual((repeated["created"], repeated["moved"]), (0, 0))
        self.assertEqual(repeated["reattached"], len(second_graph["edges"]))
        self.assertTrue(all(method == "PATCH" for method, _, _ in self.remote.writes[before:]))
        before = len(self.remote.writes)
        self.assertEqual(self.sync(second_graph)["reattached"], 0)
        self.assertEqual(len(self.remote.writes), before)

    def test_remote_endpoint_tampering_stops_before_reorganization_writes(self):
        self.sync()
        self.item("input:1:0")["endItem"]["id"] = self.item("addr:b")["id"]
        before = self.state_path.read_bytes()
        writes = len(self.remote.writes)
        with self.assertRaisesRegex(TraceError, "connector endpoints were changed"):
            self.sync(reorganize=True)
        self.assertEqual(self.state_path.read_bytes(), before)
        self.assertEqual(len(self.remote.writes), writes)

    def test_lost_post_resolution_retains_explicit_attachment_intent(self):
        remote = self.remote
        lost = []

        def lose_connector(method, url, headers, body, timeout):
            if method == "POST" and url.endswith("/connectors") and not lost:
                lost.append(True)
                remote.lose_next_post = True
            return remote(method, url, headers, body, timeout)

        self.remote = lose_connector
        with self.assertRaisesRegex(TraceError, "lost response"):
            self.sync()
        pending = read_json(self.state_path)["pending"]
        self.assertEqual(set(pending["attachments"]), {"startItem", "endItem"})
        resolve(self.state_path, item_id="remote-" + str(remote.counter))
        self.remote = remote
        self.sync()
        self.assertEqual(self.sync()["created"], 0)
        self.assertEqual(len(remote.items), 7)


if __name__ == "__main__":
    unittest.main()
