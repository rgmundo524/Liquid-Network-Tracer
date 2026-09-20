"""Safe, actionable diagnostics for rejected Miro item updates."""

import hashlib
import unittest

from liquid_tracer.common import canonical
from liquid_tracer.miro_errors import update_error


REQUEST_ID = "00000000-0000-4000-8000-000000000001"
CORRELATION_ID = "0123456789abcdef0123456789abcdef"


class UpdateDiagnosticTests(unittest.TestCase):
    def test_validation_identifies_item_fields_and_correlation_without_values(self):
        result = update_error(400, "connectors", {"X-Request-ID": REQUEST_ID}, canonical({
            "code": "invalidParameters", "correlationId": CORRELATION_ID,
            "message": "private rejected content",
            "context": {"field": "startItem.position.x", "value": "private coordinate"},
        }), item_id="3458764598765432101", patch={
            "startItem": {"id": "private source", "position": {"x": "private coordinate", "y": "50%"}},
            "captions": [{"content": "private transaction text", "position": "50%"}],
        })
        self.assertIn("Miro PATCH returned HTTP 400 (connectors; code=invalidParameters", result)
        self.assertIn("request_id=" + REQUEST_ID, result)
        self.assertIn("correlation_id=" + CORRELATION_ID, result)
        self.assertIn("item_id=3458764598765432101", result)
        self.assertIn("rejected_fields=startItem.position.x", result)
        self.assertIn("submitted_fields=captions[].content,captions[].position,startItem.id,startItem.position.x,startItem.position.y", result)
        self.assertIn("Repeating the same request will not fix", result)
        self.assertNotIn("rerun sync", result)
        for private in ("private rejected content", "private coordinate", "private source", "private transaction text", "50%"):
            self.assertNotIn(private, result)

    def test_common_validation_structures_are_supported(self):
        cases = [
            ({"context": {"fields": ["position.x", "geometry.width"]}}, "geometry.width,position.x"),
            ({"context": {"fields": {"position.y": "private value"}}}, "position.y"),
            ({"errors": [{"property": "style.fillColor"}, {"path": "$.body.captions[0].content"}]}, "captions[].content,style.fillColor"),
            ({"context": {"errors": [{"path": "/startItem/position/x"}]}}, "startItem.position.x"),
            ({"violations": [{"pointer": "/captions/123/content"}]}, "captions[].content"),
            ({"context": {"fields": [{"field": "endItem.snapTo"}]}}, "endItem.snapTo"),
        ]
        for data, expected in cases:
            with self.subTest(data=data):
                result = update_error(422, "shapes", {}, canonical(data))
                self.assertIn("rejected_fields=" + expected, result)

    def test_unrecognized_paths_and_message_content_are_never_extracted(self):
        private = "private-seed-address"
        result = update_error(400, "shapes", {}, canonical({
            "code": private, "message": "position.x",
            "context": {"field": private, "value": "geometry.width", "fields": [
                "position.x\nforged", "https://example.com/private", "data." + private,
                "captions[999999999].content", "captions[" + private + "].content",
                {"message": "style.fillColor", "property": "position." + private},
            ]},
        }), patch={private: "data.content", "style": {private: "style.color"}})
        self.assertNotIn(private, result)
        self.assertNotIn("rejected_fields=", result)
        self.assertNotIn("geometry.width", result)
        self.assertNotIn("style.fillColor", result)
        self.assertNotIn("forged", result)

    def test_transaction_hash_identifier_becomes_opaque_reference(self):
        item = "c" * 64
        first = update_error(400, "shapes", {}, b"{}", item_id=item)
        second = update_error(400, "shapes", {}, b"{}", item_id=item)
        reference = hashlib.sha256(item.encode()).hexdigest()[:12]
        self.assertIn("item_ref=" + reference, first)
        self.assertNotIn(item, first)
        self.assertEqual(first, second)
        for private in ("private-address", "1234567890" * 7, "<p>private label</p>", "bad\nforged", "\ud800"):
            with self.subTest(item=private):
                result = update_error(400, "shapes", {}, b"{}", item_id=private)
                self.assertIn("item_ref=", result)
                self.assertNotIn(private, result)

    def test_authorization_echo_cannot_enter_code_ids_or_field_diagnostics(self):
        for secret, data, headers, item in (
            (CORRELATION_ID, {"requestId": CORRELATION_ID}, {"X-Correlation-ID": CORRELATION_ID}, CORRELATION_ID),
            ("internalError", {"code": "internalError"}, {}, None),
            ("3458764598765432101", {}, {}, "3458764598765432101"),
            ("data.content", {"context": {"field": "data.content"}}, {}, None),
        ):
            with self.subTest(secret=secret):
                result = update_error(400, "shapes", headers, canonical(data),
                    item_id=item, patch={"data": {"content": "private text"}},
                    request_headers={"Authorization": "Bearer " + secret})
                self.assertNotIn(secret, result)
                self.assertNotIn("private text", result)

    def test_invalid_response_ids_and_codes_are_omitted(self):
        for value in ("unknownButPrivate", "a" * 64, "\x1b[2J", "badRequest\nforged", [], {}):
            with self.subTest(value=value):
                result = update_error(400, "shapes", {"X-Request-ID": value}, canonical({
                    "code": value, "requestId": value, "correlationId": value,
                }))
                self.assertNotIn("code=", result)
                self.assertNotIn("request_id=", result)
                self.assertNotIn("correlation_id=", result)

    def test_bearer_scheme_matching_is_case_insensitive(self):
        result = update_error(400, "shapes", {"X-Request-ID": CORRELATION_ID}, b"{}",
                              request_headers={"authorization": "bearer " + CORRELATION_ID})
        self.assertNotIn(CORRELATION_ID, result)

    def test_malformed_and_oversized_responses_keep_the_original_http_error(self):
        for raw in (b"\xff", b"<html>private proxy error</html>", b"[1,2]", b"null",
                    b"x" * 16385, b"[" * 3000 + b"]" * 3000, None, {}, 123):
            with self.subTest(raw_type=type(raw).__name__):
                result = update_error(503, "connectors", {"X-Request-ID": REQUEST_ID}, raw)
                self.assertIn("Miro PATCH returned HTTP 503 (connectors; request_id=" + REQUEST_ID, result)
                self.assertIn("reconcile the saved update", result)
                self.assertNotIn("private proxy error", result)

    def test_unexpected_argument_types_do_not_mask_failure(self):
        for value in (None, [], {}, 1, True):
            with self.subTest(value=value):
                result = update_error(400, value, value, canonical({"context": value}),
                    patch=value, item_id=value, request_headers=value, retry_action=value)
                self.assertTrue(result.startswith("Miro PATCH returned HTTP 400 (items"))
        self.assertIn("HTTP unknown", update_error("private status", "shapes", {}, b"{}"))

    def test_cyclic_or_deep_python_patch_is_bounded(self):
        patch = {"data": {"content": "private text"}}
        patch["parent"] = patch
        result = update_error(400, "shapes", {}, b"{}", patch=patch)
        self.assertIn("submitted_fields=", result)
        self.assertNotIn("private text", result)

    def test_actionable_guidance_distinguishes_http_failure_types(self):
        cases = {
            400: "correct the rejected fields", 422: "correct the rejected fields",
            401: "check or refresh MIRO_ACCESS_TOKEN", 403: "boards:write scope",
            404: "check the linked board and item", 409: "refresh and reconcile",
            429: "wait for it to reset", 408: "read the current item",
            500: "read the current item", 503: "read the current item",
            405: "inspect these diagnostics",
        }
        for status, instruction in cases.items():
            with self.subTest(status=status):
                result = update_error(status, "shapes", {}, b"{}")
                self.assertIn(instruction, result)
                self.assertIn("acknowledged progress is saved", result)

    def test_frame_retry_guidance_is_specific_and_arbitrary_text_is_ignored(self):
        result = update_error(503, "frames", {}, b"{}", retry_action="Create / update Miro frames")
        self.assertIn("rerun Create / update Miro frames", result)
        result = update_error(503, "frames", {}, b"{}", retry_action="private\nforged")
        self.assertIn("rerun sync", result)
        self.assertNotIn("private", result)


if __name__ == "__main__":
    unittest.main()
