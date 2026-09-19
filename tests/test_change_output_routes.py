"""Change designations through the real local HTTP boundary and lookup worker."""

import json
import unittest
from unittest.mock import patch

from liquid_tracer.common import TraceError, read_json
from liquid_tracer.inspection import transaction_outputs
from liquid_tracer.services import load_services
from tests import test_web


class ChangeOutputWebTests(unittest.TestCase):
    setUp = test_web.LocalWebTests.setUp
    close_server = test_web.LocalWebTests.close_server
    request = test_web.LocalWebTests.request
    success = test_web.LocalWebTests.success
    create = test_web.LocalWebTests.create
    wait = test_web.LocalWebTests.wait

    def prepare(self):
        txid, info = self.create()
        case, _ = self.server.case(info["id"])
        return txid, case, "/api/cases/" + info["id"]

    def test_fixture_lookup_worker_select_and_clear_without_starting_a_trace(self):
        txid, case, route = self.prepare()
        lookup = self.success(route + "/actions", {"action": "change-output-lookup", "txid": txid.upper()}, 202)
        self.assertFalse(lookup["live"])
        self.assertEqual(lookup["case_id"], read_json(case / "case.json")["case_id"])
        report = self.wait(lookup)
        self.assertEqual(report["txid"], txid)
        self.assertEqual(report["outputs"][0]["value_text"], "1000000")
        self.assertTrue(report["outputs"][0]["selectable"])
        self.assertFalse(report["outputs"][2]["selectable"])
        self.assertIsNone(report["current_vout"])
        with patch.object(self.server, "start_job") as worker, \
                patch("liquid_tracer.api.http", side_effect=AssertionError("Offline assignment")):
            saved = self.success(route + "/change-outputs", {
                "txid": txid, "vout": 1, "notes": "Investigator review", "expected_revision": report["revision"]})
            self.assertEqual(saved["changed"], 1)
            catalog = self.success(route + "/change-outputs", {"query": txid[:8].upper(), "limit": 1})
            self.assertEqual(catalog["total"], 1)
            self.assertEqual(catalog["rows"][0]["vout"], 1)
            cleared = self.success(route + "/change-outputs", {
                "txid": txid, "vout": None, "expected_revision": catalog["revision"]})
            self.assertEqual(cleared["changed"], 1)
            self.assertEqual(self.success(route + "/change-outputs", {})["total"], 0)
            worker.assert_not_called()
        self.assertNotIn("latest_run", read_json(case / "case.json"))

    def test_lookup_reads_saved_outputs_and_rejects_known_invalid_selections(self):
        txid, case, route = self.prepare()
        trace = self.wait(self.success(route + "/actions", {"action": "trace"}, 202))
        archive = case / "runs" / trace["run_id"]
        before = {str(p.relative_to(archive)): p.read_bytes() for p in archive.rglob("*") if p.is_file()}
        with patch("liquid_tracer.change_outputs._lookup_options", side_effect=AssertionError("Use saved output evidence")):
            lookup = self.success(route + "/actions", {"action": "change-output-lookup", "txid": txid}, 202)
        self.assertFalse(lookup["live"])
        report = self.wait(lookup)
        revision = report["revision"]
        for vout in (2, 999):
            code, error, _ = self.request(route + "/change-outputs", {
                "txid": txid, "vout": vout, "expected_revision": revision})
            self.assertEqual(code, 400, error)
        result = self.success(route + "/change-outputs", {"txid": txid, "vout": 1, "expected_revision": revision})
        self.assertEqual(result["changed"], 1)
        self.assertEqual(before, {str(p.relative_to(archive)): p.read_bytes() for p in archive.rglob("*") if p.is_file()})

    def test_missing_live_transaction_uses_credentialed_worker_and_rejects_custom_options(self):
        txid = test_web.synthetic_txid()
        info = self.success("/api/cases", {"name": "Live change review", "seeds": [txid + ":0"]}, 201)
        route = "/api/cases/" + info["id"] + "/actions"
        with patch.object(self.server, "start_job", return_value={"id": "synthetic"}) as start:
            self.success(route, {"action": "change-output-lookup", "txid": txid}, 202)
            self.assertTrue(start.call_args.kwargs["live"])
            self.assertEqual(start.call_args.kwargs["txids"], [txid])
            self.assertEqual(start.call_args.args[0][0], "change-output-lookup")
            start.reset_mock()
            for extra in ({"fixture": "/tmp/private"}, {"arguments": []}, {"source": "fixture"},
                          {"settings": {}}, {"txid": txid + ":0"}, {"txid": [txid]}, {"txid": "invalid"}):
                self.assertEqual(self.request(route, {"action": "change-output-lookup", "txid": txid, **extra})[0], 400)
            start.assert_not_called()

    def test_reviewed_import_is_offline_atomic_and_uses_current_revision(self):
        txid, case, route = self.prepare()
        endpoint = route + "/change-output-import"
        body = {"text": "Txid,ChangeVout,Notes\n" + txid.upper() + ",1,Reviewed externally\n"}
        before = load_services(case)
        with patch.object(self.server, "start_job") as worker, \
                patch("liquid_tracer.api.http", side_effect=AssertionError("Offline import")):
            preview = self.success(endpoint, body)
            self.assertTrue(preview["valid"])
            self.assertEqual(load_services(case), before)
            result = self.success(endpoint, {**body, "approve_plan": preview["approval_sha256"]})
            self.assertEqual(result["changed"], 1)
            self.assertEqual(load_services(case)["change_outputs"][txid]["vout"], 1)
            changed = {"text": "Txid,ChangeVout\n" + txid + ",0\n", "policy": "replace"}
            stale = self.success(endpoint, changed)
            current = self.success(route + "/change-outputs", {})
            self.success(route + "/change-outputs", {"txid": txid, "vout": None, "expected_revision": current["revision"]})
            self.assertEqual(self.request(endpoint, {**changed, "approve_plan": stale["approval_sha256"]})[0], 400)
            self.assertEqual(load_services(case).get("change_outputs", {}), {})
            worker.assert_not_called()
        self.assertNotIn("latest_run", read_json(case / "case.json"))

    def test_csrf_busy_invalid_fields_and_stale_selection_leave_settings_unchanged(self):
        txid, case, route = self.prepare()
        endpoint = route + "/change-outputs"
        revision = self.success(endpoint, {})["revision"]
        body = {"txid": txid, "vout": 1, "expected_revision": revision}
        self.assertEqual(self.request(endpoint, body, headers={"X-Liquid-CSRF": "wrong"})[0], 403)
        self.server.active_job = "busy"
        try:
            self.assertEqual(self.request(endpoint, body)[0], 409)
        finally:
            self.server.active_job = None
        before = load_services(case)
        for extra in ({"file": "/private"}, {"vout": True}, {"vout": -1}, {"vout": "1"},
                      {"vout": 2**32}, {"notes": []}, {"expected_revision": None}, {"expected_revision": True}):
            self.assertEqual(self.request(endpoint, {**body, **extra})[0], 400)
            self.assertEqual(load_services(case), before)
        self.assertEqual(self.request(endpoint, {"txid": txid, "vout": 1})[0], 400)
        for invalid in ({"query": []}, {"offset": -1}, {"limit": 101}, {"limit": True}):
            self.assertEqual(self.request(endpoint, invalid)[0], 400)
        self.success(endpoint, body)
        self.assertEqual(self.request(endpoint, {**body, "vout": 0})[0], 400)
        self.assertEqual(load_services(case)["change_outputs"][txid]["vout"], 1)

    def test_import_body_limits_and_unknown_fields_preserve_settings(self):
        txid, case, route = self.prepare()
        endpoint = route + "/change-output-import"
        before = load_services(case)
        large = {"text": "Txid,ChangeVout\n" + (txid + ",1\n") * 1100}
        self.assertGreater(len(large["text"]), 64 * 1024)
        self.assertTrue(self.success(endpoint, large)["valid"])
        self.assertEqual(self.request(route + "/change-outputs", large)[0], 413)
        self.assertEqual(self.request(endpoint, {"text": "x" * (512 * 1024 + 1)})[0], 400)
        for extra in ({"file": "/private"}, {"format": []}, {"policy": "overwrite"}, {"approve_plan": "wrong"}):
            self.assertEqual(self.request(endpoint, {**large, **extra})[0], 400)
        invalid = self.success(endpoint, {"text": "Txid,ChangeVout\n" + txid + ",1\n" + txid + ",0\n"})
        self.assertFalse(invalid["valid"])
        self.assertIsNone(invalid["approval_sha256"])
        self.assertEqual(load_services(case), before)

    def test_lookup_result_drops_unknown_fields_and_preserves_exact_values(self):
        txid = test_web.synthetic_txid()
        report = transaction_outputs(txid, read_json(test_web.SYNTHETIC_API)["/tx/" + txid])
        report.update(current_vout=None, current_notes="Investigator note", revision=0,
                      notice="/private/internal/file", directory="/private", credentials="secret")
        report["outputs"][0].update(value=2**64 - 1, internal_path="/private")
        public = self.server.public_result(report, "change-output-lookup", None, [txid])
        self.assertEqual(public["outputs"][0]["value_text"], str(2**64 - 1))
        self.assertNotIn("/private", json.dumps(public))
        self.assertNotIn("secret", json.dumps(public))
        with self.assertRaises(TraceError):
            self.server.public_result({**report, "txid": "0" * 64}, "change-output-lookup", None, [txid])
        with self.assertRaises(TraceError):
            self.server.public_result({**report, "revision": True}, "change-output-lookup", None, [txid])


if __name__ == "__main__":
    unittest.main()
