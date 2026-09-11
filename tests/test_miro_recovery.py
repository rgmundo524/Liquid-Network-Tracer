"""Explicit empty-board recovery never repeats a POST or guesses item identity."""

import copy
import fcntl
import json
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from liquid_tracer.common import TraceError, canonical, save_json
from liquid_tracer.miro import make_plan, sync
from liquid_tracer.miro_recovery import initial_pending_batch, recover_empty_board
from liquid_tracer.miro_requests import MiroRequests
from liquid_tracer.miro_state import SyncState, load_state
from tests.test_miro_sync import FakeMiro, NAMESPACE, graph


BOARD = "synthetic-board="


class EmptyBoard:
    def __init__(self):
        self.calls = []
        self.responses = {name: (200, {}, canonical({"data": [], "total": 0, "size": 0}))
                          for name in ("items", "connectors")}

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method, url, body))
        if method != "GET":
            raise AssertionError("Recovery attempted a board write")
        self.assert_headers = headers
        query = parse_qs(urlsplit(url).query)
        if query != {"limit": ["10"]}:
            return 400, {}, canonical({"code": "badRequest", "message": "limit must be between 10 and 50"})
        return self.responses[urlsplit(url).path.rsplit("/", 1)[-1]]


class MiroRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "miro.json"
        value = graph()
        template = value["nodes"][0]
        value["nodes"] = [{**template, "id": "addr:" + str(index), "x": index * 300, "y": 200}
                          for index in range(24)]
        value["edges"] = []
        self.plan = make_plan(value)
        with self.assertRaisesRegex(TraceError, "HTTP 500"):
            sync(self.plan, BOARD, self.path, token="synthetic-token",
                 transport=lambda *args: (500, {}, b"{}"), interval=0)
        self.initial = load_state(self.path)
        self.remote = EmptyBoard()

    def recover(self, **kwargs):
        options = {"confirmed_empty": True, "token": "synthetic-token", "transport": self.remote, "interval": 0}
        options.update(kwargs)
        return recover_empty_board(self.path, BOARD, NAMESPACE, **options)

    def assert_unchanged(self, expected=None):
        self.assertEqual(load_state(self.path), self.initial if expected is None else expected)

    def test_confirmed_empty_twenty_item_batch_recovers_atomically_and_preserves_lineage(self):
        self.assertEqual(len(self.initial["pending_creations"]), 20)
        events = []
        report = self.recover(progress=events.append)
        self.assertEqual(report, {"recovered_items": 20, "shape_batch_size": 1,
                                  "board_id": BOARD, "run_id": "one", "recovery": "confirmed_empty_board"})
        recovered = load_state(self.path)
        self.assertEqual(recovered["pending_creations"], {})
        self.assertEqual(recovered["shape_batch_size"], 1)
        audit = recovered["recovery_history"][0]
        self.assertEqual(audit["recovered_keys"], sorted(self.initial["pending_creations"]))
        self.assertEqual(audit["run_id"], self.initial["active_run_id"])
        self.assertEqual(audit["operation_id"], next(iter(self.initial["pending_creations"].values()))["operation_id"])
        self.assertTrue(audit["confirmed_empty"])
        self.assertEqual(audit["board_id"], BOARD)
        self.assertTrue(audit["checked_at"])
        expected = {**self.initial, "pending_creations": {}, "shape_batch_size": 1,
                    "recovery_history": recovered["recovery_history"]}
        self.assertEqual(recovered, expected)
        self.assertEqual(self.remote.calls, [("GET", "https://api.miro.com/v2/boards/synthetic-board%3D/items?limit=10", None),
                                            ("GET", "https://api.miro.com/v2/boards/synthetic-board%3D/connectors?limit=10", None)])
        self.assertEqual([event["completed"] for event in events], [0, 1, 2])
        self.assertNotIn("synthetic-token", json.dumps(recovered))
        # The next normal sync still owns creation and resumes the same run.
        target = FakeMiro()
        result = sync(self.plan, BOARD, self.path, token="synthetic-token", transport=target, interval=0)
        self.assertEqual(result["created"], len(self.plan["shapes"]))
        self.assertTrue(all(not url.endswith("/items/bulk") for _, url, _ in target.writes))

    def test_confirmation_must_be_explicit_boolean(self):
        for value in (False, None, 1, "true"):
            with self.subTest(value=value), self.assertRaisesRegex(TraceError, "--confirm-empty"):
                self.recover(confirmed_empty=value)
            self.assert_unchanged()
        self.assertEqual(self.remote.calls, [])

    def test_board_or_namespace_mismatch_and_legacy_state_are_rejected_without_requests(self):
        for changed in ({"board_id": "another-board"}, {"namespace": {**NAMESPACE, "case_id": "another-case"}},
                        {"schema_version": 1}):
            state = {**self.initial, **changed}
            save_json(self.path, state)
            with self.subTest(changed=changed), self.assertRaises(TraceError):
                self.recover()
            self.assert_unchanged(state)
        self.assertEqual(self.remote.calls, [])

    def test_missing_state_or_credentials_do_not_initialize_recovery(self):
        with patch.dict("os.environ", {}, clear=True), self.assertRaisesRegex(TraceError, "MIRO_ACCESS_TOKEN"):
            self.recover(token=None)
        self.assert_unchanged()
        self.path.unlink()
        with self.assertRaisesRegex(TraceError, "state does not exist"):
            self.recover()
        self.assertFalse(self.path.exists())
        self.assertEqual(self.remote.calls, [])

    def test_nonempty_either_collection_retains_all_pending_items(self):
        for collection in ("items", "connectors"):
            self.remote = EmptyBoard()
            self.remote.responses[collection] = (200, {}, canonical({"data": [{"id": "existing-item"}]}))
            with self.subTest(collection=collection), self.assertRaisesRegex(TraceError, "board contains"):
                self.recover()
            self.assert_unchanged()

    def test_empty_but_incomplete_or_malformed_inventory_is_not_absence(self):
        bodies = [[], {}, {"data": None}, {"data": [] , "cursor": "next-page"}, {"data": [], "cursor": 0},
                  {"data": [], "cursor": []}, {"data": [], "total": 1}, {"data": [], "total": False},
                  {"data": [], "total": "0"}, {"data": [], "size": 1}, {"data": [], "size": False},
                  {"data": [], "links": {"next": "https://other.example/page"}}, {"data": [], "links": []},
                  {"data": [], "links": {"next": False}}]
        for body in bodies:
            self.remote = EmptyBoard()
            self.remote.responses["connectors"] = (200, {}, canonical(body))
            with self.subTest(body=body), self.assertRaisesRegex(TraceError, "incomplete|complete empty"):
                self.recover()
            self.assert_unchanged()
            self.assertEqual(len(self.remote.calls), 2)
        self.remote.responses["connectors"] = (200, {}, b"not JSON; synthetic sensitive server text")
        with self.assertRaisesRegex(TraceError, "invalid JSON") as caught:
            self.recover()
        self.assertNotIn("sensitive", str(caught.exception))
        self.assert_unchanged()
        self.remote.responses["connectors"] = (200, {}, b"[" * 2000 + b"]" * 2000)
        with self.assertRaisesRegex(TraceError, "invalid JSON|incomplete"):
            self.recover()
        self.assert_unchanged()

    def test_optional_empty_page_fields_can_be_omitted(self):
        self.remote.responses["items"] = (200, {}, canonical({"data": []}))
        self.remote.responses["connectors"] = (200, {}, canonical({"data": [], "cursor": "", "links": {"next": None}}))
        self.assertEqual(self.recover()["recovered_items"], 20)

    def test_failed_reads_retry_only_gets_and_keep_pending(self):
        def skip_wait(requests, _delay):
            requests._cooldown_until = 0
        for status in (401, 403, 429, 500):
            self.remote = EmptyBoard()
            self.remote.responses["items"] = (status, {"Retry-After": "1"}, b"sensitive synthetic server response")
            with self.subTest(status=status), patch.object(MiroRequests, "_wait", skip_wait), \
                    self.assertRaisesRegex(TraceError, "HTTP " + str(status)) as caught:
                self.recover()
            self.assertNotIn("sensitive", str(caught.exception))
            self.assertEqual(len(self.remote.calls), 4 if status in (429, 500) else 1)
            self.assert_unchanged()

    def test_read_diagnostics_identify_collection_without_exposing_content_or_credentials(self):
        request_id = "11111111-2222-4333-8444-555555555555"
        for collection in ("items", "connectors"):
            self.remote = EmptyBoard()
            self.remote.responses[collection] = (400, {"X-Request-Id": request_id}, canonical({
                "code": "badRequest", "message": "private board title synthetic-token",
                "context": {"Authorization": "Bearer synthetic-token"},
            }))
            with self.subTest(collection=collection), self.assertRaisesRegex(TraceError, "HTTP 400") as caught:
                self.recover()
            message = str(caught.exception)
            self.assertIn(collection, message)
            self.assertIn("code=badRequest", message)
            self.assertIn("request_id=" + request_id, message)
            self.assertNotIn("private board title", message)
            self.assertNotIn("synthetic-token", message)
            self.assertIn("pending items remain unchanged", message)
            self.assertTrue(all(method == "GET" for method, _, _ in self.remote.calls))
            self.assert_unchanged()

    def test_read_diagnostics_suppress_credentials_even_when_shaped_like_request_ids(self):
        token = "11111111-2222-4333-8444-555555555555"
        self.remote.responses["items"] = (400, {"X-Request-Id": token}, canonical({"code": "badRequest"}))
        with self.assertRaisesRegex(TraceError, "HTTP 400") as caught:
            self.recover(token=token)
        self.assertNotIn(token, str(caught.exception))
        self.assert_unchanged()

    def test_transport_failure_keeps_pending(self):
        def interrupted(*args):
            raise TraceError("Synthetic transport failure")
        with self.assertRaisesRegex(TraceError, "transport failure"):
            self.recover(transport=interrupted)
        self.assert_unchanged()

    def test_non_initial_state_and_other_pending_operations_are_rejected(self):
        cases = {
            "mapped": {"items": {"old": {"id": "remote-old", "endpoint": "shapes", "managed": {}, "intent": {}}}},
            "completed": {"runs": {"old": {}}}, "latest": {"latest_run_id": "old"},
            "updates": {"pending_updates": {"unknown": {}}},
            "deletions": {"pending_deletions": {"unknown": {}}},
            "frame_deletions": {"pending_frame_deletions": {"unknown": {}}},
            "malformed_deletions": {"pending_deletions": []},
            "legacy": {"pending": {"key": "old", "endpoint": "shapes"}},
            "wrong_run": {"active_run_id": "another-run"}, "missing_run": {"active_run_id": None},
            "missing_batch": {"pending_creations": {}}, "bad_history": {"recovery_history": {}},
        }
        for name, changes in cases.items():
            state = {**copy.deepcopy(self.initial), **changes}
            save_json(self.path, state)
            with self.subTest(name=name), self.assertRaises(TraceError):
                self.recover()
            self.assert_unchanged(state)
        self.assertEqual(self.remote.calls, [])

    def test_malformed_or_mixed_pending_batch_is_rejected(self):
        mutations = {
            "operation": lambda item: item.update(operation_id="invalid"),
            "mixed_operations": lambda item: item.update(operation_id="f" * 24),
            "connector": lambda item: item.update(endpoint="connectors"),
            "wrong_key": lambda item: item.update(key="other"),
            "empty_body": lambda item: item.update(body={}),
            "invalid_position": lambda item: item["body"]["position"].update(x=True),
            "overflow_position": lambda item: item["body"]["position"].update(x=10 ** 400),
            "invalid_size": lambda item: item["body"]["geometry"].update(width=0),
        }
        for name, mutate in mutations.items():
            state = copy.deepcopy(self.initial)
            mutate(next(iter(state["pending_creations"].values())))
            save_json(self.path, state)
            with self.subTest(name=name), self.assertRaises(TraceError):
                self.recover()
            self.assert_unchanged(state)
        self.assertEqual(self.remote.calls, [])

    def test_recovery_refuses_active_publisher_lock(self):
        with self.path.with_suffix(".lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(TraceError, "Another publisher"):
                self.recover()
        self.assert_unchanged()
        self.assertEqual(self.remote.calls, [])

    def test_missing_active_journal_is_not_silently_replaced(self):
        state = {**self.initial, "_sync_journal": {"version": 1, "id": "synthetic-missing"}}
        save_json(self.path, state)
        original = self.path.read_bytes()
        with self.assertRaisesRegex(TraceError, "journal is missing"):
            self.recover()
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self.remote.calls, [])

    def test_atomic_commit_failure_preserves_pending_and_does_not_select_individual_mode(self):
        with patch.object(SyncState, "commit", side_effect=TraceError("Synthetic storage failure")), \
                self.assertRaisesRegex(TraceError, "storage failure"):
            self.recover()
        self.assert_unchanged()
        self.assertEqual(len(self.remote.calls), 2)

    def test_state_change_during_remote_reads_is_not_overwritten(self):
        changed = {**self.initial, "external_change": "preserve"}
        def changed_after_read(*args):
            result = self.remote(*args)
            if len(self.remote.calls) == 2:
                save_json(self.path, changed)
            return result
        with self.assertRaisesRegex(TraceError, "state changed"):
            self.recover(transport=changed_after_read)
        self.assert_unchanged(changed)

    def test_default_transport_reuses_miro_http_and_shared_token_quota(self):
        quota = Mock()
        quota.reserve.return_value = ("synthetic-reservation", 0)
        with patch("liquid_tracer.miro_recovery.MiroHTTP", return_value=nullcontext(self.remote)) as factory, \
                patch("liquid_tracer.miro_recovery.SharedMiroQuota", return_value=nullcontext(quota)) as quota_factory:
            result = recover_empty_board(self.path, BOARD, NAMESPACE, confirmed_empty=True,
                                         token="synthetic-token", interval=0)
        self.assertEqual(result["recovered_items"], 20)
        factory.assert_called_once_with()
        quota_factory.assert_called_once_with("synthetic-token")
        self.assertEqual(quota.reserve.call_count, 2)
        self.assertEqual(quota.finish.call_count, 2)

    def test_status_validator_has_no_mutation(self):
        self.assertEqual(initial_pending_batch(self.initial), self.initial["pending_creations"])
        self.assert_unchanged()
        for state in (None, [], {}, {**self.initial, "pending_creations": []},
                      {**self.initial, "pending_creations": {"invalid": None}}):
            with self.subTest(state=state), self.assertRaises(TraceError):
                initial_pending_batch(state)


if __name__ == "__main__":
    unittest.main()
