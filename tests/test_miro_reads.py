import copy
import json
import threading
import unittest
from urllib.parse import parse_qs, unquote, urlsplit

from liquid_tracer.common import TraceError
from liquid_tracer.miro_reads import preflight
from liquid_tracer.miro_requests import MiroRequests


BASE = "https://api.miro.com/v2/boards/synthetic"


def fixture(count=101, shapes=2):
    records, bodies = {}, {}
    for index in range(shapes):
        key = "shape:" + str(index)
        body = {"id": "shape/" + str(index), "type": "shape", "parent": {"id": "manual-frame"},
                "geometry": {"width": 100, "height": 100, "rotation": 17},
                "position": {"x": index * 300, "y": 0, "origin": "center", "relativeTo": "parent_top_left"},
                "data": {"content": "Investigator annotation"}, "style": {"fillColor": "#ff0000"}}
        records[key] = {"id": body["id"], "endpoint": "shapes", "managed": {"style": {"fillColor": "#000000"}},
                        "intent": {"style": {"fillColor": "#000000"}}}
        bodies[body["id"]] = body
    for index in range(count):
        key = "edge:" + str(index)
        body = {"id": "connector/" + str(index), "type": "connector", "shape": "curved",
                "startItem": {"id": "shape/0", "snapTo": "right"},
                "endItem": {"id": "shape/1", "snapTo": "left"},
                "captions": [{"content": "Manual caption " + str(index), "position": "50%"}],
                "style": {"strokeColor": "#123456", "strokeWidth": "2"}}
        records[key] = {"id": body["id"], "endpoint": "connectors",
                        "managed": {"style": {"strokeColor": "#000000", "strokeWidth": "2"}},
                        "intent": {"style": {"strokeColor": "#000000", "strokeWidth": "2"}}}
        bodies[body["id"]] = body
    return {"items": records}, bodies


class PageTransport:
    def __init__(self, bodies):
        self.bodies = copy.deepcopy(bodies)
        self.connectors = [copy.deepcopy(body) for body in bodies.values() if body["type"] == "connector"]
        self.calls = []
        self.pages = None

    def __call__(self, method, url, headers=None, body=None, timeout=30):
        self.calls.append((method, url))
        if method != "GET":
            raise AssertionError("Preflight must perform only reads")
        parsed = urlsplit(url)
        if parsed.path.endswith("/connectors"):
            query = parse_qs(parsed.query)
            assert query.get("limit") == ["50"]
            cursor = query.get("cursor", [""])[0]
            if self.pages is not None:
                response = self.pages[cursor]
            else:
                start = int(cursor or 0)
                response = {"data": self.connectors[start:start + 50]}
                if start + 50 < len(self.connectors):
                    response["cursor"] = str(start + 50)
            return 200, {}, json.dumps(response).encode()
        item_id = unquote(parsed.path.rsplit("/", 1)[-1])
        response = self.bodies.get(item_id)
        return (200 if response is not None else 404), {}, json.dumps(response).encode()


