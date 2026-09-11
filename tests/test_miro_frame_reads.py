import copy
import json
import unittest
from urllib.parse import parse_qs, urlsplit

from liquid_tracer.common import TraceError
from liquid_tracer.miro import _bounds
from liquid_tracer.miro_reads import check_empty_frames, preflight, validate_frame_children
from liquid_tracer.miro_requests import MiroRequests
from tests.test_miro_reads import BASE, PageTransport, fixture


FRAME_KEY = "frame:activity:synthetic"
FRAME_ID = "frame/synthetic"
FRAME_PROOF = {"schema_version": 1, "key": FRAME_KEY, "kind": "activity"}


def framed_fixture():
    state, bodies = fixture(count=0, shapes=1)
    body = {"id": FRAME_ID, "type": "frame", "data": {"title": "Activity 1"},
            "position": {"x": 1000, "y": 800, "origin": "center", "relativeTo": "canvas_center"},
            "geometry": {"width": 600, "height": 400}}
    state["items"][FRAME_KEY] = {"id": FRAME_ID, "endpoint": "frames", "managed": {}, "intent": {},
                                 "frame_proof": copy.deepcopy(FRAME_PROOF)}
    bodies[FRAME_ID] = body
    bodies["shape/0"]["parent"] = {"id": FRAME_ID}
    bodies["shape/0"]["position"].update(x=200, y=150)
    return state, bodies


class FrameTransport(PageTransport):
    def __init__(self, bodies):
        super().__init__(bodies)
        self.child_pages = {FRAME_ID: {"data": []}}
        self.child_status = 200

    def __call__(self, method, url, headers=None, body=None, timeout=30):
        parsed = urlsplit(url)
        if parsed.path.endswith("/items"):
            self.calls.append((method, url))
            assert method == "GET"
            query = parse_qs(parsed.query)
            assert query.get("limit") in (["1"], ["50"])
            response = self.child_pages[query["parent_item_id"][0]]
            if isinstance(response, dict) and "pages" in response:
                response = response["pages"][query.get("cursor", [""])[0]]
            raw = response if isinstance(response, bytes) else json.dumps(response).encode()
            return self.child_status, {}, raw
        return super().__call__(method, url, headers, body, timeout)


