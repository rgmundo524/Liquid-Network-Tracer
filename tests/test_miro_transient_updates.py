import copy
import json
import unittest
from unittest.mock import patch as mocked

from liquid_tracer.common import TraceError, canonical
from liquid_tracer.miro_requests import MiroRequestNotSent, MiroRequests
from liquid_tracer.miro_updates import patch_item


URL = "https://api.miro.com/v2/boards/board/shapes/shape-1"
HEADERS = {"Authorization": "Bearer private-token"}


def shape():
    return {"id": "shape-1", "type": "shape", "data": {"content": "old", "shape": "rectangle"},
            "style": {"fontSize": "14", "borderColor": "#aabbcc"},
            "position": {"x": 0, "y": 20, "origin": "center", "relativeTo": "canvas_center"},
            "geometry": {"width": 160, "height": 100}}


def response(body, status=200):
    return status, {}, canonical(body)


class Scripted:
    def __init__(self, *steps):
        self.steps = list(steps)
        self.calls = []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method, url, json.loads(body) if body is not None else None))
        expected, result = self.steps.pop(0)
        if method != expected:
            raise AssertionError(f"Expected {expected}, got {method}")
        if callable(result):
            return result()
        if isinstance(result, BaseException):
            raise result
        return result


class TransientUpdateTests(unittest.TestCase):
    def execute(self, transport, submitted=None, baseline=None, endpoint="shapes", item_id="shape-1", **kwargs):
        submitted = {"data": {"content": "updated"}} if submitted is None else submitted
        baseline = shape() if baseline is None else baseline
        with MiroRequests(transport, interval=0) as requests:
            with mocked.object(requests, "_wait") as wait:
                result = patch_item(requests, URL, HEADERS, submitted, baseline, endpoint, item_id, **kwargs)
        self.assertEqual(transport.steps, [])
        return result, wait

    def test_acknowledged_update_returns_server_normalization(self):
        body = shape()
        body["data"]["content"] = "<p>updated<br /></p>"
        transport = Scripted(("PATCH", response(body)))
        result, wait = self.execute(transport, {"data": {"content": "<p>updated<br></p>"}})
        self.assertEqual(result, body)
        wait.assert_not_called()

    def test_rejected_requests_are_not_replayed_or_read(self):
        for status in (400, 401, 403, 404, 422):
            with self.subTest(status=status):
                transport = Scripted(("PATCH", response({"code": "invalidParameters", "message": "private-token"}, status)))
                with self.assertRaisesRegex(TraceError, "HTTP " + str(status)) as caught:
                    self.execute(transport)
                self.assertEqual([call[0] for call in transport.calls], ["PATCH"])
                self.assertNotIn("private-token", str(caught.exception))

    def test_transient_rejection_reads_baseline_before_replaying(self):
        desired = shape()
        desired["data"]["content"] = "updated"
        for status in (409, 500, 502, 503, 504):
            with self.subTest(status=status):
                transport = Scripted(("PATCH", response({}, status)), ("GET", response(shape())),
                                     ("PATCH", response(desired)))
                result, wait = self.execute(transport)
                self.assertEqual(result, desired)
                self.assertEqual([call[0] for call in transport.calls], ["PATCH", "GET", "PATCH"])
                self.assertEqual(transport.calls[0][2], transport.calls[2][2])
                wait.assert_called_once_with(1)

    def test_readback_accepts_an_update_applied_despite_lost_response(self):
        desired = shape()
        desired["data"]["content"] = "updated"
        for result in (TraceError("private-token transport failure"), response({}, 503)):
            with self.subTest(result=type(result).__name__):
                transport = Scripted(("PATCH", result), ("GET", response(desired)))
                actual, wait = self.execute(transport)
                self.assertEqual(actual, desired)
                self.assertEqual([call[0] for call in transport.calls], ["PATCH", "GET"])
                wait.assert_not_called()

    def test_readback_compares_all_submitted_fields_and_normalized_numbers(self):
        desired = shape()
        desired["style"] = {"fontSize": 18.0, "borderColor": "#AABBCC", "fillColor": "#ffffff"}
        desired["position"]["x"] = 120.0
        submitted = {"style": {"fontSize": "18", "borderColor": "#aabbcc"}, "position": {"x": 120}}
        transport = Scripted(("PATCH", response({}, 500)), ("GET", response(desired)))
        actual, _ = self.execute(transport, submitted)
        self.assertEqual(actual, desired)

    def test_unchanged_normalized_style_allows_bounded_replay(self):
        current = shape()
        current["style"]["fontSize"] = 14
        current["style"]["borderColor"] = "#AABBCC"
        desired = copy.deepcopy(current)
        desired["style"]["fontSize"] = 18
        submitted = {"style": {"fontSize": "18", "borderColor": "#000000"}}
        transport = Scripted(("PATCH", response({}, 500)), ("GET", response(current)),
                             ("PATCH", response(desired)))
        self.execute(transport, submitted)

    def test_three_failed_attempts_stop_with_saved_progress(self):
        transport = Scripted(*[(method, response({}, 503) if method == "PATCH" else response(shape()))
                               for method in ("PATCH", "GET") * 3])
        with self.assertRaisesRegex(TraceError, "persisted after 3 attempts") as caught:
            self.execute(transport)
        self.assertEqual([call[0] for call in transport.calls], ["PATCH", "GET"] * 3)
        self.assertIn("acknowledged progress is saved", str(caught.exception))

    def test_last_failed_attempt_can_still_be_reconciled_as_applied(self):
        desired = shape()
        desired["data"]["content"] = "updated"
        transport = Scripted(("PATCH", response({}, 500)), ("GET", response(shape())),
                             ("PATCH", response({}, 500)), ("GET", response(shape())),
                             ("PATCH", response({}, 500)), ("GET", response(desired)))
        actual, wait = self.execute(transport)
        self.assertEqual(actual, desired)
        self.assertEqual([call.args for call in wait.call_args_list], [(1,), (2,)])

    def test_manual_change_and_partial_application_stop_without_replay(self):
        for content in ("analyst note", "updated"):
            with self.subTest(content=content):
                current = shape()
                current["data"]["content"] = content
                submitted = {"data": {"content": "updated"}, "style": {"fontSize": "18"}}
                transport = Scripted(("PATCH", response({}, 500)), ("GET", response(current)))
                with self.assertRaisesRegex(TraceError, "preserve manual edits"):
                    self.execute(transport, submitted)
                self.assertEqual(len(transport.calls), 2)

    def test_incomplete_or_wrong_readback_is_not_a_baseline(self):
        for body in ({"id": "shape-1", "type": "shape"},
                     {**shape(), "id": "another-item"}, {**shape(), "type": "frame"}, [], None):
            with self.subTest(body=body):
                transport = Scripted(("PATCH", response({}, 503)), ("GET", response(body)))
                with self.assertRaises(TraceError):
                    self.execute(transport)
                self.assertEqual(len(transport.calls), 2)

    def test_failed_verification_read_does_not_replay_or_echo_transport_error(self):
        for result in (response({}, 403), TraceError("private-token and private board text"), (200, {}, b"not json")):
            with self.subTest(result=type(result).__name__):
                transport = Scripted(("PATCH", TraceError("private-token")), ("GET", result))
                with self.assertRaisesRegex(TraceError, "could not be verified") as caught:
                    self.execute(transport)
                self.assertNotIn("private-token", str(caught.exception))
                self.assertEqual(len(transport.calls), 2)

    def test_uncertain_attachment_change_never_infers_fixed_mode_from_percentages(self):
        baseline = {"id": "connector-1", "type": "connector", "startItem": {"id": "a", "position": {"x": "100%", "y": "50%"}},
                    "endItem": {"id": "b"}, "shape": "straight"}
        submitted = {"startItem": copy.deepcopy(baseline["startItem"])}
        transport = Scripted(("PATCH", response({}, 500)), ("GET", response(baseline)))
        with self.assertRaisesRegex(TraceError, "cannot be safely verified"):
            self.execute(transport, submitted, baseline, "connectors", "connector-1")
        self.assertEqual(len(transport.calls), 2)

    def test_acknowledged_attachment_update_remains_supported(self):
        body = {"id": "connector-1", "type": "connector", "startItem": {"id": "a"}, "endItem": {"id": "b"}}
        transport = Scripted(("PATCH", response(body)))
        self.execute(transport, {"startItem": {"id": "a"}}, body, "connectors", "connector-1")

    def test_connector_readback_checks_identities_and_partial_caption_fields(self):
        baseline = {"id": "connector-1", "type": "connector", "startItem": {"id": "a"}, "endItem": {"id": "b"},
                    "captions": [{"id": "caption-id", "content": "old", "position": "50%"}]}
        desired = copy.deepcopy(baseline)
        desired["captions"][0]["content"] = "updated"
        submitted = {"captions": [{"content": "updated", "position": "50%"}]}
        transport = Scripted(("PATCH", response({}, 503)), ("GET", response(desired)))
        self.execute(transport, submitted, baseline, "connectors", "connector-1")
        desired["endItem"]["id"] = "manually-rewired"
        transport = Scripted(("PATCH", response({}, 503)), ("GET", response(desired)))
        with self.assertRaises(TraceError):
            self.execute(transport, submitted, baseline, "connectors", "connector-1")

    def test_parent_changed_during_content_update_stops_automatic_recovery(self):
        current = shape()
        current["data"]["content"] = "updated"
        current["parent"] = {"id": "manual-frame"}
        transport = Scripted(("PATCH", response({}, 500)), ("GET", response(current)))
        with self.assertRaises(TraceError):
            self.execute(transport)

    def test_parented_shape_movement_cannot_be_inferred_or_replayed(self):
        for applied in (False, True):
            with self.subTest(applied=applied):
                baseline = shape()
                baseline["parent"] = {"id": "frame"}
                current = copy.deepcopy(baseline)
                if applied:
                    current["position"]["x"] = 120
                transport = Scripted(("PATCH", response({}, 500)), ("GET", response(current)))
                with self.assertRaises(TraceError):
                    self.execute(transport, {"position": {"x": 120}}, baseline)

    def test_parent_relative_coordinates_cannot_be_replayed_even_without_parent_id(self):
        baseline = shape()
        baseline["position"]["relativeTo"] = "parent_top_left"
        transport = Scripted(("PATCH", response({}, 500)), ("GET", response(baseline)))
        with self.assertRaises(TraceError):
            self.execute(transport, {"position": {"x": 120}}, baseline)

    def test_frame_movement_or_parent_change_requires_explicit_recovery(self):
        frame = {"id": "frame-1", "type": "frame", "position": {"x": 0, "y": 0}}
        for submitted, baseline, endpoint, item_id in (
                ({"position": {"x": 120}}, frame, "frames", "frame-1"),
                ({"parent": None}, shape(), "shapes", "shape-1")):
            with self.subTest(submitted=submitted):
                transport = Scripted(("PATCH", response({}, 503)), ("GET", response(baseline)))
                with self.assertRaisesRegex(TraceError, "rerun Create / update Miro frames"):
                    self.execute(transport, submitted, baseline, endpoint, item_id,
                                 retry_action="Create / update Miro frames")

    def test_frozen_patch_and_baseline_cannot_be_changed_between_attempts(self):
        submitted, baseline = {"data": {"content": "updated"}}, shape()
        current = copy.deepcopy(baseline)

        def fail_and_mutate():
            submitted["data"]["content"] = "unexpected replacement"
            baseline["data"]["content"] = "unexpected baseline"
            return response({}, 503)

        transport = Scripted(("PATCH", fail_and_mutate), ("GET", response(current)), ("PATCH", response(current)))
        self.execute(transport, submitted, baseline)
        self.assertEqual(transport.calls[0][2], transport.calls[2][2])
        self.assertEqual(transport.calls[2][2], {"data": {"content": "updated"}})

    def test_cancellation_and_unsent_errors_propagate_without_new_requests(self):
        for error in (MiroRequestNotSent("not sent"), KeyboardInterrupt(), SystemExit()):
            with self.subTest(error=type(error).__name__):
                transport = Scripted(("PATCH", error))
                with self.assertRaises(type(error)) as caught:
                    self.execute(transport)
                self.assertIs(caught.exception, error)
                self.assertEqual(len(transport.calls), 1)
        error = MiroRequestNotSent("verification canceled")
        transport = Scripted(("PATCH", response({}, 500)), ("GET", error))
        with self.assertRaises(MiroRequestNotSent) as caught:
            self.execute(transport)
        self.assertIs(caught.exception, error)

    def test_invalid_success_acknowledgment_does_not_replay(self):
        for body in ({**shape(), "id": "wrong"}, {**shape(), "type": "frame"}, None):
            with self.subTest(body=body):
                transport = Scripted(("PATCH", response(body)))
                with self.assertRaisesRegex(TraceError, "wrong item ID/type"):
                    self.execute(transport)
                self.assertEqual(len(transport.calls), 1)


if __name__ == "__main__":
    unittest.main()
