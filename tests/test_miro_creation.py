"""Initial publication, unordered bulk responses, and uncertain POST recovery."""

import copy
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from liquid_tracer.common import TraceError, canonical, read_json, save_json
from liquid_tracer.miro import _shape_batches, make_plan, resolve, sync
from liquid_tracer.miro_requests import MiroRequestNotSent, MiroRequests
from liquid_tracer.miro_state import SyncState, load_state
from liquid_tracer import miro_requests
from tests.test_miro_sync import FakeMiro, graph


class MiroCreationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "miro.json"
        self.remote = FakeMiro()

    def sync(self, plan=None, transport=None, **kwargs):
        return sync(plan or make_plan(graph()), "synthetic-board=", self.path,
                    token="synthetic-token", transport=transport or self.remote,
                    interval=0, max_items=1000, **kwargs)

    def test_twenty_shape_batches_match_reversed_responses_and_survive_repeat(self):
        value = graph()
        template = value["nodes"][0]
        value["nodes"] = [{**template, "id": "addr:" + str(i), "label": "Synthetic address " + str(i),
                           "x": i * 300, "y": 200} for i in range(57)]
        value["edges"] = []
        plan = make_plan(value)
        batch_sizes = []

        def reverse(method, url, headers, body, timeout):
            result = self.remote(method, url, headers, body, timeout)
            if method == "POST" and url.endswith("/items/bulk"):
                batch_sizes.append(len(json.loads(body)))
                response = json.loads(result[2])
                # The official generic Item response omits style and may use
                # a null parent for a root-canvas item. Never infer input order.
                for item in response["data"]:
                    item.pop("style", None)
                    item["parent"] = None
                response["data"].reverse()
                return result[0], result[1], canonical(response)
            return result

        report = self.sync(plan, transport=reverse)
        state = read_json(self.path)
        self.assertEqual(batch_sizes, [20, 20, 19])
        self.assertEqual(report["created"], 59)
        self.assertEqual(len(self.remote.writes), 3)
        for item in plan["shapes"]:
            remote = self.remote.items[state["items"][item["key"]]["id"]]
            self.assertEqual(remote["data"]["content"], item["body"]["data"]["content"])
        prior = copy.deepcopy(state["items"])
        # Partial bulk response bodies must not cause destructive guesses about
        # user colors during a later complete, per-shape preflight.
        selected = self.remote.items[state["items"]["addr:1"]["id"]]
        selected["style"]["fillColor"] = "#aabbcc"
        report = self.sync(plan, transport=reverse)
        self.assertEqual(report["created"], 0)
        self.assertEqual(report["updated"], 0)
        self.assertEqual(selected["style"]["fillColor"], "#aabbcc")
        self.assertEqual(read_json(self.path)["items"], prior)
        self.assertEqual(len(self.remote.writes), 3)

    def test_spatially_ambiguous_inputs_are_split_before_bulk_dispatch(self):
        pending = []
        for key, x in (("one", 0), ("two", .005), ("three", 300)):
            pending.append({"key": key, "body": {"data": {"shape": "circle"}, "position": {"x": x, "y": 0}}})
        self.assertEqual([[item["key"] for item in batch] for batch in _shape_batches(pending)],
                         [["one"], ["two", "three"]])

    def test_bad_bulk_responses_keep_every_member_uncertain_and_never_replay(self):
        mutations = {
            "missing_member": lambda data: data.pop(),
            "duplicate_id": lambda data: data[1].update(id=data[0]["id"]),
            "duplicate_position": lambda data: data[1].update(position=copy.deepcopy(data[0]["position"]),
                                                               data=copy.deepcopy(data[0]["data"])),
            "wrong_type": lambda data: data[0].update(type="connector"),
            "missing_position": lambda data: data[0].pop("position"),
            "invalid_position": lambda data: data[0].update(position=[]),
            "invalid_parent": lambda data: data[0].update(parent="frame"),
            "remote_parent": lambda data: data[0].update(parent={"id": "frame"}),
            "nonfinite_position": lambda data: data[0]["position"].update(x=float("inf")),
            "wrong_position": lambda data: data[0]["position"].update(x=123456789),
        }
        plan = make_plan(graph())
        for name, mutate in mutations.items():
            with self.subTest(case=name):
                self.path = Path(self.temporary.name) / (name + ".json")
                self.remote = FakeMiro()

                def malformed(method, url, headers, body, timeout):
                    result = self.remote(method, url, headers, body, timeout)
                    if url.endswith("/items/bulk"):
                        response = json.loads(result[2])
                        mutate(response["data"])
                        return result[0], result[1], canonical(response)
                    return result

                with self.assertRaisesRegex(TraceError, "reconcile"):
                    self.sync(plan, transport=malformed)
                state = read_json(self.path)
                self.assertEqual(state["items"], {})
                self.assertEqual(set(state["pending_creations"]), {item["key"] for item in plan["shapes"]})
                self.assertEqual(len({item["operation_id"] for item in state["pending_creations"].values()}), 1)
                before = len(self.remote.calls)
                with self.assertRaisesRegex(TraceError, "outcome is uncertain"):
                    self.sync(plan)
                self.assertEqual(len(self.remote.calls), before)

    def test_bulk_rejection_can_be_retried_but_server_error_cannot(self):
        for code in (400, 401, 403, 422, 408, 500, 503):
            with self.subTest(status=code):
                self.path = Path(self.temporary.name) / (str(code) + ".json")
                calls = []
                def reject(*args):
                    calls.append(args[0])
                    return code, {}, b"{}"
                with self.assertRaisesRegex(TraceError, "HTTP " + str(code)):
                    self.sync(transport=reject)
                state = read_json(self.path)
                self.assertEqual(bool(state.get("pending_creations")), code in (408, 500, 503))
                self.assertEqual(calls, ["POST"])

    def test_lost_bulk_response_resolves_individual_keys_and_resumes_without_duplicates(self):
        plan = make_plan(graph())
        self.remote.lose_next_post = True
        with self.assertRaisesRegex(TraceError, "lost response"):
            self.sync(plan)
        pending = read_json(self.path)["pending_creations"]
        with self.assertRaisesRegex(TraceError, "--key"):
            resolve(self.path, absent=True)
        for index, (key, entry) in enumerate(pending.items()):
            found = [item_id for item_id, body in self.remote.items.items()
                     if body["data"]["content"] == entry["body"]["data"]["content"]]
            self.assertEqual(len(found), 1)
            resolve(self.path, item_id=found[0], key=key)
            if index < len(pending) - 1:
                with self.assertRaisesRegex(TraceError, "outcome is uncertain"):
                    self.sync(plan)
        self.assertEqual(self.sync(plan)["created"], len(plan["connectors"]))
        self.assertEqual(len(self.remote.items), len(plan["shapes"]) + len(plan["connectors"]))
        self.assertEqual(read_json(self.path)["pending_creations"], {})

    def test_failed_parallel_connector_saves_other_acknowledgement_and_leaves_only_uncertain_intent(self):
        plan = make_plan(graph("one", True))
        first_started, second_started = threading.Event(), threading.Event()
        observed = []
        lock = threading.Lock()

        def transport(method, url, headers, body, timeout):
            payload = json.loads(body) if body else None
            if method == "POST" and url.endswith("/connectors"):
                with lock:
                    observed.append(payload)
                    ordinal = len(observed)
                if ordinal == 1:
                    first_started.set()
                    if not second_started.wait(3):
                        raise AssertionError("Second connector did not overlap")
                    return 503, {}, b"{}"
                if ordinal == 2:
                    second_started.set()
                    if not first_started.wait(3):
                        raise AssertionError("First connector did not overlap")
                    time.sleep(.03)
            with lock:
                return self.remote(method, url, headers, body, timeout)

        with self.assertRaisesRegex(TraceError, "HTTP 503"):
            self.sync(plan, transport=transport, workers=2)
        state = read_json(self.path)
        self.assertEqual(len(observed), 2)
        self.assertEqual(set(state["pending_creations"]), {plan["connectors"][0]["key"]})
        self.assertIn(plan["connectors"][1]["key"], state["items"])
        self.assertEqual({entry["id"] for entry in state["items"].values()}, set(self.remote.items))
        for item in plan["connectors"][2:]:
            self.assertNotIn(item["key"], state["items"])
            self.assertNotIn(item["key"], state["pending_creations"])
        # The fake server proves the rejected request was never committed.
        resolve(self.path, key=plan["connectors"][0]["key"], absent=True)
        self.assertEqual(self.sync(plan)["created"], 3)
        self.assertEqual(len(self.remote.items), len(plan["shapes"]) + len(plan["connectors"]))

    def test_proven_unsent_connector_clears_intent_and_drains_started_creations(self):
        plan = make_plan(graph("one", True))
        original_send = MiroRequests.send
        refused = []
        def refuse(requests, method, url, headers=None, body=None, timeout=30, **kwargs):
            if method == "POST" and url.endswith("/connectors") and not refused:
                refused.append(True)
                raise MiroRequestNotSent("Synthetic stopped dispatch")
            return original_send(requests, method, url, headers, body, timeout, **kwargs)
        with patch.object(MiroRequests, "send", refuse), self.assertRaisesRegex(TraceError, "stopped dispatch"):
            self.sync(plan)
        state = read_json(self.path)
        self.assertEqual(state.get("pending_creations", {}), {})
        self.assertEqual({entry["id"] for entry in state["items"].values()}, set(self.remote.items))
        self.sync(plan)
        self.assertEqual(len(self.remote.items), len(plan["shapes"]) + len(plan["connectors"]))

    def test_parallel_creations_have_durable_intents_before_transport(self):
        observed = []
        def inspect(method, url, headers, body, timeout):
            if method == "POST":
                pending = load_state(self.path).get("pending_creations", {})
                payload = json.loads(body)
                bodies = [entry["body"] for entry in pending.values()]
                if isinstance(payload, list):
                    for item in payload:
                        self.assertIn({key: value for key, value in item.items() if key != "type"}, bodies)
                else:
                    self.assertIn(payload, bodies)
                observed.append(len(pending))
            return self.remote(method, url, headers, body, timeout)
        self.sync(transport=inspect)
        self.assertTrue(observed)
        self.assertEqual(read_json(self.path)["pending_creations"], {})

    def test_cancel_while_shape_batch_waits_for_quota_leaves_no_uncertain_items(self):
        waiting = threading.Event()
        interrupted = False
        original_start = MiroRequests._start
        original_wait = miro_requests.wait

        def wait_for_quota(requests, *args, **kwargs):
            waiting.set()
            requests._wait(60)
            return original_start(requests, *args, **kwargs)

        def interrupt_wait(futures, *args, **kwargs):
            nonlocal interrupted
            if waiting.is_set() and not interrupted:
                interrupted = True
                raise KeyboardInterrupt
            return original_wait(futures, *args, **kwargs)

        with patch.object(MiroRequests, "_start", wait_for_quota), patch.object(miro_requests, "wait", interrupt_wait):
            with self.assertRaises(KeyboardInterrupt):
                self.sync()
        self.assertTrue(interrupted)
        self.assertEqual(self.remote.calls, [])
        state = read_json(self.path)
        self.assertEqual(state.get("pending_creations", {}), {})
        self.assertEqual(state["items"], {})
        self.assertEqual(self.sync()["created"], 7)

    def test_repeated_interrupts_drain_all_acknowledged_connectors(self):
        both_started, release = threading.Event(), threading.Event()
        lock = threading.Lock()
        started = 0
        interrupts = 0
        original_wait = miro_requests.wait

        def transport(method, url, headers, body, timeout):
            nonlocal started
            if method == "POST" and url.endswith("/connectors"):
                with lock:
                    started += 1
                    if started == 2:
                        both_started.set()
                if not release.wait(3):
                    raise AssertionError("Coordinator did not drain connector requests")
            with lock:
                return self.remote(method, url, headers, body, timeout)

        def interrupt_wait(futures, *args, **kwargs):
            nonlocal interrupts
            if not both_started.is_set():
                return original_wait(futures, *args, **kwargs)
            if interrupts < 2:
                interrupts += 1
                raise KeyboardInterrupt
            release.set()
            return original_wait(futures, *args, **kwargs)

        with patch.object(miro_requests, "wait", interrupt_wait), self.assertRaises(KeyboardInterrupt):
            self.sync(transport=transport, workers=2)
        self.assertEqual(interrupts, 2)
        state = read_json(self.path)
        self.assertEqual(state["pending_creations"], {})
        self.assertEqual({record["id"] for record in state["items"].values()}, set(self.remote.items))
        self.assertEqual(len(state["items"]), 7)
        self.assertEqual(self.sync()["created"], 0)

    def test_failed_acknowledgement_checkpoint_preserves_uncertain_batch(self):
        original_commit = SyncState.commit
        failed = False
        def fail_ack(journal, sets=(), deletes=()):
            nonlocal failed
            if not failed and any(path[0] == "items" for path, _ in sets):
                failed = True
                raise TraceError("Synthetic acknowledgement checkpoint failure")
            return original_commit(journal, sets=sets, deletes=deletes)
        with patch.object(SyncState, "commit", fail_ack), self.assertRaisesRegex(TraceError, "checkpoint failure"):
            self.sync()
        state = read_json(self.path)
        self.assertEqual(state["items"], {})
        self.assertEqual(len(state["pending_creations"]), 5)
        self.assertEqual(len(self.remote.items), 5)
        before = len(self.remote.calls)
        with self.assertRaisesRegex(TraceError, "outcome is uncertain"):
            self.sync()
        self.assertEqual(len(self.remote.calls), before)

    def test_legacy_schema_two_uncertain_item_still_reconciles_and_migrates(self):
        plan = make_plan(graph())
        item = plan["shapes"][0]
        legacy = {"schema_version": 2, "board_id": "synthetic-board=", "namespace": plan["namespace"],
                  "items": {}, "runs": {}, "pending": {"key": item["key"], "endpoint": "shapes",
                  "body": item["body"], "run_id": plan["run_id"]}}
        save_json(self.path, legacy)
        _, _, raw = self.remote("POST", "https://api.miro.com/v2/boards/synthetic/shapes", {}, canonical(item["body"]), 30)
        resolve(self.path, item_id=json.loads(raw)["id"])
        report = self.sync(plan)
        self.assertEqual(report["created"], 6)
        self.assertEqual(len(self.remote.items), 7)
        self.assertIsNone(read_json(self.path)["pending"])


if __name__ == "__main__":
    unittest.main()