class MiroFrameReadsTests(unittest.TestCase):
    def read(self, state, transport, removals=None):
        with MiroRequests(transport, interval=0, workers=4) as requests:
            return preflight(requests, BASE, {}, state, removals or {})

    def check_empty(self, state, transport):
        records = {key: record for key, record in state["items"].items() if record["endpoint"] == "frames"}
        with MiroRequests(transport, interval=0, workers=4) as requests:
            check_empty_frames(requests, BASE, {}, records)

    def validate_children(self, state, transport):
        remote = self.read(state, transport)
        records = {key: record for key, record in state["items"].items() if record["endpoint"] == "frames"}
        with MiroRequests(transport, interval=0, workers=4) as requests:
            validate_frame_children(requests, BASE, {}, state, remote, records)

    def test_reads_frame_endpoint_and_normalizes_only_managed_child(self):
        state, bodies = framed_fixture()
        before = copy.deepcopy((state, bodies))
        transport = FrameTransport(bodies)
        remote = self.read(state, transport)
        self.assertEqual((state, bodies), before)
        self.assertEqual(len(transport.calls), 2)
        self.assertTrue(any("/frames/frame%2Fsynthetic" in url for _, url in transport.calls))
        self.assertEqual(remote[FRAME_KEY], bodies[FRAME_ID])
        child = remote["shape:0"]
        self.assertEqual(child["position"], {"x": 900, "y": 750, "origin": "center", "relativeTo": "canvas_center"})
        self.assertEqual(child["parent"], {"id": FRAME_ID})
        self.assertEqual(child["_frame_source_position"], bodies["shape/0"]["position"])
        self.assertEqual(child["style"], bodies["shape/0"]["style"])
        self.assertEqual(child["data"], bodies["shape/0"]["data"])
        self.assertEqual(_bounds(child, "shape:0")[:2], (900, 750))

    def test_manual_frame_move_and_size_are_used_instead_of_saved_layout(self):
        state, bodies = framed_fixture()
        state["items"][FRAME_KEY]["intent"] = {"position": {"x": 0, "y": 0},
                                                 "geometry": {"width": 100, "height": 100}}
        bodies[FRAME_ID]["position"].update(x=-700, y=2500)
        bodies[FRAME_ID]["geometry"].update(width=800, height=700)
        remote = self.read(state, FrameTransport(bodies))
        self.assertEqual(_bounds(remote["shape:0"], "shape:0")[:2], (-900, 2300))

    def test_explicit_canvas_coordinates_are_not_translated_twice(self):
        state, bodies = framed_fixture()
        bodies["shape/0"]["position"]["relativeTo"] = "canvas_center"
        remote = self.read(state, FrameTransport(bodies))
        expected = {**bodies["shape/0"], "_frame_source_position": bodies["shape/0"]["position"]}
        self.assertEqual(remote["shape:0"], expected)
        self.assertEqual(_bounds(remote["shape:0"], "shape:0")[:2], (200, 150))

    def test_external_frame_is_not_inferred_and_existing_layout_check_blocks_it(self):
        state, bodies = framed_fixture()
        bodies["shape/0"]["parent"]["id"] = "external-frame"
        remote = self.read(state, FrameTransport(bodies))
        self.assertEqual(remote["shape:0"], bodies["shape/0"])
        with self.assertRaisesRegex(TraceError, "frame/group-relative"):
            _bounds(remote["shape:0"], "shape:0")

    def test_explicit_canvas_child_of_external_frame_needs_no_guess(self):
        state, bodies = framed_fixture()
        bodies["shape/0"]["parent"]["id"] = "external-frame"
        bodies["shape/0"]["position"]["relativeTo"] = "canvas_center"
        remote = self.read(state, FrameTransport(bodies))
        self.assertEqual(_bounds(remote["shape:0"], "shape:0")[:2], (200, 150))

    def test_invalid_frame_type_geometry_origin_and_coordinate_system_abort(self):
        changes = [(("type",), "shape"), (("geometry",), {}), (("geometry", "width"), 0),
                   (("geometry", "height"), float("inf")), (("geometry", "rotation"), 45),
                   (("geometry", "width"), True), (("position", "x"), float("nan")),
                   (("position", "origin"), "top_left"), (("position", "relativeTo"), "parent_top_left"),
                   (("parent",), {"id": "external-frame"})]
        for path, value in changes:
            with self.subTest(path=path, value=value):
                state, bodies = framed_fixture()
                target = bodies[FRAME_ID]
                for part in path[:-1]:
                    target = target[part]
                target[path[-1]] = value
                transport = FrameTransport(bodies)
                with self.assertRaisesRegex(TraceError, "supported canvas geometry.*No board writes"):
                    self.read(state, transport)
                self.assertTrue(all(method == "GET" for method, _ in transport.calls))

    def test_managed_frame_with_self_or_cyclic_parent_is_rejected(self):
        for parent in (FRAME_ID, "other-frame"):
            with self.subTest(parent=parent):
                state, bodies = framed_fixture()
                bodies[FRAME_ID]["parent"] = {"id": parent}
                if parent != FRAME_ID:
                    state["items"]["frame:other"] = {"id": parent, "endpoint": "frames", "managed": {}, "intent": {}}
                    bodies[parent] = {**copy.deepcopy(bodies[FRAME_ID]), "id": parent, "parent": {"id": FRAME_ID}}
                with self.assertRaisesRegex(TraceError, "supported canvas geometry"):
                    self.read(state, FrameTransport(bodies))

    def test_ambiguous_or_malformed_child_coordinates_abort(self):
        for update in ({"relativeTo": None}, {"relativeTo": "unknown"}, {"origin": "top_left"},
                       {"x": float("inf")}, {"y": None}, {"x": True}):
            with self.subTest(update=update):
                state, bodies = framed_fixture()
                bodies["shape/0"]["position"].update(update)
                with self.assertRaisesRegex(TraceError, "managed-frame coordinates"):
                    self.read(state, FrameTransport(bodies))
        for parent in ([], {"id": []}):
            with self.subTest(parent=parent):
                state, bodies = framed_fixture()
                bodies["shape/0"]["parent"] = parent
                with self.assertRaisesRegex(TraceError, "malformed frame/group coordinates"):
                    self.read(state, FrameTransport(bodies))

    def test_missing_frame_only_allowed_for_matching_attempted_frame_deletion(self):
        state, bodies = framed_fixture()
        bodies["shape/0"].pop("parent")
        bodies["shape/0"]["position"]["relativeTo"] = "canvas_center"
        del bodies[FRAME_ID]
        removals = {FRAME_KEY: copy.deepcopy(FRAME_PROOF)}
        pending = {"id": FRAME_ID, "proof": copy.deepcopy(FRAME_PROOF), "attempted": True}
        for entry in ({}, {**pending, "attempted": False}, {**pending, "id": "wrong"},
                      {**pending, "proof": {"kind": "activity"}}):
            with self.subTest(entry=entry):
                state["pending_frame_deletions"] = {FRAME_KEY: entry}
                with self.assertRaisesRegex(TraceError, "missing or inaccessible mapped items"):
                    self.read(state, FrameTransport(bodies), removals)
        state["pending_frame_deletions"] = {FRAME_KEY: pending}
        self.assertNotIn(FRAME_KEY, self.read(state, FrameTransport(bodies), removals))
        with self.assertRaisesRegex(TraceError, "missing or inaccessible mapped items"):
            self.read(state, FrameTransport(bodies))

    def test_fee_deletion_intent_cannot_authorize_missing_frame(self):
        state, bodies = framed_fixture()
        del bodies[FRAME_ID]
        state["pending_deletions"] = {FRAME_KEY: {"attempted": True}}
        with self.assertRaisesRegex(TraceError, "missing or inaccessible mapped items"):
            self.read(state, FrameTransport(bodies), {FRAME_KEY: FRAME_PROOF})

    def test_missing_approved_frame_still_cannot_anchor_relative_child(self):
        state, bodies = framed_fixture()
        del bodies[FRAME_ID]
        state["pending_frame_deletions"] = {FRAME_KEY: {"id": FRAME_ID, "proof": FRAME_PROOF, "attempted": True}}
        with self.assertRaisesRegex(TraceError, "incomplete managed-frame coordinates"):
            self.read(state, FrameTransport(bodies), {FRAME_KEY: FRAME_PROOF})

    def test_empty_frame_check_uses_parent_filter_on_same_authenticated_board(self):
        state, bodies = framed_fixture()
        transport = FrameTransport(bodies)
        self.check_empty(state, transport)
        self.assertEqual(len(transport.calls), 1)
        method, url = transport.calls[0]
        self.assertEqual(method, "GET")
        self.assertTrue(url.startswith(BASE + "/items?"))
        self.assertEqual(parse_qs(urlsplit(url).query), {"parent_item_id": [FRAME_ID], "limit": ["1"]})

    def test_adopted_managed_or_manual_items_block_frame_mutations(self):
        for item_id in ("shape/0", "unrelated-investigator-note"):
            with self.subTest(item_id=item_id):
                state, bodies = framed_fixture()
                transport = FrameTransport(bodies)
                transport.child_pages[FRAME_ID] = {"data": [{"id": item_id}]}
                with self.assertRaisesRegex(TraceError, "contains attached items.*progress is saved"):
                    self.check_empty(state, transport)

    def test_child_check_rejects_inconclusive_or_malformed_responses(self):
        for response in ({}, {"data": {}}, {"data": [], "cursor": 123}, {"data": [], "cursor": "next"},
                         {"data": [], "total": 1}, b"{", b"\xff"):
            with self.subTest(response=response):
                state, bodies = framed_fixture()
                transport = FrameTransport(bodies)
                transport.child_pages[FRAME_ID] = response
                with self.assertRaisesRegex(TraceError, "progress is saved"):
                    self.check_empty(state, transport)
        state, bodies = framed_fixture()
        transport = FrameTransport(bodies)
        transport.child_status = 403
        with self.assertRaisesRegex(TraceError, "HTTP 403.*progress is saved"):
            self.check_empty(state, transport)

    def test_children_inventory_accepts_verified_managed_shape(self):
        state, bodies = framed_fixture()
        transport = FrameTransport(bodies)
        transport.child_pages[FRAME_ID] = {"data": [{"id": "shape/0", "type": "shape"}]}
        self.validate_children(state, transport)
        self.assertTrue(any(parse_qs(urlsplit(url).query).get("limit") == ["50"]
                            for _, url in transport.calls))

    def test_children_inventory_rejects_unknown_items_and_child_frames(self):
        for child in ({"id": "unrelated-note", "type": "shape"}, {"id": FRAME_ID, "type": "frame"},
                      {"id": "shape/0", "type": "connector"}):
            with self.subTest(child=child):
                state, bodies = framed_fixture()
                transport = FrameTransport(bodies)
                transport.child_pages[FRAME_ID] = {"data": [child]}
                with self.assertRaisesRegex(TraceError, "outside this trace.*No board writes"):
                    self.validate_children(state, transport)

    def test_children_inventory_requires_complete_parent_agreement(self):
        state, bodies = framed_fixture()
        transport = FrameTransport(bodies)
        with self.assertRaisesRegex(TraceError, "inventory disagrees"):
            self.validate_children(state, transport)
        bodies["shape/0"]["position"]["relativeTo"] = "canvas_center"
        bodies["shape/0"]["parent"] = {"id": "other-frame"}
        transport = FrameTransport(bodies)
        transport.child_pages[FRAME_ID] = {"data": [{"id": "shape/0", "type": "shape"}]}
        with self.assertRaisesRegex(TraceError, "positions changed or could not be verified"):
            self.validate_children(state, transport)

    def test_children_inventory_paginates_with_escaped_cursor_on_same_board(self):
        state, bodies = framed_fixture()
        for index in range(1, 51):
            shape_id, shape_key = "shape/" + str(index), "shape:" + str(index)
            bodies[shape_id] = {**copy.deepcopy(bodies["shape/0"]), "id": shape_id}
            state["items"][shape_key] = {**copy.deepcopy(state["items"]["shape:0"]), "id": shape_id}
        children = [{"id": "shape/" + str(index), "type": "shape"} for index in range(51)]
        cursor = "https://untrusted.invalid/?page=2&token=other"
        transport = FrameTransport(bodies)
        transport.child_pages[FRAME_ID] = {"pages": {"": {"data": children[:50], "cursor": cursor},
                                                    cursor: {"data": children[50:]}}}
        self.validate_children(state, transport)
        collection_calls = [url for _, url in transport.calls if "/items?" in url]
        self.assertEqual(len(collection_calls), 2)
        self.assertEqual(parse_qs(urlsplit(collection_calls[1]).query)["cursor"], [cursor])
        self.assertTrue(all(url.startswith(BASE + "/items?") for url in collection_calls))

    def test_children_inventory_rejects_repeated_cursor_and_changed_duplicate(self):
        child = {"id": "shape/0", "type": "shape"}
        for second in ({"data": [child], "cursor": "next"},
                       {"data": [{**child, "parent": {"id": "unexpected"}}]}, {"data": [], "cursor": "other"}):
            with self.subTest(second=second):
                state, bodies = framed_fixture()
                transport = FrameTransport(bodies)
                transport.child_pages[FRAME_ID] = {"pages": {"": {"data": [child], "cursor": "next"}, "next": second}}
                with self.assertRaisesRegex(TraceError, "pagination|changed between pages"):
                    self.validate_children(state, transport)

    def test_children_inventory_rejects_malformed_collection_and_http_error(self):
        for response in ({}, {"data": [] , "cursor": 123}, {"data": [None]}, {"data": [{"id": []}]}, b"{"):
            with self.subTest(response=response):
                state, bodies = framed_fixture()
                transport = FrameTransport(bodies)
                transport.child_pages[FRAME_ID] = response
                with self.assertRaisesRegex(TraceError, "no board writes"):
                    self.validate_children(state, transport)
        state, bodies = framed_fixture()
        transport = FrameTransport(bodies)
        transport.child_status = 403
        with self.assertRaisesRegex(TraceError, "HTTP 403.*no board writes"):
            self.validate_children(state, transport)


if __name__ == "__main__":
    unittest.main()
