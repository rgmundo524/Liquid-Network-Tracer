"""Change preferences are local, revisioned, and separate from immutable evidence."""

import fcntl
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import patch

from liquid_tracer import change_outputs
from liquid_tracer.cli import main
from liquid_tracer.common import TraceError, canonical, digest, read_json, save_json
from liquid_tracer.investigations import create_investigation, read_case
from liquid_tracer.services import load_services, set_service
from tests.fixtures import A, B, C, D, X, fixture


def save_archive(case, transactions=None, source="https://blockstream.info/liquid/api", run_id="0123456789abcdef"):
    """An isolated, checksum-verified synthetic snapshot for local read tests."""
    directory = case / "runs" / run_id
    directory.mkdir(parents=True, exist_ok=True)
    state = {"case_id": read_case(case)["case_id"], "run_id": run_id, "source": source,
             "transactions": {txid: {"data": data} for txid, data in (transactions or {
                 key[4:]: value for key, value in fixture().items() if key.count("/") == 2}).items()}}
    save_json(directory / "trace.json", state)
    save_json(directory / "graph.json", {})
    save_json(directory / "miro-plan.json", {})
    (directory / "SHA256SUMS").write_text("".join(digest((directory / name).read_bytes()) + "  " + name + "\n"
        for name in ("trace.json", "graph.json", "miro-plan.json")))
    save_json(case / "case.json", {**read_case(case), "latest_run": run_id})
    return directory


class ChangeOutputTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.case = create_investigation(self.root, "Change choices", seeds=[A + ":0"])

    def test_set_edit_clear_and_noop_preserve_other_settings_and_evidence(self):
        set_service(self.case, "SYNTHETIC-customer", name="Customer", notes="Keep evidence")
        before = load_services(self.case)
        archive = save_archive(self.case)
        evidence = (archive / "trace.json").read_bytes()
        result = change_outputs.set_change_output(self.case, A.upper(), 0, "Investigator review", before["revision"])
        self.assertEqual((result["changed"], result["revision"]), (1, before["revision"] + 1))
        stored = (self.case / "services.json").read_bytes()
        self.assertEqual(change_outputs.set_change_output(self.case, A, 0, "Investigator review")["changed"], 0)
        self.assertEqual((self.case / "services.json").read_bytes(), stored)
        after = load_services(self.case)
        self.assertEqual(after["rules"], before["rules"])
        self.assertEqual(after["history"][:-1], before["history"])
        self.assertEqual(after["history"][-1]["type"], "change_outputs")
        self.assertEqual(change_outputs.set_change_output(self.case, A, 1, "Updated rationale")["changed"], 1)
        self.assertEqual(change_outputs.catalog(self.case, A[:8])["rows"][0]["vout"], 1)
        self.assertEqual(change_outputs.set_change_output(self.case, A, None)["changed"], 1)
        self.assertEqual(change_outputs.set_change_output(self.case, A, None)["changed"], 0)
        self.assertEqual(change_outputs.catalog(self.case)["total"], 0)
        self.assertEqual((archive / "trace.json").read_bytes(), evidence)

    def test_unknown_hash_can_be_saved_without_lookup(self):
        with patch("liquid_tracer.inspection.inspect_transaction", side_effect=AssertionError("No lookup")):
            self.assertEqual(change_outputs.set_change_output(self.case, X, 4294967295)["changed"], 1)
        self.assertEqual(change_outputs.catalog(self.case)["rows"][0]["txid"], X)

    def test_input_and_saved_mapping_validation(self):
        for vout in (True, False, -1, 2**32, 1.5, "1"):
            with self.subTest(vout=vout), self.assertRaises(TraceError):
                change_outputs.set_change_output(self.case, A, vout)
        for txid in (None, "z" * 64, A + ":0", A[:-1]):
            with self.subTest(txid=txid), self.assertRaises(TraceError):
                change_outputs.set_change_output(self.case, txid, 0)
        for notes in (None, "x" * 4001, "invalid\x00notes"):
            with self.subTest(notes=notes), self.assertRaises(TraceError):
                change_outputs.set_change_output(self.case, A, 0, notes)
        for mapping in ([], {A.upper(): {"vout": 0, "notes": "", "updated_at": "now"}},
                        {A: {"vout": True, "notes": "", "updated_at": "now"}},
                        {A: {"vout": None, "notes": "", "updated_at": "now"}},
                        {A: {"vout": 0, "notes": "", "updated_at": ""}}):
            settings = {**load_services(self.case), "change_outputs": mapping}
            save_json(self.case / "services.json", settings)
            with self.assertRaises(TraceError):
                load_services(self.case)
            (self.case / "services.json").unlink()
        for options in ({"limit": 101}, {"offset": True}, {"query": "a" * 257}):
            with self.assertRaises(TraceError):
                change_outputs.catalog(self.case, **options)

    def test_known_fee_pegout_unspendable_and_missing_indexes_rejected(self):
        save_archive(self.case)
        for txid, vout in ((A, 2), (C, 1), (D, 0), (A, 3)):
            with self.subTest(txid=txid, vout=vout), self.assertRaises(TraceError):
                change_outputs.set_change_output(self.case, txid, vout)
        self.assertFalse((self.case / "services.json").exists())
        change_outputs.set_change_output(self.case, A, 1)
        change_outputs.set_change_output(self.case, B, 0)

    def test_stale_editor_and_locks(self):
        change_outputs.set_change_output(self.case, A, 0, expected_revision=0)
        with self.assertRaisesRegex(TraceError, "changed"):
            change_outputs.set_change_output(self.case, A, 1, expected_revision=0)
        with (self.case / "trace.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
            for action in (lambda: change_outputs.set_change_output(self.case, B, 0),
                           lambda: change_outputs.transaction_lookup(self.case, A)):
                with self.assertRaisesRegex(TraceError, "active"):
                    action()

        def guarded_save(path, data):
            for filename in ("trace.lock", "case.lock"):
                with (self.case / filename).open("a") as lock:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            save_json(path, data)
        with patch("liquid_tracer.change_outputs.save_json", side_effect=guarded_save) as save:
            change_outputs.set_change_output(self.case, A, 1)
            self.assertEqual(save.call_count, 1)

    def test_saved_transaction_lookup_is_offline_and_does_not_rewrite_evidence(self):
        archive = save_archive(self.case)
        before = {p.name: p.read_bytes() for p in archive.iterdir()}
        case_bytes = (self.case / "case.json").read_bytes()
        change_outputs.set_change_output(self.case, A, 1, "Reviewed")
        with patch("liquid_tracer.inspection.inspect_transaction", side_effect=AssertionError("No network")):
            self.assertFalse(change_outputs.lookup_requires_network(self.case, A))
            report = change_outputs.transaction_lookup(self.case, A.upper())
        self.assertEqual((report["txid"], report["current_vout"], report["current_notes"]), (A, 1, "Reviewed"))
        self.assertEqual([row["selectable"] for row in report["outputs"]], [True, True, False])
        self.assertEqual(before, {p.name: p.read_bytes() for p in archive.iterdir()})
        self.assertEqual((self.case / "case.json").read_bytes(), case_bytes)
        (archive / "trace.json").write_text("{}")
        with self.assertRaisesRegex(TraceError, "checksum"):
            change_outputs.transaction_lookup(self.case, A)

    def test_missing_transaction_uses_original_source_and_bounded_lookup(self):
        save_archive(self.case)
        self.assertTrue(change_outputs.lookup_requires_network(self.case, X))
        with patch("liquid_tracer.inspection.inspect_transaction", return_value={"txid": X, "outputs": []}) as lookup:
            report = change_outputs.transaction_lookup(self.case, X)
        self.assertEqual(report["txid"], X)
        lookup.assert_called_once_with(X, fixture=None, base_url="https://blockstream.info/liquid/api", auth="none",
                                       max_requests=5, max_seconds=30)
        self.assertFalse((self.case / "services.json").exists())

    def test_fixture_lookup_and_missing_or_changed_fixture_protection(self):
        source = self.root / "fixture.json"
        save_json(source, fixture())
        case = create_investigation(self.root, "Fixture lookup", fixture=source)
        self.assertFalse(change_outputs.lookup_requires_network(case, A))
        with patch("liquid_tracer.api.http", side_effect=AssertionError("Fixture must be offline")):
            self.assertEqual(len(change_outputs.transaction_lookup(case, A)["outputs"]), 3)
        save_archive(case, source="fixture://" + digest(canonical(fixture())))
        save_json(source, {})
        with self.assertRaisesRegex(TraceError, "source"):
            change_outputs.lookup_requires_network(case, X)
        source.unlink()
        with self.assertRaisesRegex(TraceError, "unavailable"):
            change_outputs.transaction_lookup(case, X)
        metadata = read_case(case)
        metadata["fixture"] = None
        save_json(case / "case.json", metadata)
        with self.assertRaisesRegex(TraceError, "original synthetic fixture"):
            change_outputs.lookup_requires_network(case, X)

    def test_cli_set_list_lookup_file_and_clear(self):
        save_archive(self.case)
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            self.assertEqual(main(["change-output-set", "--case", str(self.case), "--txid", A, "--vout", "1"]), 0)
        self.assertEqual(json.loads(stdout.getvalue())["vout"], 1)
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            self.assertEqual(main(["change-output-list", "--case", str(self.case)]), 0)
        self.assertEqual(json.loads(stdout.getvalue())["total"], 1)
        output = self.root / "lookup.json"
        arguments = ["change-output-lookup", "--case", str(self.case), "--txid", A, "--output", str(output)]
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(arguments), 0)
        self.assertEqual(read_json(output)["current_vout"], 1)
        with patch("liquid_tracer.change_outputs.transaction_lookup", side_effect=AssertionError("No lookup")), redirect_stderr(io.StringIO()):
            self.assertEqual(main(arguments), 1)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(["change-output-set", "--case", str(self.case), "--txid", A, "--clear"]), 0)
        self.assertEqual(change_outputs.catalog(self.case)["total"], 0)


if __name__ == "__main__":
    unittest.main()
