"""Creation diagnostics and explicitly selected per-board batch strategies."""

import copy
import json
import tempfile
import unittest
from pathlib import Path

from liquid_tracer.common import TraceError, canonical, read_json, save_json
from liquid_tracer.miro import _shape_batches, make_plan, publish, sync
from liquid_tracer.miro_errors import creation_error
from tests.test_miro_sync import FakeMiro, graph


REQUEST_ID = "00000000-0000-4000-8000-000000000001"
CORRELATION_ID = "0123456789abcdef0123456789abcdef"


class CreationDiagnosticTests(unittest.TestCase):
    def test_error_reports_only_operation_code_and_correlation_identifiers(self):
        result = creation_error(500, "items/bulk", 20, {
            "X-Request-ID": REQUEST_ID, "X-Correlation-ID": CORRELATION_ID,
            "Set-Cookie": "private-session-cookie",
        }, canonical({"code": "internalServerError", "message": "private graph content",
                      "context": {"fields": [{"message": "private seed"}]}}))
        self.assertEqual(result, "Miro POST returned HTTP 500 (items/bulk; 20 items; "
                         "code=internalServerError; request_id=" + REQUEST_ID +
                         "; correlation_id=" + CORRELATION_ID + ")")

    def test_content_and_arbitrary_error_fields_never_enter_diagnostic(self):
        for value in ("https://miro.com/private-board", "<p>private-text</p>",
                      "badRequest\nforged-line", "\x1b[2J", "a" * 64,
                      "unknownButPrivate", "private board name"):
            with self.subTest(value=value):
                result = creation_error(500, "shapes", 1, {"X-Request-ID": value},
                                        canonical({"code": value, "message": value,
                                                   "requestId": value, "details": value}))
                self.assertEqual(result, "Miro POST returned HTTP 500 (shapes; 1 item)")

    def test_authorization_echo_is_filtered_even_when_it_has_valid_id_format(self):
        result = creation_error(500, "shapes", 1, {"X-Request-ID": CORRELATION_ID},
                                canonical({"requestId": CORRELATION_ID,
                                           "message": "Bearer " + CORRELATION_ID}),
                                request_headers={"Authorization": "Bearer " + CORRELATION_ID})
        self.assertNotIn(CORRELATION_ID, result)
        code_echo = creation_error(500, "shapes", 1, {}, canonical({"code": "internalError"}),
                                   request_headers={"Authorization": "Bearer internalError"})
        self.assertNotIn("internalError", code_echo)

    def test_malformed_or_oversized_bodies_do_not_obscure_http_failure(self):
        for body in (b"\xff", b"<html>proxy error</html>", b"[1,2]", b"null",
                     b"x" * 16385, b"[" * 3000 + b"]" * 3000):
            with self.subTest(length=len(body)):
                result = creation_error(503, "connectors", 1, {"X-Request-ID": REQUEST_ID}, body)
                self.assertEqual(result, "Miro POST returned HTTP 503 (connectors; 1 item; "
                                 "request_id=" + REQUEST_ID + ")")

    def test_known_snake_case_error_and_body_request_id_are_supported(self):
        result = creation_error(400, "frames", 1, {}, canonical({
            "code": "BAD_REQUEST", "requestId": REQUEST_ID,
        }))
        self.assertIn("code=BAD_REQUEST", result)
        self.assertIn("request_id=" + REQUEST_ID, result)


class ShapeBatchStrategyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "miro.json"
        self.plan = make_plan(graph())
        self.remote = FakeMiro()

    def seed_strategy(self, size):
        save_json(self.path, {
            "schema_version": 2, "board_id": "synthetic-board=",
            "namespace": copy.deepcopy(self.plan["namespace"]),
            "items": {}, "runs": {}, "pending": None, "pending_creations": {},
            "shape_batch_size": size,
        })

    def sync(self, transport=None):
        return sync(self.plan, "synthetic-board=", self.path, token="synthetic-token",
                    transport=transport or self.remote, interval=0, max_items=1000)

    def test_individual_mode_uses_shape_endpoint_and_survives_restart(self):
        self.seed_strategy(1)
        self.sync()
        writes = [call for call in self.remote.calls if call[0] == "POST"]
        self.assertEqual(sum(call[1].endswith("/shapes") for call in writes), len(self.plan["shapes"]))
        self.assertFalse(any(call[1].endswith("/items/bulk") for call in writes))
        before = len(writes)
        self.assertEqual(read_json(self.path)["shape_batch_size"], 1)
        self.assertEqual(self.sync()["created"], 0)
        self.assertEqual(len([call for call in self.remote.calls if call[0] == "POST"]), before)

    def test_explicit_smaller_batch_preserves_bulk_plus_single_remainder(self):
        self.seed_strategy(2)
        observed = []

        def transport(method, url, headers, body, timeout):
            if method == "POST" and url.endswith(("/items/bulk", "/shapes")):
                payload = json.loads(body)
                observed.append(len(payload) if isinstance(payload, list) else 1)
            return self.remote(method, url, headers, body, timeout)

        self.sync(transport)
        count = len(self.plan["shapes"])
        self.assertEqual(observed, [2] * (count // 2) + ([1] if count % 2 else []))

    def test_single_failure_keeps_only_its_intent_and_never_retries_automatically(self):
        self.seed_strategy(1)
        calls = []

        def fail(method, url, headers, body, timeout):
            calls.append((method, url))
            return 500, {"X-Request-ID": REQUEST_ID}, canonical({"code": "internalError"})

        with self.assertRaisesRegex(TraceError, "HTTP 500.*shapes; 1 item.*request_id="):
            self.sync(fail)
        self.assertEqual(len(calls), 1)
        self.assertEqual(list(read_json(self.path)["pending_creations"]), ["legend"])
        with self.assertRaisesRegex(TraceError, "outcome is uncertain"):
            self.sync(fail)
        self.assertEqual(len(calls), 1)

    def test_bulk_failure_does_not_silently_change_strategy_or_replay(self):
        calls = []

        def fail(method, url, headers, body, timeout):
            calls.append((method, url))
            return 500, {"X-Request-ID": REQUEST_ID}, canonical({"code": "internalError"})

        with self.assertRaisesRegex(TraceError, "HTTP 500.*items/bulk.*code=internalError"):
            self.sync(fail)
        state = read_json(self.path)
        self.assertEqual(len(calls), 1)
        self.assertEqual(state.get("shape_batch_size", 20), 20)
        self.assertEqual(set(state["pending_creations"]), {item["key"] for item in self.plan["shapes"]})

    def test_invalid_batch_sizes_are_rejected(self):
        for size in (True, False, 0, 21, -1, 1.5, "1", None):
            with self.subTest(size=size), self.assertRaisesRegex(TraceError, "batch size"):
                list(_shape_batches([], size))

    def test_invalid_persisted_batch_size_fails_before_network_or_state_writes(self):
        for size in (True, False, 0, 21, -1, 1.5, "1", None):
            with self.subTest(size=size):
                self.seed_strategy(size)
                before = self.path.read_bytes()
                with self.assertRaisesRegex(TraceError, "batch"):
                    self.sync()
                self.assertEqual(self.remote.calls, [])
                self.assertEqual(self.path.read_bytes(), before)

    def test_legacy_publish_also_shows_safe_diagnostics_and_keeps_uncertain_item(self):
        def fail(*args):
            return 500, {"X-Request-ID": REQUEST_ID}, canonical({"message": "private text"})

        with self.assertRaisesRegex(TraceError, "HTTP 500.*shapes; 1 item.*request_id=") as raised:
            publish(self.plan, "synthetic-board=", self.path, token="synthetic-token", transport=fail, interval=0)
        self.assertNotIn("private text", str(raised.exception))
        self.assertEqual(read_json(self.path)["pending"]["key"], "legend")
