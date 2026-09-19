"""Saved input downloads through the loopback HTTP boundary."""

import csv
import io
import unittest
import zipfile
from unittest.mock import patch

from liquid_tracer.common import save_json
from liquid_tracer.services import load_services, set_service
from tests import test_web


class InputExportWebTests(unittest.TestCase):
    setUp = test_web.LocalWebTests.setUp
    close_server = test_web.LocalWebTests.close_server
    request = test_web.LocalWebTests.request
    success = test_web.LocalWebTests.success
    create = test_web.LocalWebTests.create

    def prepare(self):
        _, info = self.create()
        case, _ = self.server.case(info["id"])
        return case, "/api/cases/" + info["id"] + "/input-exports/"

    def populate(self, case):
        set_service(case, "SYNTHETIC-export-000", name="Saved name 000", notes='Evidence, "quoted"\nSecond line')
        data = load_services(case)
        template = data["rules"]["SYNTHETIC-export-000"]
        data["rules"] = {
            f"SYNTHETIC-export-{i:03d}": {**template, "address": f"SYNTHETIC-export-{i:03d}",
                                         "name": f"Saved name {i:03d}", "enabled": i != 119}
            for i in range(120)}
        data["name_colors"] = {f"saved name {i:03d}": "#123456" for i in range(120)}
        data["change_outputs"] = {f"{i:064x}": {"vout": i, "notes": f"Output {i}", "updated_at": "2026-09-19"}
                                  for i in range(120)}
        save_json(case / "services.json", data)

    @staticmethod
    def rows(data):
        return list(csv.DictReader(io.StringIO(data.decode("utf-8"))))

    @staticmethod
    def snapshot(case):
        return {str(path.relative_to(case)): path.read_bytes() if path.is_file() else None
                for path in case.rglob("*")}

    def test_each_csv_download_contains_all_pages_and_saved_values(self):
        case, route = self.prepare()
        self.populate(case)
        before = self.snapshot(case)
        with patch.object(self.server, "start_job", side_effect=AssertionError("No worker")), \
                patch("liquid_tracer.api.http", side_effect=AssertionError("No API")):
            for kind in ("attributions", "name-colors", "change-outputs"):
                with self.subTest(kind=kind):
                    code, data, response = self.request(route + kind)
                    self.assertEqual(code, 200)
                    self.assertEqual(response.getheader("Content-Type"), "text/csv; charset=utf-8")
                    self.assertEqual(response.getheader("Content-Disposition"), f'attachment; filename="{kind}.csv"')
                    self.assertEqual(response.getheader("Cache-Control"), "no-store")
                    self.assertEqual(response.getheader("X-Content-Type-Options"), "nosniff")
                    self.assertEqual(response.getheader("Cross-Origin-Resource-Policy"), "same-origin")
                    self.assertEqual(response.getheader("Content-Length"), str(len(data)))
                    self.assertEqual(len(self.rows(data)), 120)
            rows = self.rows(self.success(route + "attributions"))
            self.assertEqual(rows[-1]["Address"], "SYNTHETIC-export-119")
            self.assertEqual(rows[-1]["enabled"], "false")
            self.assertEqual(rows[0]["notes"], 'Evidence, "quoted"\nSecond line')
        self.assertEqual(self.snapshot(case), before)
        self.assertIsNone(self.server.active_job)
        self.assertEqual(self.server.jobs, {})

    def test_all_zip_contains_all_three_input_types_without_run_or_credentials(self):
        case, route = self.prepare()
        self.populate(case)
        before = self.snapshot(case)
        with patch.object(self.server, "start_job", side_effect=AssertionError("No worker")):
            code, data, response = self.request(route + "all", headers={"Origin": self.server.origin})
        self.assertEqual(code, 200)
        self.assertEqual(response.getheader("Content-Type"), "application/zip")
        self.assertEqual(response.getheader("Content-Disposition"), 'attachment; filename="input-csvs.zip"')
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            self.assertEqual(archive.namelist(), ["attributions.csv", "name-colors.csv", "change-outputs.csv"])
            for name in archive.namelist():
                self.assertEqual(len(self.rows(archive.read(name))), 120)
        self.assertEqual(self.snapshot(case), before)
        self.assertFalse((case / "runs").exists())

    def test_empty_case_exports_headers_without_creating_settings_or_files(self):
        case, route = self.prepare()
        before = self.snapshot(case)
        for kind in ("attributions", "name-colors", "change-outputs"):
            data = self.success(route + kind)
            self.assertEqual(self.rows(data), [])
            self.assertEqual(len(data.decode().splitlines()), 1)
        with zipfile.ZipFile(io.BytesIO(self.success(route + "all"))) as archive:
            self.assertEqual(len(archive.namelist()), 3)
            for name in archive.namelist():
                self.assertEqual(self.rows(archive.read(name)), [])
        self.assertEqual(self.snapshot(case), before)
        self.assertFalse((case / "services.json").exists())

    def test_large_single_kind_is_a_zip_with_every_row_once(self):
        case, route = self.prepare()
        self.populate(case)
        with patch("liquid_tracer.address_import.MAX_ROWS", 50):
            code, data, response = self.request(route + "attributions")
        self.assertEqual(code, 200)
        self.assertEqual(response.getheader("Content-Type"), "application/zip")
        self.assertEqual(response.getheader("Content-Disposition"), 'attachment; filename="attributions.zip"')
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            self.assertEqual(archive.namelist(), [f"attributions-part-{i:03d}.csv" for i in (1, 2, 3)])
            rows = [row for name in archive.namelist() for row in self.rows(archive.read(name))]
        self.assertEqual(len(rows), 120)
        self.assertEqual(len({row["Address"] for row in rows}), 120)

    def test_downloads_enforce_same_origin_and_known_routes(self):
        case, route = self.prepare()
        before = self.snapshot(case)
        for headers in ({"Host": "attacker.example"}, {"Origin": "https://attacker.example"},
                        {"Sec-Fetch-Site": "cross-site"}):
            self.assertEqual(self.request(route + "all", headers=headers)[0], 403)
        for suffix in ("unknown", "services.json", "all?query=needle", "all?offset=100", "all#fragment",
                       "all/extra", "../case.json", "%2E%2E%2Fcase.json", "%2Fetc%2Fpasswd"):
            with self.subTest(suffix=suffix):
                self.assertIn(self.request(route + suffix)[0], (400, 404))
        self.assertEqual(self.request("/api/cases/" + "0" * 32 + "/input-exports/all")[0], 404)
        self.assertEqual(self.request(route + "all", {})[0], 404)
        self.assertEqual(self.snapshot(case), before)

    def test_download_is_available_while_a_worker_is_running(self):
        _, route = self.prepare()
        with patch.object(self.server, "ensure_idle", side_effect=AssertionError("Read-only download")), \
                patch.object(self.server, "start_job", side_effect=AssertionError("No worker")):
            self.assertEqual(self.request(route + "all")[0], 200)


if __name__ == "__main__":
    unittest.main()
