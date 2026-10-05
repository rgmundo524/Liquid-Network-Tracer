"""Durable per-investigation preview labels do not touch immutable evidence."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil
import threading
import unittest
from unittest.mock import patch

from liquid_tracer.common import TraceError
from liquid_tracer.preview_numbers import LOCK, REGISTRY, numbered_previews
from liquid_tracer.workflow_api import plot_summary, plot_summary_page, public_plot, selected_plot, workflow_result
from tests.test_workflow_summaries import WorkflowSummaryTests


class PreviewNumberTests(unittest.TestCase):
    setUp = WorkflowSummaryTests.setUp
    report = WorkflowSummaryTests.report
    real_plot = WorkflowSummaryTests.real_plot

    def registry(self):
        return json.loads((self.case / REGISTRY).read_text())

    def test_legacy_bootstrap_is_chronological_across_pages_and_preserves_evidence(self):
        directories = [self.report(number) for number in (3, 1, 2)]
        before = {}
        for directory in directories:
            number = int(directory.name[-8:], 16)
            os.utime(directory / "plot.json", ns=(number, number))
            before[directory.name] = {path.name: path.read_bytes() for path in directory.iterdir()}
        with patch("liquid_tracer.workflow_api.PLOT_SUMMARY_LIMIT", 1):
            values, cursor = [], None
            for _ in directories:
                page = plot_summary_page(self.case, cursor)
                values += page["plots"]
                cursor = page["next_cursor"]
        self.assertEqual([value["preview_number"] for value in values], [3, 2, 1])
        self.assertEqual(self.registry()["next_number"], 4)
        for directory in directories:
            self.assertEqual(before[directory.name], {path.name: path.read_bytes() for path in directory.iterdir()})

    def test_numbers_survive_deletion_timestamp_changes_and_late_imports(self):
        one, two = self.report(1), self.report(2)
        os.utime(one / "plot.json", ns=(1, 1))
        os.utime(two / "plot.json", ns=(2, 2))
        self.assertEqual(plot_summary(self.case, one.name)["preview_number"], 1)
        shutil.rmtree(one)
        os.utime(two / "plot.json", ns=(500, 500))
        imported = self.report(3)
        os.utime(imported / "plot.json", ns=(0, 0))
        self.assertEqual(plot_summary(self.case, imported.name)["preview_number"], 3)
        self.assertEqual(plot_summary(self.case, two.name)["preview_number"], 2)
        self.assertEqual(self.registry()["numbers"][one.name], 1)

    def test_known_previews_need_no_scan_or_write_lock_after_reload(self):
        directory = self.report(1)
        plot_summary(self.case, directory.name)
        with patch("liquid_tracer.preview_numbers._completed", side_effect=AssertionError("no rescan")), \
             patch("liquid_tracer.preview_numbers._lock", side_effect=AssertionError("read-only case")):
            result = plot_summary(self.case, directory.name)
        self.assertEqual(result["preview_number"], 1)
        self.assertNotIn("preview_number_notice", result)

    def test_concurrent_registration_assigns_unique_persistent_numbers(self):
        count = 16
        barrier = threading.Barrier(count)

        def create(number):
            directory = self.report(number)
            barrier.wait(5)
            return plot_summary(self.case, directory.name)

        with ThreadPoolExecutor(max_workers=count) as pool:
            values = list(pool.map(create, range(1, count + 1)))
        self.assertEqual({value["preview_number"] for value in values}, set(range(1, count + 1)))
        registered = self.registry()
        self.assertEqual(registered["next_number"], count + 1)
        for value in values:
            self.assertEqual(registered["numbers"][value["id"]], value["preview_number"])

    def test_incomplete_and_symlinked_previews_do_not_reserve_numbers(self):
        good, unfinished, linked_report = (self.report(number) for number in (1, 2, 3))
        (unfinished / "SHA256SUMS").unlink()
        (linked_report / "plot.json").unlink()
        (linked_report / "plot.json").symlink_to(good / "plot.json")
        linked_directory = self.case / "previews" / ("a" * 16 + "-plots-00000004")
        linked_directory.symlink_to(good, target_is_directory=True)
        plot_summary(self.case, good.name)
        self.assertEqual(self.registry()["numbers"], {good.name: 1})
        (unfinished / "SHA256SUMS").write_text("completed later")
        self.assertEqual(plot_summary(self.case, unfinished.name)["preview_number"], 2)

    def test_summary_does_not_accept_number_in_untrusted_report(self):
        directory = self.report(1, preview_number=777, preview_number_notice="forged")
        item = plot_summary(self.case, directory.name)
        self.assertEqual(item["preview_number"], 1)
        self.assertNotIn("preview_number_notice", item)

    def test_public_numbers_reject_boolean_fractional_and_unsafe_values(self):
        for number in (None, True, 0, -1, 1.5, "1", 2 ** 53):
            with self.subTest(number=number):
                self.assertNotIn("preview_number", public_plot({"preview_number": number}))
        self.assertEqual(public_plot({"preview_number": 1})["preview_number"], 1)
        self.assertNotIn("preview_number_notice", public_plot({"preview_number_notice": True}))

    def test_corrupt_registry_is_not_reset_or_silently_renumbered(self):
        directory = self.report(1)
        plot_summary(self.case, directory.name)
        valid = self.registry()
        invalid = ["{", json.dumps({**valid, "case_id": "wrong"}),
                   json.dumps({**valid, "numbers": {directory.name: True}}),
                   json.dumps({**valid, "next_number": 1}),
                   json.dumps({**valid, "numbers": {directory.name: 1, "a" * 16 + "-plots-00000002": 1}})]
        for raw in invalid:
            with self.subTest(raw=raw):
                (self.case / REGISTRY).write_text(raw)
                item = plot_summary(self.case, directory.name)
                self.assertNotIn("preview_number", item)
                self.assertIn("restore preview-numbers.json", item["preview_number_notice"])
                self.assertEqual((self.case / REGISTRY).read_text(), raw)

    def test_unwritable_registry_retains_known_labels_and_does_not_invent_new_ones(self):
        one = self.report(1)
        plot_summary(self.case, one.name)
        two = self.report(2)
        before = (self.case / REGISTRY).read_bytes()
        with patch("liquid_tracer.preview_numbers.save_json", side_effect=OSError("disk full")):
            page = plot_summary_page(self.case)
        values = {value["id"]: value for value in page["plots"]}
        self.assertEqual(values[one.name]["preview_number"], 1)
        self.assertNotIn("preview_number", values[two.name])
        self.assertIn("could not be saved", values[two.name]["preview_number_notice"])
        self.assertEqual((self.case / REGISTRY).read_bytes(), before)
        self.assertEqual(plot_summary(self.case, two.name)["preview_number"], 2)

    def test_read_only_legacy_case_shows_notice_without_provisional_numbers(self):
        directory = self.report(1)
        with patch("liquid_tracer.preview_numbers._lock", side_effect=PermissionError("read only")):
            item = plot_summary(self.case, directory.name)
        self.assertNotIn("preview_number", item)
        self.assertIn("write access", item["preview_number_notice"])
        self.assertFalse((self.case / REGISTRY).exists())

    def test_registry_size_limit_preserves_existing_readable_numbers(self):
        one = self.report(1)
        self.assertEqual(plot_summary(self.case, one.name)["preview_number"], 1)
        before = (self.case / REGISTRY).read_bytes()
        two = self.report(2)
        with patch("liquid_tracer.preview_numbers.MAX_BYTES", len(before)):
            page = plot_summary_page(self.case)
            values = {value["id"]: value for value in page["plots"]}
            self.assertEqual(values[one.name]["preview_number"], 1)
            self.assertNotIn("preview_number", values[two.name])
            self.assertIn("size limit", values[two.name]["preview_number_notice"])
            self.assertEqual((self.case / REGISTRY).read_bytes(), before)
            # Later reads of known previews still work without any repair.
            known = plot_summary(self.case, one.name)
            self.assertEqual(known["preview_number"], 1)
            self.assertNotIn("preview_number_notice", known)
        self.assertEqual(plot_summary(self.case, two.name)["preview_number"], 2)

    def test_registry_size_limit_does_not_write_an_unreadable_bootstrap(self):
        directory = self.report(1)
        with patch("liquid_tracer.preview_numbers.MAX_BYTES", 1):
            item = plot_summary(self.case, directory.name)
        self.assertNotIn("preview_number", item)
        self.assertIn("size limit", item["preview_number_notice"])
        self.assertFalse((self.case / REGISTRY).exists())
        self.assertEqual(plot_summary(self.case, directory.name)["preview_number"], 1)

    def test_registry_and_lock_symlinks_are_not_followed(self):
        directory = self.report(1)
        target = self.root / "outside"
        target.write_text("unchanged")
        for name in (REGISTRY, LOCK, REGISTRY + ".tmp"):
            with self.subTest(name=name):
                path = self.case / name
                path.unlink(missing_ok=True)
                path.symlink_to(target)
                result = plot_summary(self.case, directory.name)
                self.assertNotIn("preview_number", result)
                self.assertIn("preview_number_notice", result)
                self.assertEqual(target.read_text(), "unchanged")
                path.unlink()

    def test_nonregular_sidecars_do_not_block_numbering(self):
        directory = self.report(1)
        for name in (REGISTRY, LOCK, REGISTRY + ".tmp"):
            with self.subTest(name=name):
                path = self.case / name
                path.unlink(missing_ok=True)
                os.mkfifo(path)
                result = plot_summary(self.case, directory.name)
                self.assertNotIn("preview_number", result)
                self.assertIn("preview_number_notice", result)
                path.unlink()

    def test_creation_selected_and_completion_keep_the_same_number_without_snapshot_changes(self):
        first, second = self.real_plot(), self.real_plot()
        self.assertEqual((first["preview_number"], second["preview_number"]), (1, 2))
        for result in (first, second):
            directory = Path(result["directory"])
            before = {path.name: path.read_bytes() for path in directory.iterdir()}
            selected = selected_plot(self.case, result["preview_id"])
            completed = workflow_result(self.case, result, "plot")
            self.assertEqual(selected["preview_number"], result["preview_number"])
            self.assertEqual(completed["preview_number"], result["preview_number"])
            self.assertEqual(before, {path.name: path.read_bytes() for path in directory.iterdir()})

    def test_numbering_write_failure_does_not_destroy_completed_preview(self):
        with patch("liquid_tracer.preview_numbers.save_json", side_effect=OSError("disk full")):
            result = self.real_plot()
        self.assertIn("preview_number_notice", result)
        self.assertTrue((Path(result["directory"]) / "SHA256SUMS").is_file())
        self.assertEqual(selected_plot(self.case, result["preview_id"])["preview_number"], 1)


if __name__ == "__main__":
    unittest.main()
