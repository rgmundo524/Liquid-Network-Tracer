"""CSV limit changes release older branches through saved CLI continuations."""

import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from liquid_tracer.cli import main, verify_export
from liquid_tracer.common import read_json, save_json
from liquid_tracer.investigations import create_investigation
from liquid_tracer.services import load_services
from tests.fixtures import CONFIRMED, output


def branching_chain():
    """Two selected outputs: one limited at global hop 2, one always open."""
    def txid(name):
        return hashlib.sha256(("SYNTHETIC-csv-hop-" + name).encode()).hexdigest()

    seed = txid("seed")
    ids = {branch: [seed] + [txid(branch + str(depth)) for depth in range(1, 11)]
           for branch in ("service", "open")}
    addresses = {branch: ["SYNTHETIC-csv-hop-" + branch + str(depth)
                          for depth in range(11)] for branch in ids}
    data = {"/tx/" + seed: {"txid": seed, "status": dict(CONFIRMED), "vin": [],
                           "vout": [output(addresses[branch][0]) for branch in ids]},
            "/tx/" + seed + "/outspends": []}
    for index, branch in enumerate(ids):
        for depth in range(1, 11):
            previous, current = ids[branch][depth - 1:depth + 1]
            data["/tx/" + current] = {
                "txid": current, "status": dict(CONFIRMED),
                "vin": [{"txid": previous, "vout": index if depth == 1 else 0,
                         "prevout": output(addresses[branch][depth - 1])}],
                "vout": [output(addresses[branch][depth])]}
            spend = {"spent": True, "txid": current, "vin": 0,
                     "status": dict(CONFIRMED)}
            if depth == 1:
                data["/tx/" + seed + "/outspends"].append(spend)
            else:
                data["/tx/" + previous + "/outspends"] = [spend]
        data["/tx/" + ids[branch][-1] + "/outspends"] = [{"spent": False}]
        for address in addresses[branch]:
            data["/address/" + address] = {
                "address": address, "chain_stats": {"tx_count": 2},
                "mempool_stats": {"tx_count": 0}}
    return ids, addresses, data


class CsvHopContinuationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ids, self.addresses, data = branching_chain()
        self.fixture = self.root / "api.json"
        save_json(self.fixture, data)
        self.csv = self.root / "attributions.csv"

    def invoke(self, arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = main(arguments)
        self.assertEqual(status, 0, stderr.getvalue())
        return json.loads(stdout.getvalue())

    def prepare_case(self, workers):
        self.workers = workers
        seed = self.ids["service"][0]
        self.seeds = [seed + ":0", seed + ":1"]
        self.case = create_investigation(self.root / "cases", "CSV hops " + str(workers),
                                         fixture=self.fixture, seeds=self.seeds)
        self.archives = {}

    def import_limit(self, limit, *, replace=False):
        self.csv.write_text("Address,Name,stop_tracing,hop_limit,Duplicate count\n" +
                            self.addresses["service"][2] + ",Example service,false," +
                            str(limit) + ",1\n", encoding="utf-8")
        args = ["input-import", "--case", str(self.case), "--file", str(self.csv)]
        if replace:
            args.extend(["--on-conflict", "replace"])
        review = self.invoke(args)
        self.assertTrue(review["valid"])
        self.assertEqual(review["files"][0]["kind"], "attributions")
        result = self.invoke([*args, "--approve-plan", review["approval_sha256"]])
        return review, result

    def trace(self, *extra):
        report = self.invoke(["trace", "--case", str(self.case), "--fixture", str(self.fixture),
                              "--api-workers", str(self.workers), *extra])
        directory = self.case / "runs" / report["run_id"]
        state = read_json(directory / "trace.json")
        self.assertEqual(state["status"], "bounded_complete")
        self.assertEqual(state["errors"], [])
        self.archives[directory] = (directory / "trace.json").read_bytes()
        return state

    def continue_trace(self, additional):
        return self.trace("--resume", "latest", "--additional-hops", str(additional))

    def assert_branches(self, state, service_depth, open_depth):
        expected = {self.ids["service"][0]}
        for branch, depth in (("service", service_depth), ("open", open_depth)):
            expected.update(self.ids[branch][1:depth + 1])
        self.assertEqual(set(state["transactions"]), expected)
        self.assertEqual(state["limits"]["max_hops"], open_depth)
        self.assertEqual(state["outputs"][self.ids["service"][service_depth] + ":0"]["status"],
                         "attribution_hop_limit")
        self.assertEqual(state["outputs"][self.ids["open"][open_depth] + ":0"]["status"],
                         "hop_limit")

    def assert_archives_unchanged(self):
        for directory, before in self.archives.items():
            self.assertEqual((directory / "trace.json").read_bytes(), before)
            verify_export(directory)

    def reach_older_boundary(self, limit, global_depth):
        self.import_limit(limit)
        first = self.trace("--seed", self.seeds[0], "--seed", self.seeds[1], "--hops", "2")
        self.assertEqual(first["limits"]["max_hops"], 2)
        held = self.continue_trace(global_depth - 2)
        self.assert_branches(held, 2 + limit, global_depth)
        # Save another run with the same exhausted branch before changing CSV.
        unchanged = self.continue_trace(0)
        self.assert_branches(unchanged, 2 + limit, global_depth)
        self.assertEqual(unchanged["stats"]["new_transactions_this_run"], 0)
        return unchanged

    def test_csv_one_to_two_backfills_hop_four_while_open_branch_reaches_five(self):
        for workers in (1, 8):
            with self.subTest(workers=workers):
                self.prepare_case(workers)
                held = self.reach_older_boundary(1, 4)
                review, saved = self.import_limit(2, replace=True)
                self.assertEqual(review["counts"]["replace"], 1)
                self.assertEqual(saved["changed"], 1)
                state = self.continue_trace(1)
                self.assert_branches(state, 4, 5)
                self.assertEqual(set(state["transactions"]) - set(held["transactions"]),
                                 {self.ids["service"][4], self.ids["open"][5]})
                self.assertEqual(state["service_controls"]["rules"][self.addresses["service"][2]]["hop_limit"], 2)
                self.assert_archives_unchanged()

    def test_csv_two_to_four_fetches_each_missing_intermediate_transaction(self):
        for workers in (1, 8):
            with self.subTest(workers=workers):
                self.prepare_case(workers)
                held = self.reach_older_boundary(2, 6)
                self.import_limit(4, replace=True)
                state = self.continue_trace(1)
                self.assert_branches(state, 6, 7)
                self.assertEqual(set(state["transactions"]) - set(held["transactions"]),
                                 {self.ids["service"][5], self.ids["service"][6], self.ids["open"][7]})
                for depth in (4, 5):
                    self.assertEqual(state["links"][self.ids["service"][depth] + ":0"]["spending_txid"],
                                     self.ids["service"][depth + 1])
                history = load_services(self.case)["history"]
                self.assertEqual(history[-1]["previous"]["hop_limit"], 2)
                self.assertEqual(history[-1]["rule"]["hop_limit"], 4)
                self.assert_archives_unchanged()

    def test_default_keep_does_not_change_limit_or_release_the_older_branch(self):
        self.prepare_case(8)
        self.reach_older_boundary(2, 6)
        before = (self.case / "services.json").read_bytes()
        review, saved = self.import_limit(4)
        self.assertEqual(review["counts"]["keep"], 1)
        self.assertEqual(saved["changed"], 0)
        self.assertEqual((self.case / "services.json").read_bytes(), before)
        state = self.continue_trace(1)
        self.assert_branches(state, 4, 7)
        self.assertEqual(state["service_controls"]["rules"][self.addresses["service"][2]]["hop_limit"], 2)
        self.assert_archives_unchanged()

    def test_raised_csv_limit_backfills_without_increasing_existing_global_ceiling(self):
        self.prepare_case(8)
        self.reach_older_boundary(1, 4)
        self.import_limit(2, replace=True)
        state = self.continue_trace(0)
        self.assert_branches(state, 4, 4)
        self.assertEqual(state["stats"]["new_transactions_this_run"], 1)
        self.assert_archives_unchanged()