class MiroReadsTests(unittest.TestCase):
    def read(self, state, transport, removals=None, progress=None):
        with MiroRequests(transport, interval=0, workers=4) as requests:
            return preflight(requests, BASE, {"Authorization": "Bearer synthetic-token"}, state,
                             removals or {}, progress)

    def test_complete_connector_pages_reduce_reads_and_preserve_all_manual_fields(self):
        state, bodies = fixture()
        before = copy.deepcopy(state)
        transport = PageTransport(bodies)
        remote = self.read(state, transport)
        self.assertEqual(len(transport.calls), 5)  # Three connector pages and two full shape reads.
        self.assertEqual(remote, {key: bodies[record["id"]] for key, record in state["items"].items()})
        self.assertEqual(state, before)
        self.assertTrue(all("/items?" not in url for _, url in transport.calls))

    def test_small_boards_keep_individual_reads(self):
        state, bodies = fixture(count=50)
        transport = PageTransport(bodies)
        self.read(state, transport)
        self.assertEqual(len(transport.calls), 52)
        self.assertTrue(all("?" not in url for _, url in transport.calls))

    def test_partial_page_fields_get_fresh_individual_body(self):
        state, bodies = fixture(count=51)
        state["pending_updates"] = {"edge:5": {"patch": {"style": {"fontSize": "12"}}}}
        bodies["connector/5"]["style"]["fontSize"] = "18"
        transport = PageTransport(bodies)
        del transport.connectors[0]["style"]
        del transport.connectors[1]["captions"]
        del transport.connectors[2]["startItem"]
        del transport.connectors[3]["style"]["strokeWidth"]
        del transport.connectors[4]["captions"][0]["position"]
        del transport.connectors[5]["style"]["fontSize"]
        remote = self.read(state, transport)
        self.assertEqual(len(transport.calls), 10)
        for index in range(6):
            self.assertEqual(remote["edge:" + str(index)], bodies["connector/" + str(index)])

    def test_empty_captions_are_a_real_manual_deletion(self):
        state, bodies = fixture(count=51)
        bodies["connector/0"]["captions"] = []
        transport = PageTransport(bodies)
        remote = self.read(state, transport)
        self.assertEqual(remote["edge:0"]["captions"], [])
        self.assertEqual(len(transport.calls), 4)

    def test_omitted_list_id_is_checked_individually(self):
        state, bodies = fixture(count=51)
        transport = PageTransport(bodies)
        transport.connectors.pop()
        remote = self.read(state, transport)
        self.assertEqual(remote["edge:50"], bodies["connector/50"])
        self.assertEqual(len(transport.calls), 4)

    def test_empty_page_with_cursor_falls_back_to_individual_checks(self):
        state, bodies = fixture(count=51, shapes=0)
        transport = PageTransport(bodies)
        transport.pages = {"": {"data": [], "cursor": "unused"}}
        remote = self.read(state, transport)
        self.assertEqual(len(remote), 51)
        self.assertEqual(len(transport.calls), 52)

    def test_missing_item_is_never_silently_recreated(self):
        state, bodies = fixture(count=51)
        transport = PageTransport(bodies)
        transport.connectors.pop()
        del transport.bodies["connector/50"]
        with self.assertRaisesRegex(TraceError, "missing or inaccessible mapped items.*edge:50"):
            self.read(state, transport)
        state["pending_deletions"] = {"edge:50": {"attempted": True}}
        remote = self.read(state, transport, {"edge:50": {}})
        self.assertNotIn("edge:50", remote)

    def test_stop_after_all_mapped_ids_and_encode_cursor_on_original_host(self):
        state, bodies = fixture(count=51)
        transport = PageTransport(bodies)
        cursor = "https://untrusted.invalid/?cursor=next&token=other"
        transport.pages = {
            "": {"data": transport.connectors[:50], "cursor": cursor,
                 "links": {"next": "https://untrusted.invalid/"}},
            cursor: {"data": transport.connectors[50:], "cursor": "unneeded-page"},
        }
        self.read(state, transport)
        self.assertEqual(len(transport.calls), 4)
        self.assertTrue(all(url.startswith(BASE + "/") for _, url in transport.calls))
        self.assertTrue(any(parse_qs(urlsplit(url).query).get("cursor") == [cursor]
                            for _, url in transport.calls))

    def test_cursor_loop_and_conflicting_duplicate_abort_before_writes(self):
        state, bodies = fixture(count=51)
        for conflict in (False, True):
            with self.subTest(conflict=conflict):
                transport = PageTransport(bodies)
                repeated = copy.deepcopy(transport.connectors[0])
                if conflict:
                    repeated["style"]["strokeColor"] = "#654321"
                transport.pages = {
                    "": {"data": [transport.connectors[0]], "cursor": "next"},
                    "next": {"data": [repeated], "cursor": "other" if conflict else "next"},
                }
                with self.assertRaisesRegex(TraceError, "changed between pages|repeated a cursor"):
                    self.read(state, transport)

    def test_consistent_duplicate_does_not_double_count_progress(self):
        state, bodies = fixture(count=51)
        transport = PageTransport(bodies)
        transport.pages = {
            "": {"data": transport.connectors[:50], "cursor": "next"},
            "next": {"data": transport.connectors[49:]},
        }
        events, callback_threads = [], []
        class Progress:
            def emit(self, phase, completed, total, message):
                events.append((completed, total))
                callback_threads.append(threading.get_ident())
        self.read(state, transport, progress=Progress())
        self.assertEqual(events[-1], (53, 53))
        self.assertTrue(all(completed <= total for completed, total in events))
        self.assertEqual(set(callback_threads), {threading.get_ident()})

    def test_unsupported_connector_aborts_even_in_complete_page(self):
        state, bodies = fixture(count=51)
        transport = PageTransport(bodies)
        transport.connectors[0]["isSupported"] = False
        with self.assertRaisesRegex(TraceError, "unsupported mapped connector"):
            self.read(state, transport)

    def test_invalid_collection_json_and_http_error_abort(self):
        state, _ = fixture(count=51, shapes=0)
        for status, body in ((403, {}), (200, []), (200, {"data": {}}),
                             (200, {"data": [], "cursor": 123}), (200, {"data": [None]})):
            with self.subTest(status=status, body=body):
                def transport(*_args):
                    return status, {}, json.dumps(body).encode()
                with self.assertRaisesRegex(TraceError, "no board writes made"):
                    self.read(state, transport)
        for raw in (b"{", b"\xff"):
            with self.subTest(raw=raw):
                with self.assertRaisesRegex(TraceError, "invalid JSON.*no board writes made"):
                    self.read(state, lambda *_args: (200, {}, raw))


if __name__ == "__main__":
    unittest.main()
