import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from liquid_tracer.common import TraceError, canonical, digest, read_json, save_json
from liquid_tracer.miro import _retry_delay, make_plan, publish, resolve, sync


NAMESPACE = {"case_id": "case-123", "source": "fixture:liquid-demo", "address_mode": "merged"}


def graph(run="one", extended=False):
    nodes = [
        {"id": "addr:a", "label": "Address A", "kind": "address", "color": "#facc15", "x": 0, "y": 50, "width": 160, "height": 160},
        {"id": "tx:1", "label": "Transaction 1", "kind": "transaction", "color": "#facc15", "x": 400, "y": 50, "width": 160, "height": 160},
        {"id": "addr:b", "label": "Address B", "kind": "address", "color": "#facc15", "x": 800, "y": 50, "width": 160, "height": 160},
    ]
    edges = [
        {"id": "input:1:0", "source": "addr:a", "target": "tx:1", "label": "vin 0", "quantity": "amount confidential", "role": "traced_input"},
        {"id": "output:1:0", "source": "tx:1", "target": "addr:b", "label": "vout 0", "quantity": "amount confidential", "role": "candidate_output"},
    ]
    if extended:
        nodes += [
            {"id": "tx:2", "label": "Transaction 2", "kind": "transaction", "color": "#facc15", "x": 1200, "y": 50, "width": 160, "height": 160},
            {"id": "addr:c", "label": "Address C", "kind": "address", "color": "#facc15", "x": 1600, "y": 50, "width": 160, "height": 160},
        ]
        edges += [
            {"id": "input:2:0", "source": "addr:b", "target": "tx:2", "label": "vin 0", "quantity": "amount confidential", "role": "traced_input"},
            {"id": "output:2:0", "source": "tx:2", "target": "addr:c", "label": "vout 0", "quantity": "amount confidential", "role": "candidate_output"},
        ]
    return {"namespace": copy.deepcopy(NAMESPACE), "run_id": run, "simulated": True, "notice": "Candidate ancestry only.",
            "nodes": nodes, "edges": edges, "run": {"limits": {"max_hops": 2}, "stop_reason": "hop_limit",
                "parent_run": "one" if run != "one" else None, "ancestor_runs": ["one"] if run != "one" else []}}


class FakeMiro:
    def __init__(self):
        self.items, self.calls = {}, []
        self.counter = 0
        self.lose_next_post = False
        self.normalize = False

    @property
    def writes(self):
        return [call for call in self.calls if call[0] != "GET"]

    def __call__(self, method, url, headers, body, timeout):
        payload = json.loads(body) if body is not None else None
        self.calls.append((method, url, copy.deepcopy(payload)))
        parts = url.split("/")
        if method == "POST":
            self.counter += 1
            item_id = "remote-" + str(self.counter)
            result = copy.deepcopy(payload)
            result["id"] = item_id
            result["type"] = "shape" if parts[-1] == "shapes" else "connector"
            if self.normalize and "data" in result:
                result["data"]["content"] = result["data"]["content"].replace("<br>", "<br />")
            if self.normalize and "fontSize" in result.get("style", {}):
                result["style"]["fontSize"] = float(result["style"]["fontSize"])
            self.items[item_id] = result
            if self.lose_next_post:
                self.lose_next_post = False
                raise TraceError("Synthetic lost response")
            return 201, {}, canonical(result)
        item_id = parts[-1]
        if item_id not in self.items:
            return 404, {}, b"{}"
        if method == "GET":
            return 200, {}, canonical(self.items[item_id])
        if method == "PATCH":
            for key, value in payload.items():
                if isinstance(value, dict):
                    self.items[item_id].setdefault(key, {}).update(copy.deepcopy(value))
                else:
                    self.items[item_id][key] = copy.deepcopy(value)
            return 200, {}, canonical(self.items[item_id])
        raise AssertionError("Unexpected method " + method)


class MiroSyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_path = Path(self.tmp.name) / "miro.json"
        self.remote = FakeMiro()

    def sync(self, plan, **kwargs):
        return sync(plan, "board=", self.state_path, token="test-token", transport=self.remote, interval=0, **kwargs)

    def item(self, key):
        item_id = read_json(self.state_path)["items"][key]["id"]
        return self.remote.items[item_id]

    def test_two_cumulative_runs_share_nodes_and_rerun_is_idempotent(self):
        first = make_plan(graph())
        initial = self.sync(first)
        old_ids = {k: v["id"] for k, v in read_json(self.state_path)["items"].items()}
        self.assertEqual(initial["created"], 7)
        second = make_plan(graph("two", True))
        report = self.sync(second, max_items=5)
        self.assertEqual((report["new_shapes"], report["new_connectors"], report["created"]), (3, 2, 5))
        self.assertEqual(report["updated"], 0)
        self.assertEqual(report["runs"], 2)
        mapping = read_json(self.state_path)["items"]
        for key, item_id in old_ids.items():
            self.assertEqual(mapping[key]["id"], item_id)
        self.assertEqual(self.item("input:2:0")["startItem"]["id"], old_ids["addr:b"])
        before = len(self.remote.writes)
        repeated = self.sync(second, max_items=0)
        self.assertEqual(len(self.remote.writes), before)
        self.assertEqual((repeated["created"], repeated["updated"]), (0, 0))
        self.assertEqual(len(self.remote.items), 12)
        self.assertIn("run:one", mapping)
        self.assertIn("run:two", mapping)

    def test_continuation_reports_checked_items_before_any_board_writes(self):
        self.sync(make_plan(graph()))
        self.remote.calls.clear()
        events, writes_during_preflight = [], []

        def progress(event):
            events.append(event)
            if event["phase"] in ("preflight", "layout"):
                writes_during_preflight.extend(self.remote.writes)

        report = self.sync(make_plan(graph("two", True)), progress=progress)
        preflight = [event for event in events if event["phase"] == "preflight"]
        self.assertEqual([event["completed"] for event in preflight], list(range(8)))
        self.assertEqual({event["total"] for event in preflight}, {7})
        creating = [event for event in events if event["phase"] == "creating"]
        self.assertEqual([event["completed"] for event in creating], list(range(6)))
        self.assertEqual({event["total"] for event in creating}, {5})
        self.assertEqual(events[-1]["phase"], "complete")
        self.assertEqual(writes_during_preflight, [])
        self.assertEqual(report["created"], 5)
        for event in events:
            self.assertEqual(set(event), {"phase", "completed", "total", "message"})
            self.assertNotIn("test-token", json.dumps(event))
            self.assertNotIn("board=", json.dumps(event))
            self.assertNotIn("remote-", json.dumps(event))

    def test_unchanged_repeat_preserves_manual_edits_without_per_item_state_rewrites(self):
        plan = make_plan(graph())
        self.sync(plan)
        self.item("addr:a")["data"]["content"] = "An analyst's private annotation"
        original = copy.deepcopy(self.item("addr:a"))
        writes = len(self.remote.writes)
        with patch("liquid_tracer.miro.save_json", wraps=save_json) as save:
            report = self.sync(plan)
        self.assertEqual(len(self.remote.writes), writes)
        self.assertEqual(self.item("addr:a"), original)
        self.assertEqual(report["created"], 0)
        self.assertEqual(report["updated"], 0)
        self.assertEqual(len(report["conflicts"]), 1)
        # Persist active-run intent and final summary, not every unchanged item.
        self.assertEqual(save.call_count, 2)
        self.assertIsNone(read_json(self.state_path)["active_run_id"])

    def test_preflight_reads_use_shorter_pacing_and_writes_keep_original_interval(self):
        self.sync(make_plan(graph()))
        with patch("liquid_tracer.miro.time.sleep") as sleep:
            sync(make_plan(graph("two", True)), "board=", self.state_path,
                 token="test-token", transport=self.remote)
        delays = [call.args[0] for call in sleep.call_args_list]
        self.assertEqual(delays[:7], [.05] * 7)
        self.assertEqual(delays[7:], [.4] * 5)

    def test_rate_limited_preflight_reports_wait_and_then_resumes_same_count(self):
        plan = make_plan(graph())
        self.sync(plan)
        remote, events = self.remote, []
        rejected = False

        def transport(method, url, *args):
            nonlocal rejected
            if method == "GET" and not rejected:
                rejected = True
                return 429, {"Retry-After": "3"}, b"{}"
            return remote(method, url, *args)

        with patch("liquid_tracer.miro.time.sleep") as sleep:
            report = sync(plan, "board=", self.state_path, token="test-token",
                          transport=transport, interval=0, progress=events.append)
        waiting = next(index for index, event in enumerate(events) if event["phase"] == "waiting")
        self.assertEqual(events[waiting]["retry_after"], 3)
        self.assertEqual(events[waiting]["reason"], "rate_limit")
        self.assertEqual((events[waiting]["completed"], events[waiting]["total"]), (0, 7))
        self.assertIn("rate limit", events[waiting]["message"])
        self.assertEqual(events[waiting + 1], events[waiting - 1])
        self.assertIn(3, [call.args[0] for call in sleep.call_args_list])
        self.assertEqual((report["created"], report["updated"]), (0, 0))

    def test_rate_limited_post_reports_wait_without_duplicate_creation(self):
        remote, events = self.remote, []
        rejected = False

        def transport(method, url, *args):
            nonlocal rejected
            if method == "POST" and not rejected:
                rejected = True
                return 429, {"Retry-After": "1"}, b"{}"
            return remote(method, url, *args)

        with patch("liquid_tracer.miro.time.sleep"):
            report = sync(make_plan(graph()), "board=", self.state_path, token="test-token",
                          transport=transport, interval=0, progress=events.append)
        waiting = [event for event in events if event["phase"] == "waiting"]
        self.assertEqual(len(waiting), 1)
        self.assertEqual((waiting[0]["completed"], waiting[0]["total"]), (0, 7))
        self.assertEqual(report["created"], 7)
        self.assertEqual(len(self.remote.items), 7)
        self.assertIsNone(read_json(self.state_path)["pending"])

    def test_server_read_retry_reports_wait_and_keeps_preflight_before_writes(self):
        self.sync(make_plan(graph()))
        remote, events = self.remote, []
        rejected = False
        self.remote.calls.clear()

        def transport(method, url, *args):
            nonlocal rejected
            if method == "GET" and not rejected:
                rejected = True
                return 503, {}, b"{}"
            return remote(method, url, *args)

        with patch("liquid_tracer.miro.time.sleep"):
            sync(make_plan(graph("two", True)), "board=", self.state_path, token="test-token",
                 transport=transport, interval=0, progress=events.append)
        waiting = [event for event in events if event["phase"] == "waiting"]
        self.assertEqual(len(waiting), 1)
        self.assertIn("retrying a Miro read", waiting[0]["message"])
        self.assertEqual(waiting[0]["reason"], "server_retry")
        self.assertEqual([call[0] for call in self.remote.calls[:7]], ["GET"] * 7)

    def test_failed_progress_reporting_cannot_lose_acknowledged_remote_ids(self):
        def fail(event):
            raise OSError("Synthetic closed progress file")

        report = self.sync(make_plan(graph()), progress=fail)
        state = read_json(self.state_path)
        self.assertEqual(report["created"], 7)
        self.assertEqual({record["id"] for record in state["items"].values()}, set(self.remote.items))
        self.assertIsNone(state["pending"])
        self.assertIsNone(state["active_run_id"])

    def test_rate_limit_reset_header_and_bounded_waits(self):
        with patch("liquid_tracer.miro.time.time", return_value=1000):
            self.assertEqual(_retry_delay({"X-RateLimit-Reset": "1005"}), 5)
            self.assertEqual(_retry_delay({"X-RateLimit-Reset": "999"}), 1)
            self.assertEqual(_retry_delay({"Retry-After": "2", "X-RateLimit-Reset": "1005"}), 2)
            self.assertEqual(_retry_delay({}), 2)
            for headers in ({"Retry-After": "31"}, {"X-RateLimit-Reset": "1031"}, {"Retry-After": "nan"}):
                with self.subTest(headers=headers), self.assertRaisesRegex(TraceError, "wait for the limit to reset"):
                    _retry_delay(headers)

    def test_manual_layout_content_and_style_survive_new_run(self):
        self.sync(make_plan(graph()))
        a = self.item("addr:a")
        a["position"] = {"x": 10000, "y": 875, "origin": "center"}
        a["geometry"] = {"width": 320, "height": 200}
        a["data"]["content"] += "<p>Analyst annotation: follow up with exchange.</p>"
        a["style"]["fillColor"] = "#a855f7"
        original = copy.deepcopy(a)
        new = graph("two", True)
        new["nodes"][0].update({"label": "Address A with automatic enrichment", "color": "#22c55e"})
        new["nodes"][2]["color"] = "#22c55e"
        report = self.sync(make_plan(new))
        self.assertEqual(self.item("addr:a"), original)
        self.assertEqual(self.item("addr:b")["style"]["fillColor"], "#22c55e")
        conflicts = {(c["key"], c["field"]) for c in report["conflicts"]}
        self.assertIn(("addr:a", "data.content"), conflicts)
        self.assertIn(("addr:a", "style.fillColor"), conflicts)
        for method, _, body in self.remote.writes:
            if method == "PATCH":
                self.assertNotIn("position", body)
                self.assertNotIn("geometry", body)
                self.assertNotIn("parent", body)
        for key in ("run:two", "tx:2", "addr:c"):
            item = self.item(key)
            self.assertGreater(item["position"]["x"] - item["geometry"]["width"] / 2, 10160)
        self.assertEqual(self.item("addr:c")["position"]["x"] - self.item("tx:2")["position"]["x"], 400)

    def test_manual_connector_caption_is_preserved_while_style_updates(self):
        self.sync(make_plan(graph()))
        connector = self.item("input:1:0")
        connector["captions"][0]["content"] = "Manual evidence note"
        new = graph("two")
        new["edges"][0]["quantity"] = "new evidence label"
        new["edges"][0]["role"] = "context_input"
        report = self.sync(make_plan(new))
        self.assertEqual(self.item("input:1:0")["captions"][0]["content"], "Manual evidence note")
        self.assertEqual(self.item("input:1:0")["style"]["strokeColor"], "#9ca3af")
        self.assertIn("captions", [c["field"] for c in report["conflicts"]])

    def test_remote_deletion_aborts_preflight_without_board_or_state_writes(self):
        self.sync(make_plan(graph()))
        item_id = read_json(self.state_path)["items"]["addr:a"]["id"]
        del self.remote.items[item_id]
        old_state = self.state_path.read_bytes()
        writes = len(self.remote.writes)
        with self.assertRaisesRegex(TraceError, "missing or inaccessible mapped items.*addr:a"):
            self.sync(make_plan(graph("two", True)))
        self.assertEqual(len(self.remote.writes), writes)
        self.assertEqual(self.state_path.read_bytes(), old_state)

    def test_changed_connector_endpoint_aborts_preflight(self):
        self.sync(make_plan(graph()))
        self.item("input:1:0")["startItem"]["id"] = self.item("addr:b")["id"]
        writes = len(self.remote.writes)
        with self.assertRaisesRegex(TraceError, "connector endpoints were changed"):
            self.sync(make_plan(graph("two", True)))
        self.assertEqual(len(self.remote.writes), writes)

    def test_foreign_case_source_mode_or_board_is_rejected_without_network(self):
        self.sync(make_plan(graph()))
        calls = len(self.remote.calls)
        for field, value in (("case_id", "another-case"), ("source", "https://another-source/api"), ("address_mode", "outpoint_occurrences")):
            new = graph("two")
            new["namespace"][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(TraceError, "different board, case, API source, or address mode"):
                self.sync(make_plan(new))
        with self.assertRaisesRegex(TraceError, "different board"):
            sync(make_plan(graph()), "another-board", self.state_path, token="test-token", transport=self.remote, interval=0)
        self.assertEqual(len(self.remote.calls), calls)

    def test_local_preview_never_uses_network_credentials_or_mutates_files(self):
        plan = make_plan(graph())
        target = Path(self.tmp.name) / "not-created" / "state.json"
        def fail(*args):
            self.fail("Dry run must not call network transport")
        with patch.dict("os.environ", {}, clear=True):
            report = sync(plan, "board=", target, dry_run=True, transport=fail)
        self.assertEqual(report["new_items"], 7)
        self.assertTrue(report["remote_preflight_required"])
        self.assertFalse(target.parent.exists())
        self.sync(plan)
        before = self.state_path.read_bytes()
        with patch.dict("os.environ", {}, clear=True):
            report = sync(make_plan(graph("two", True)), "board=", self.state_path, dry_run=True, transport=fail)
        self.assertEqual(report["new_items"], 5)
        self.assertEqual(report["mapped_shapes"], 4)
        self.assertEqual(self.state_path.read_bytes(), before)

    def test_new_item_budget_is_checked_before_network(self):
        with self.assertRaisesRegex(TraceError, "7 new items"):
            self.sync(make_plan(graph()), max_items=6)
        self.assertEqual(self.remote.calls, [])
        self.assertFalse(self.state_path.exists())
        self.sync(make_plan(graph()))
        count = len(self.remote.calls)
        with self.assertRaisesRegex(TraceError, "5 new items"):
            self.sync(make_plan(graph("two", True)), max_items=4)
        self.assertEqual(len(self.remote.calls), count)

    def test_uncertain_post_requires_reconciliation_then_resumes_without_duplicates(self):
        plan = make_plan(graph())
        self.remote.lose_next_post = True
        with self.assertRaisesRegex(TraceError, "lost response"):
            self.sync(plan)
        state = read_json(self.state_path)
        self.assertEqual(state["pending"]["key"], "legend")
        self.assertIn("body", state["pending"])
        with self.assertRaisesRegex(TraceError, "outcome is uncertain"):
            self.sync(plan)
        self.assertEqual(len(self.remote.writes), 1)
        resolve(self.state_path, item_id="remote-1")
        record = read_json(self.state_path)["items"]["legend"]
        self.assertIn("managed", record)
        self.assertIn("intent", record)
        result = self.sync(plan)
        self.assertEqual(result["created"], 6)
        self.assertEqual(len(self.remote.items), 7)
        self.assertEqual(self.item("legend")["id"], "remote-1")

    def test_uncertain_connector_reconciliation_keeps_endpoints_and_captions(self):
        plan = make_plan(graph())
        original = self.remote
        def transport(method, url, *args):
            if method == "POST" and url.endswith("/connectors"):
                original.lose_next_post = True
            return original(method, url, *args)
        with self.assertRaisesRegex(TraceError, "lost response"):
            sync(plan, "board=", self.state_path, token="test-token", transport=transport, interval=0)
        pending = read_json(self.state_path)["pending"]
        self.assertEqual(pending["endpoint"], "connectors")
        resolve(self.state_path, item_id="remote-6")
        result = self.sync(plan)
        self.assertEqual(result["created"], 1)
        self.assertEqual(len(self.remote.items), 7)
        self.assertEqual(self.item("input:1:0")["startItem"]["id"], self.item("addr:a")["id"])

    def test_server_normalization_does_not_cause_spurious_patch(self):
        self.remote.normalize = True
        g = graph()
        g["nodes"][0]["label"] = "Address A\nSecond line"
        plan = make_plan(g)
        self.sync(plan)
        writes = len(self.remote.writes)
        report = self.sync(plan, max_items=0)
        self.assertEqual(report["updated"], 0)
        self.assertEqual(report["conflicts"], [])
        self.assertEqual(len(self.remote.writes), writes)

    def test_plan_schema_checksum_and_shape_geometry_guards(self):
        legacy = graph()
        del legacy["namespace"]
        with self.assertRaisesRegex(TraceError, "schema 2"):
            self.sync(make_plan(legacy))
        bad = make_plan(graph())
        bad["shapes"][0]["body"]["position"]["x"] = float("nan")
        bad["sha256"] = digest(canonical({k: v for k, v in bad.items() if k != "sha256"}))
        with self.assertRaisesRegex(TraceError, "Malformed Miro plan"):
            self.sync(bad)
        bad = make_plan(graph())
        bad["run_id"] = "tampered"
        with self.assertRaisesRegex(TraceError, "checksum mismatch"):
            self.sync(bad)
        self.assertEqual(self.remote.calls, [])

    def test_run_notes_are_distinct_and_legend_is_stable(self):
        one, two = make_plan(graph()), make_plan(graph("two"))
        self.assertEqual(one["shapes"][0], two["shapes"][0])
        self.assertEqual(one["shapes"][1]["key"], "run:one")
        self.assertEqual(two["shapes"][1]["key"], "run:two")
        self.assertIn("max_hops", two["shapes"][1]["body"]["data"]["content"])

    def test_frame_relative_coordinates_abort_before_any_writes(self):
        self.sync(make_plan(graph()))
        item = self.item("addr:a")
        item["parent"] = {"id": "frame-99"}
        item["position"]["relativeTo"] = "parent_top_left"
        before = self.state_path.read_bytes()
        writes = len(self.remote.writes)
        with self.assertRaisesRegex(TraceError, "frame/group-relative coordinates"):
            self.sync(make_plan(graph("two", True)))
        self.assertEqual(len(self.remote.writes), writes)
        self.assertEqual(self.state_path.read_bytes(), before)

    def test_new_batch_clears_rotated_existing_bounds(self):
        self.sync(make_plan(graph()))
        item = self.item("addr:a")
        item["position"]["x"] = 10000
        item["geometry"] = {"width": 100, "height": 1200, "rotation": 90}
        self.sync(make_plan(graph("two", True)))
        for key in ("run:two", "tx:2", "addr:c"):
            new = self.item(key)
            self.assertGreaterEqual(new["position"]["x"] - new["geometry"]["width"] / 2, 10900)
        self.assertEqual(self.item("addr:a")["geometry"], {"width": 100, "height": 1200, "rotation": 90})

    def test_stale_run_and_independent_branch_rejected_but_skipped_continuation_allowed(self):
        one = make_plan(graph())
        self.sync(one)
        self.sync(make_plan(graph("two", True)))
        calls = len(self.remote.calls)
        with self.assertRaisesRegex(TraceError, "older or independent branch"):
            self.sync(one)
        independent = graph("independent")
        independent["run"].update({"parent_run": None, "ancestor_runs": []})
        with self.assertRaisesRegex(TraceError, "older or independent branch"):
            self.sync(make_plan(independent))
        self.assertEqual(len(self.remote.calls), calls)
        skipped = graph("four", True)
        skipped["run"].update({"parent_run": "three", "ancestor_runs": ["one", "two", "three"]})
        self.assertEqual(self.sync(make_plan(skipped))["runs"], 3)

    def test_interrupted_sync_blocks_different_run_even_after_post_reconciled(self):
        self.sync(make_plan(graph()))
        self.remote.lose_next_post = True
        with self.assertRaisesRegex(TraceError, "lost response"):
            self.sync(make_plan(graph("two", True)))
        resolve(self.state_path, item_id="remote-8")
        calls = len(self.remote.calls)
        with self.assertRaisesRegex(TraceError, "interrupted Miro sync for run two"):
            self.sync(make_plan(graph()))
        self.assertEqual(len(self.remote.calls), calls)
        self.sync(make_plan(graph("two", True)))
        self.assertIsNone(read_json(self.state_path)["active_run_id"])

    def test_http_408_remains_pending_for_incremental_and_legacy_paths(self):
        def timeout(*args):
            return 408, {}, b"{}"
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                target = Path(self.tmp.name) / ("legacy.json" if legacy else "sync.json")
                g = graph()
                if legacy:
                    del g["namespace"]
                call = publish if legacy else sync
                with self.assertRaisesRegex(TraceError, "HTTP 408"):
                    call(make_plan(g), "board=", target, token="test-token", transport=timeout, interval=0)
                self.assertEqual(read_json(target)["pending"]["key"], "legend")


if __name__ == "__main__":
    unittest.main()
