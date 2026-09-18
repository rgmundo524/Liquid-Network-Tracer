"""Reviewed color imports through the real local HTTP API and CLI."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from liquid_tracer.cli import main
from liquid_tracer.common import read_json
from liquid_tracer.investigations import create_investigation
from liquid_tracer.services import load_services, set_service
from tests import test_web


class NameColorImportWebTests(unittest.TestCase):
    setUp = test_web.LocalWebTests.setUp
    close_server = test_web.LocalWebTests.close_server
    request = test_web.LocalWebTests.request
    success = test_web.LocalWebTests.success
    create = test_web.LocalWebTests.create

    def prepare(self):
        _, case = self.create()
        path, _ = self.server.case(case["id"])
        route = "/api/cases/" + case["id"]
        payload = {"text": "Address,Name\nSYNTHETIC-one,Perp\nSYNTHETIC-two,PERP\n"}
        preview = self.success(route + "/address-import", payload)
        self.success(route + "/address-import", {**payload, "approve_plan": preview["approval_sha256"]})
        return path, route

    def test_preview_apply_and_catalog_refresh_without_trace_or_board_requests(self):
        case, route = self.prepare()
        before = (case / "services.json").read_bytes()
        rules = load_services(case)["rules"]
        payload = {"text": "Name,Color\npErP,#93C5FD\n"}
        with patch.object(self.server, "start_job") as worker, \
                patch("liquid_tracer.api.http", side_effect=AssertionError("Offline import")):
            preview = self.success(route + "/name-color-import", payload)
            self.assertTrue(preview["valid"])
            self.assertEqual(preview["changes"][0]["addresses"], 2)
            self.assertEqual((case / "services.json").read_bytes(), before)
            result = self.success(route + "/name-color-import", {**payload, "approve_plan": preview["approval_sha256"]})
            self.assertEqual(result["changed"], 1)
            self.assertEqual(load_services(case)["name_colors"], {"perp": "#93c5fd"})
            self.assertEqual(load_services(case)["rules"], rules)
            self.assertNotIn("latest_run", read_json(case / "case.json"))
            catalog = self.success(route + "/name-colors", {})
            self.assertEqual(catalog["rows"][0]["color"], "#93c5fd")
            worker.assert_not_called()

    def test_conflict_keep_replace_and_clear_require_the_matching_review(self):
        case, route = self.prepare()
        endpoint = route + "/name-color-import"
        def apply(text, policy="keep"):
            body = {"text": text, "policy": policy}
            review = self.success(endpoint, body)
            return self.success(endpoint, {**body, "approve_plan": review["approval_sha256"]})
        apply("Name,Color\nPerp,#123456\n")
        self.assertEqual(apply("Name,Color\nperp,#abcdef\n")["changed"], 0)
        self.assertEqual(load_services(case)["name_colors"]["perp"], "#123456")
        self.assertEqual(apply("Name,Color\nPERP,#abcdef\n", "replace")["changed"], 1)
        self.assertEqual(apply('[{"name":"Perp","color":null}]', "replace")["changed"], 1)
        self.assertEqual(load_services(case)["name_colors"], {})

    def test_csrf_busy_stale_and_invalid_requests_preserve_settings(self):
        case, route = self.prepare()
        endpoint = route + "/name-color-import"
        payload = {"text": "Name,Color\nPerp,#123456\n"}
        self.assertEqual(self.request(endpoint, payload, headers={"X-Liquid-CSRF": "wrong"})[0], 403)
        self.server.active_job = "busy"
        try:
            self.assertEqual(self.request(endpoint, payload)[0], 409)
        finally:
            self.server.active_job = None
        before = (case / "services.json").read_bytes()
        for extra in ({"file": "/private"}, {"role": "seed"}, {"format": ["csv"]},
                      {"policy": "overwrite"}, {"approve_plan": "wrong"}):
            self.assertEqual(self.request(endpoint, {**payload, **extra})[0], 400)
            self.assertEqual((case / "services.json").read_bytes(), before)
        invalid = self.success(endpoint, {"text": payload["text"] + "Unknown,#ffffff\n"})
        self.assertFalse(invalid["valid"])
        self.assertIsNone(invalid["approval_sha256"])
        self.assertEqual((case / "services.json").read_bytes(), before)
        review = self.success(endpoint, payload)
        set_service(case, "SYNTHETIC-three", name="Other")
        self.assertEqual(self.request(endpoint, {**payload, "approve_plan": review["approval_sha256"]})[0], 400)
        self.assertFalse(load_services(case).get("name_colors"))

    def test_larger_import_body_does_not_expand_unrelated_route_limits(self):
        _, route = self.prepare()
        # A valid duplicate-heavy input above the ordinary 64 KiB route limit.
        body = {"text": "Name,Color\n" + ("PERP" + " " * 10 + ",#123456\n") * 4000}
        self.assertGreater(len(body["text"]), 64 * 1024)
        self.assertTrue(self.success(route + "/name-color-import", body)["valid"])
        self.assertEqual(self.request(route + "/name-colors", body)[0], 413)
        self.assertEqual(self.request(route + "/name-color-import", {"text": "x" * (512 * 1024 + 1)})[0], 400)


class NameColorImportCliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.case = create_investigation(self.root, "Color CSV")
        set_service(self.case, "SYNTHETIC-one", name="Perp")
        self.file = self.root / "colors.csv"
        self.file.write_text("Name,Color\nPERP,#123456\n")

    def command(self, *extra):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors), \
                patch("liquid_tracer.api.http", side_effect=AssertionError("Offline import")):
            status = main(["name-color-import", "--case", str(self.case), "--file", str(self.file), *extra])
        return status, json.loads(output.getvalue()) if output.getvalue() else None, errors.getvalue()

    def test_default_preview_apply_and_changed_file_rejection(self):
        before = (self.case / "services.json").read_bytes()
        status, preview, _ = self.command()
        self.assertEqual(status, 0)
        self.assertTrue(preview["valid"])
        self.assertEqual((self.case / "services.json").read_bytes(), before)
        self.file.write_text("Name,Color\nPERP,#abcdef\n")
        self.assertNotEqual(self.command("--approve-plan", preview["approval_sha256"])[0], 0)
        self.assertEqual((self.case / "services.json").read_bytes(), before)
        _, preview, _ = self.command("--dry-run")
        status, result, _ = self.command("--approve-plan", preview["approval_sha256"])
        self.assertEqual(status, 0)
        self.assertEqual(result["changed"], 1)
        self.assertEqual(load_services(self.case)["name_colors"], {"perp": "#abcdef"})

    def test_invalid_rows_have_nonzero_exit_and_no_writes(self):
        before = (self.case / "services.json").read_bytes()
        self.file.write_text("Name,Color\nPerp,#123456\nUnknown,#abcdef\n")
        status, preview, _ = self.command()
        self.assertEqual(status, 1)
        self.assertFalse(preview["valid"])
        self.assertEqual((self.case / "services.json").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
