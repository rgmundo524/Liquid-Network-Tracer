"""Unified CSV uploads through the real local HTTP API and command line."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from liquid_tracer.cli import main
from liquid_tracer.investigations import create_investigation
from liquid_tracer.services import load_services, set_service
from tests import test_web


def batch():
    return [
        {"name": "colors.csv", "text": "Name,Color\nExample service,#123456\n"},
        {"name": "attributions.csv", "text": "Address,Name,stop_tracing,hop_limit\nSYNTHETIC-import,Example service,false,1\n"},
        {"name": "change.csv", "text": "Txid,ChangeVout,Notes\n" + "a" * 64 + ",0,Reviewed\n"},
    ]


class InputImportWebTests(unittest.TestCase):
    setUp = test_web.LocalWebTests.setUp
    close_server = test_web.LocalWebTests.close_server
    request = test_web.LocalWebTests.request
    success = test_web.LocalWebTests.success
    create = test_web.LocalWebTests.create

    def prepare(self):
        _, case = self.create()
        path, _ = self.server.case(case["id"])
        return path, "/api/cases/" + case["id"]

    def test_one_preview_and_apply_saves_all_three_without_starting_a_job(self):
        case, route = self.prepare()
        body = {"files": batch()}
        with patch.object(self.server, "start_job") as worker, \
                patch("liquid_tracer.api.http", side_effect=AssertionError("Offline import")):
            review = self.success(route + "/input-import", body)
            self.assertTrue(review["valid"])
            self.assertFalse((case / "services.json").exists())
            self.assertEqual([file["name"] for file in review["files"]], [file["name"] for file in body["files"]])
            saved = self.success(route + "/input-import", {**body, "approve_plan": review["approval_sha256"]})
            self.assertEqual(saved["changed"], 3)
            settings = load_services(case)
            self.assertEqual(settings["rules"]["SYNTHETIC-import"]["hop_limit"], 1)
            self.assertEqual(settings["name_colors"], {"example service": "#123456"})
            self.assertEqual(settings["change_outputs"]["a" * 64]["vout"], 0)
            worker.assert_not_called()

    def test_security_busy_and_stale_review_preserve_settings(self):
        case, route = self.prepare()
        endpoint = route + "/input-import"
        body = {"files": batch()}
        self.assertEqual(self.request(endpoint, body, headers={"X-Liquid-CSRF": "wrong"})[0], 403)
        self.server.active_job = "busy"
        try:
            self.assertEqual(self.request(endpoint, body)[0], 409)
        finally:
            self.server.active_job = None
        for extra in ({"file": "/private/key"}, {"arguments": ["--shell"]}, {"approve_plan": "bad"}):
            self.assertEqual(self.request(endpoint, {**body, **extra})[0], 400)
        review = self.success(endpoint, body)
        set_service(case, "SYNTHETIC-other", name="Other")
        before = (case / "services.json").read_bytes()
        self.assertEqual(self.request(endpoint, {**body, "approve_plan": review["approval_sha256"]})[0], 400)
        self.assertEqual((case / "services.json").read_bytes(), before)

    def test_invalid_rows_block_every_file_and_import_has_its_own_body_limit(self):
        case, route = self.prepare()
        body = {"files": batch()}
        body["files"][0]["text"] += "Unknown,#abcdef\n"
        review = self.success(route + "/input-import", body)
        self.assertFalse(review["valid"])
        self.assertIsNone(review["approval_sha256"])
        self.assertFalse((case / "services.json").exists())
        body = {"files": [{"name": "attributions.csv", "text": "Address,Name\n" +
                            "SYNTHETIC-import,Example service\n" * 4000}]}
        self.assertGreater(len(body["files"][0]["text"]), 64 * 1024)
        self.assertTrue(self.success(route + "/input-import", body)["valid"])
        self.assertEqual(self.request(route + "/services", body)[0], 413)


class InputImportCliTests(unittest.TestCase):
    def test_multiple_files_preview_apply_and_stale_file_rejection(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            case = create_investigation(root / "cases", "Combined CSV")
            args = ["input-import", "--case", str(case)]
            paths = []
            for file in batch():
                path = root / file["name"]
                path.write_text(file["text"], encoding="utf-8")
                paths.append(path)
                args.extend(["--file", str(path)])

            def command(*extra):
                output = io.StringIO()
                with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
                    status = main([*args, *extra])
                return status, json.loads(output.getvalue()) if output.getvalue() else None

            status, review = command("--dry-run")
            self.assertEqual(status, 0)
            self.assertTrue(review["valid"])
            self.assertFalse((case / "services.json").exists())
            paths[0].write_text("Name,Color\nExample service,#abcdef\n", encoding="utf-8")
            self.assertNotEqual(command("--approve-plan", review["approval_sha256"])[0], 0)
            self.assertFalse((case / "services.json").exists())
            _, review = command()
            status, result = command("--approve-plan", review["approval_sha256"])
            self.assertEqual(status, 0)
            self.assertEqual(result["changed"], 3)
            self.assertEqual(load_services(case)["name_colors"], {"example service": "#abcdef"})
