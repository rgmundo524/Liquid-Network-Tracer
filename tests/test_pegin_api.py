"""Peg-in HTTP admission and CLI propagation of bounded search options."""
import contextlib
import io
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from liquid_tracer.cli import main, parser
from liquid_tracer.common import TraceError
from liquid_tracer.pegin_api import start_match, start_workspace_match, start_workspace_outputs
from liquid_tracer.pegin_matching import query
from tests.test_pegin_matching import BTC, CLAIM


class PeginApiTests(unittest.TestCase):
    def setUp(self):
        self.server = SimpleNamespace(root=Path("/unused-workspace"), start_job=Mock(return_value={"id": "queued"}))
        self.body = {"action": "pegin-match", "txid": BTC, "max_pages": 10}

    def start(self, body, *, legacy=False):
        if legacy:
            return start_match(self.server, Path("/unused-case"), {"blockchain": "bitcoin"}, body)
        return start_workspace_match(self.server, body)

    def test_default_window_and_public_mode_are_explicit_worker_arguments(self):
        for legacy in (False, True):
            with self.subTest(legacy=legacy), \
                    patch("liquid_tracer.pegin_matching.match_pegin", side_effect=AssertionError("worker only")):
                self.assertEqual(self.start(self.body, legacy=legacy), {"id": "queued"})
                (arguments,), options = self.server.start_job.call_args
                args = parser().parse_args(arguments)
                self.assertEqual((args.start_minutes, args.end_minutes), (-5, 4320))
                self.assertIsNone(args.start_hours)
                self.assertIsNone(args.end_hours)
                self.assertIn("--start-minutes", arguments)
                self.assertIn("--end-minutes", arguments)
                self.assertIn("--public", arguments)
                self.assertTrue(args.public)
                self.assertTrue(options["live"])
                self.assertEqual(options["action"], "pegin-match")
                self.assertEqual(args.case, Path("/unused-case") if legacy else None)

    def test_signed_fractional_window_and_enterprise_choice_reach_worker(self):
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                self.start({**self.body, "vout": 1, "liquid_txid": CLAIM,
                            "start_hours": -3.5, "end_hours": 6.25, "public": False}, legacy=legacy)
                (arguments,), _ = self.server.start_job.call_args
                args = parser().parse_args(arguments)
                self.assertEqual((args.start_hours, args.end_hours), (-3.5, 6.25))
                self.assertEqual((args.vout, args.liquid_txid), (1, CLAIM))
                self.assertFalse(args.public)
                self.assertNotIn("--public", arguments)

    def test_partial_window_uses_default_for_missing_endpoint(self):
        for changes, expected in (({"start_hours": -1}, (-1, 72)), ({"end_hours": 24}, (0, 24))):
            self.start({**self.body, **changes})
            args = parser().parse_args(self.server.start_job.call_args.args[0])
            self.assertEqual((args.start_hours, args.end_hours), expected)

    def test_signed_minute_offsets_and_missing_endpoints_reach_worker(self):
        for changes, expected in (({"start_minutes": -5, "end_minutes": 15}, (-5, 15)),
                                  ({"start_minutes": -1.5}, (-1.5, 4320)),
                                  ({"end_minutes": 24}, (-5, 24)),
                                  ({"start_minutes": -5256000, "end_minutes": 5256000}, (-5256000, 5256000))):
            for legacy in (False, True):
                with self.subTest(changes=changes, legacy=legacy):
                    self.start({**self.body, **changes}, legacy=legacy)
                    args = parser().parse_args(self.server.start_job.call_args.args[0])
                    self.assertEqual((args.start_minutes, args.end_minutes), expected)
                    self.assertIsNone(args.start_hours)
                    self.assertIsNone(args.end_hours)

    def test_public_flag_requires_an_actual_boolean_before_admission(self):
        for public in (None, 0, 1, "false", [], {}):
            for legacy in (False, True):
                with self.subTest(public=public, legacy=legacy), self.assertRaises(TraceError):
                    self.start({**self.body, "public": public}, legacy=legacy)
        self.server.start_job.assert_not_called()

    def test_invalid_window_never_creates_a_job(self):
        invalid = [{"start_hours": None, "end_hours": None}, {"start_hours": None},
                   {"end_hours": None}, {"start_hours": True}, {"end_hours": False},
                   {"start_hours": "0"}, {"end_hours": []}, {"start_hours": float("nan")},
                   {"end_hours": float("inf")}, {"start_hours": -87601}, {"end_hours": 87601},
                   {"end_hours": 10 ** 1000},
                   {"start_hours": 72}, {"end_hours": 0}, {"start_hours": 3, "end_hours": -3}]
        for changes in invalid:
            for legacy in (False, True):
                with self.subTest(changes=changes, legacy=legacy), self.assertRaises(TraceError):
                    self.start({**self.body, **changes}, legacy=legacy)
        self.server.start_job.assert_not_called()

    def test_known_claim_still_validates_window_and_strict_allowed_fields(self):
        for changes in ({"start_hours": 24, "end_hours": 12}, {"base_url": "https://untrusted.invalid"},
                        {"action": "trace"}):
            with self.subTest(changes=changes), self.assertRaises(TraceError):
                self.start({**self.body, "liquid_txid": CLAIM, **changes})
        self.server.start_job.assert_not_called()

    def test_invalid_minutes_or_mixed_units_never_create_a_job(self):
        invalid = [{"start_minutes": None}, {"end_minutes": None}, {"start_minutes": True},
                   {"end_minutes": False}, {"start_minutes": "-5"}, {"end_minutes": []},
                   {"start_minutes": float("nan")}, {"end_minutes": float("inf")},
                   {"start_minutes": -5256001}, {"end_minutes": 5256001}, {"end_minutes": 10 ** 1000},
                   {"start_minutes": 4320}, {"end_minutes": -5}, {"start_minutes": 15, "end_minutes": -5},
                   {"start_minutes": -5, "end_hours": 72}, {"start_hours": None, "end_minutes": 15}]
        for changes in invalid:
            for legacy in (False, True):
                with self.subTest(changes=changes, legacy=legacy), self.assertRaises(TraceError):
                    self.start({**self.body, **changes}, legacy=legacy)
        self.server.start_job.assert_not_called()

    def test_selected_outputs_are_normalized_and_single_output_remains_compatible(self):
        for fields, expected in (({"vouts": [2, 0, 2]}, [0, 2]), ({"vouts": []}, None)):
            self.start({**self.body, **fields})
            args = parser().parse_args(self.server.start_job.call_args.args[0])
            self.assertEqual(args.vouts, expected)
            self.assertIsNone(args.vout)
        self.start({**self.body, "vout": 1})
        args = parser().parse_args(self.server.start_job.call_args.args[0])
        self.assertEqual(args.vout, 1)
        self.assertIsNone(args.vouts)

    def test_batch_and_reverse_admission_performs_no_case_or_network_reads(self):
        bodies = [({"mode": "batch", "txids": [BTC.upper(), CLAIM, BTC]}, [BTC, CLAIM]),
                  ({"mode": "reverse", "liquid_txid": CLAIM.upper()}, None)]
        with patch("liquid_tracer.investigations.read_case", side_effect=AssertionError("No case reads")), \
                patch("liquid_tracer.api.Esplora.get", side_effect=AssertionError("Worker-only API")):
            for identifiers, txids in bodies:
                with self.subTest(mode=identifiers["mode"]):
                    self.start({"action": "pegin-match", **identifiers, "max_requests": 500,
                                "max_seconds": 30.5, "public": False})
                    arguments = self.server.start_job.call_args.args[0]
                    args = parser().parse_args(arguments)
                    self.assertEqual(args.txids, txids)
                    self.assertEqual(args.from_liquid, CLAIM if txids is None else None)
                    self.assertIsNone(args.txid)
                    self.assertEqual((args.max_pages, args.max_requests, args.max_seconds), (100, 500, 30.5))
                    self.assertFalse(args.public)
                    self.assertEqual(args.investigations_dir, self.server.root)

    def test_batch_resume_identifier_is_forwarded_without_reading_saved_report(self):
        identity = "d" * 32
        with patch("liquid_tracer.pegin_matching.read_report", side_effect=AssertionError("Worker-only report reads")):
            self.start({"action": "pegin-match", "mode": "batch", "txids": [BTC], "resume_report": identity})
        args = parser().parse_args(self.server.start_job.call_args.args[0])
        self.assertEqual(args.resume_report, identity)

    def test_new_modes_reject_ambiguous_unbounded_and_injected_fields(self):
        single = {**self.body}
        batch = {"action": "pegin-match", "mode": "batch", "txids": [BTC]}
        reverse = {"action": "pegin-match", "mode": "reverse", "liquid_txid": CLAIM}
        invalid = [
            {**single, "vout": None, "vouts": []}, {**single, "vout": 1, "vouts": [1]},
            *({**single, "vouts": v} for v in (None, "1,2", [True], [-1], [0x40000000])),
            *({**batch, "txids": v} for v in (None, BTC, [], [BTC] * 501, [BTC, None], ["bad"])),
            *({**reverse, "liquid_txid": v} for v in (None, "", [CLAIM], "bad")),
            {**batch, "txid": BTC}, {**batch, "vouts": [0]}, {**batch, "liquid_txid": CLAIM},
            {**reverse, "txids": [BTC]}, {**reverse, "vout": 0},
            {**single, "resume_report": "d" * 32}, {**reverse, "resume_report": "d" * 32},
            *({**batch, "resume_report": value} for value in (None, "", "../report", [], "f" * 33)),
            *({**single, "mode": v} for v in (None, [], "invalid")),
        ]
        for body in (single, batch, reverse):
            invalid.extend({**body, key: value} for key, value in (
                ("max_requests", 0), ("max_requests", True), ("max_requests", 10001),
                ("max_seconds", 0), ("max_seconds", float("nan")), ("max_seconds", float("inf")),
                ("max_seconds", "10"), ("max_seconds", 10 ** 1000),
                ("case_id", "a" * 32), ("fixture", "/tmp/fixture.json"),
                ("base_url", "https://untrusted.invalid"), ("client_secret", "rejected")))
        for body in invalid:
            with self.subTest(body=body), self.assertRaises(TraceError):
                self.start(body)
        for body in (batch, reverse):
            with self.subTest(legacy=body), self.assertRaises(TraceError):
                self.start(body, legacy=True)
        self.server.start_job.assert_not_called()


class PeginOutputApiTests(unittest.TestCase):
    def setUp(self):
        self.server = SimpleNamespace(start_job=Mock(return_value={"id": "queued"}))

    def test_output_lookup_public_default_and_enterprise_arguments(self):
        for fields, public in (({}, True), ({"public": True}, True), ({"public": False}, False)):
            with self.subTest(fields=fields):
                result = start_workspace_outputs(self.server, {"txid": BTC.upper(), **fields})
                self.assertEqual(result, {"id": "queued"})
                arguments = ["inspect-txs", "--txids", BTC, "--blockchain", "bitcoin"]
                if public:
                    arguments += ["--auth", "none", "--base-url", "https://blockstream.info/api"]
                self.server.start_job.assert_called_with(arguments, action="pegin-outputs", live=True, txids=[BTC])

    def test_output_lookup_rejects_invalid_fields_before_job_admission(self):
        invalid = [{}, {"txid": None}, {"txid": "abc"}, {"txid": [BTC]}, {"txid": BTC + "," + BTC},
                   *({"txid": BTC, "public": value} for value in (None, 0, 1, "false", [], {})),
                   *({"txid": BTC, key: value} for key, value in (
                       ("blockchain", "liquid"), ("fixture", "/tmp/data.json"), ("base_url", "https://example.com"),
                       ("auth", "none"), ("case_id", "a" * 32), ("action", "lookup"),
                       ("client_secret", "never-accepted"), ("max_seconds", 3600)))]
        for body in invalid:
            with self.subTest(body=body), self.assertRaises(TraceError):
                start_workspace_outputs(self.server, body)
        self.server.start_job.assert_not_called()


class PeginQueryUnitsTests(unittest.TestCase):
    def test_minutes_are_saved_canonically_and_python_legacy_behavior_is_preserved(self):
        minutes = query(BTC, start_minutes=-5, end_minutes=15)
        self.assertEqual((minutes["start_minutes"], minutes["end_minutes"]), (-5, 15))
        self.assertNotIn("start_hours", minutes)
        hours = query(BTC, start_hours=-3.5, end_hours=6.25)
        self.assertEqual((hours["start_hours"], hours["end_hours"]), (-3.5, 6.25))
        self.assertNotIn("start_minutes", hours)
        unlimited = query(BTC)
        self.assertNotIn("start_minutes", unlimited)
        self.assertNotIn("start_hours", unlimited)

    def test_query_rejects_partial_invalid_and_mixed_unit_windows(self):
        for window in ({"start_minutes": -5}, {"end_minutes": 15},
                       {"start_minutes": -5, "end_minutes": -5},
                       {"start_minutes": -5, "end_minutes": 15, "start_hours": 0, "end_hours": 72}):
            with self.subTest(window=window), self.assertRaises(TraceError):
                query(BTC, **window)


class PeginCliTests(unittest.TestCase):
    def test_cli_forwards_batch_resume_without_reading_the_report(self):
        identity = "d" * 32
        with patch("liquid_tracer.pegin_matching.match_pegin_batch", return_value={"ok": True}) as match, \
                patch("liquid_tracer.pegin_matching.read_report", side_effect=AssertionError("Engine-only report reads")), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["pegin-match", "--investigations-dir", "/unused-workspace", "--txids", BTC,
                                   "--resume-report", identity], progress=lambda _: None), 0)
        self.assertEqual(match.call_args.kwargs["resume_report"], identity)
    def test_cli_routes_multiple_outputs_batch_and_reverse_with_common_options(self):
        for arguments, function, expected_args in (
                (["--txid", BTC, "--vouts", "1,0,1"], "match_pegin", (None, BTC)),
                (["--txids", BTC.upper() + "," + CLAIM + "," + BTC], "match_pegin_batch",
                 (Path("/unused-workspace"), [BTC, CLAIM])),
                (["--from-liquid", CLAIM], "match_liquid_pegin", (Path("/unused-workspace"), CLAIM))):
            with self.subTest(function=function), \
                    patch("liquid_tracer.pegin_matching." + function, return_value={"ok": True}) as match, \
                    contextlib.redirect_stdout(io.StringIO()):
                status = main(["pegin-match", "--investigations-dir", "/unused-workspace", *arguments,
                               "--public", "--max-requests", "321", "--max-seconds", "12.5"],
                              progress=lambda _: None)
                self.assertEqual(status, 0)
                self.assertEqual(match.call_args.args, expected_args)
                options = match.call_args.kwargs
                self.assertEqual((options["max_requests"], options["max_seconds"], options["public"]),
                                 (321, 12.5, True))
                self.assertEqual((options["start_minutes"], options["end_minutes"]), (-5, 4320))
                if function == "match_pegin":
                    self.assertEqual(options["vouts"], [0, 1])

    def test_cli_rejects_ambiguous_sources_and_output_selections(self):
        invalid = [["--txid", BTC, "--txids", CLAIM], ["--txid", BTC, "--from-liquid", CLAIM],
                   ["--txids", BTC, "--from-liquid", CLAIM],
                   ["--txid", BTC, "--vout", "0", "--vouts", "1"],
                   ["--txids", "not-a-hash"], ["--txids", ",".join([BTC] * 501)],
                   ["--txid", BTC, "--vouts", "-1"], ["--txid", BTC, "--vouts", "0,not-an-index"]]
        for arguments in invalid:
            with self.subTest(arguments=arguments), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                parser().parse_args(["pegin-match", *arguments])
        with patch("liquid_tracer.cli.read_case", side_effect=AssertionError("Validate before case read")), \
                contextlib.redirect_stderr(io.StringIO()):
            for arguments in (["--txids", BTC, "--case", "/unused-case"],
                              ["--from-liquid", CLAIM, "--vout", "1"],
                              ["--txid", BTC, "--resume-report", "d" * 32],
                              ["--from-liquid", CLAIM, "--resume-report", "d" * 32],
                              ["--txids", BTC, "--liquid-txid", CLAIM]):
                self.assertNotEqual(main(["pegin-match", *arguments], progress=lambda _: None), 0)

    def test_cli_dispatch_always_passes_window_and_preserves_public_option(self):
        for extra, unit, expected in (([], "minutes", (-5, 4320, False)),
                                (["--start-minutes", "-5", "--end-minutes", "15"], "minutes", (-5, 15, False)),
                                (["--end-minutes", "90"], "minutes", (-5, 90, False)),
                                (["--start-minutes", "-1.5"], "minutes", (-1.5, 4320, False)),
                                (["--end-hours", "24"], "hours", (0, 24, False)),
                                (["--start-hours", "-2.5", "--end-hours", "12.75", "--public"],
                                 "hours", (-2.5, 12.75, True))):
            with self.subTest(extra=extra), \
                    patch("liquid_tracer.pegin_matching.match_pegin", return_value={"ok": True}) as match, \
                    contextlib.redirect_stdout(io.StringIO()):
                status = main(["pegin-match", "--investigations-dir", "/unused-workspace", "--txid", BTC, *extra],
                              progress=lambda _: None)
                self.assertEqual(status, 0)
                options = match.call_args.kwargs
                self.assertEqual((options["start_" + unit], options["end_" + unit], options["public"]), expected)
                self.assertEqual(options["workspace"], Path("/unused-workspace"))

    def test_cli_rejects_mixed_units_in_both_orders(self):
        for extra in (["--start-minutes", "-5", "--end-hours", "72"],
                      ["--start-hours", "0", "--end-minutes", "15"],
                      ["--end-hours", "72", "--end-minutes", "15"]):
            with self.subTest(extra=extra), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                parser().parse_args(["pegin-match", "--txid", BTC, *extra])





class BridgeApiTests(unittest.TestCase):
    def setUp(self):
        self.server = SimpleNamespace(root=Path('/unused-workspace'), bridge_reports_root=Path('/owned-session/.pegin-matching'),
                                      start_job=Mock(return_value={'id':'queued'}))
        self.body = {'action':'pegin-match','bridge':'pegin','network':'bitcoin','mode':'single','txid':BTC}

    def test_four_directions_are_validated_before_session_worker_admission(self):
        with patch('liquid_tracer.bridge_matching.match_bridge', side_effect=AssertionError('No engine work at admission')):
            for bridge in ('pegin','pegout'):
                for network in ('bitcoin','liquid'):
                    fields = {**self.body,'bridge':bridge,'network':network}
                    start_workspace_match(self.server,fields)
                    args = parser().parse_args(self.server.start_job.call_args.args[0])
                    self.assertEqual(args.command,'bridge-match')
                    self.assertEqual((args.bridge,args.network,args.txid),(bridge,network,BTC))
                    self.assertEqual(args.report_root,self.server.bridge_reports_root)
                    self.assertRegex(args.report_id,r'^[0-9a-f]{32}$')
                    self.assertEqual(args.investigations_dir,self.server.root)
                    self.assertTrue(args.public)
                    self.assertIsNone(self.server.start_job.call_args.kwargs.get('case'))
                    self.assertEqual((args.start_minutes,args.end_minutes),(-4320,5) if (bridge,network)==('pegout','bitcoin') else (-5,4320))

    def test_batches_and_session_resume_propagate_without_browser_path_access(self):
        body = {'action':'pegin-match','bridge':'pegin','network':'liquid','mode':'batch',
                'txids':[CLAIM,BTC,CLAIM.upper()],'resume_report':'f'*32,'public':False,'max_requests':17,'max_seconds':20}
        start_workspace_match(self.server,body)
        args = parser().parse_args(self.server.start_job.call_args.args[0])
        self.assertEqual(args.txids,[CLAIM,BTC]);self.assertEqual(args.resume_report,'f'*32)
        self.assertFalse(args.public);self.assertEqual(args.max_requests,17);self.assertEqual(args.max_seconds,20)
        self.assertIsNone(args.txid)

    def test_invalid_bridge_requests_never_admit_jobs(self):
        changes = [{'bridge':'transfer'},{'network':'testnet'},{'mode':'reverse'},{'txid':None},
                   {'public':'false'},{'start_minutes':None},{'end_minutes':-5},{'vout':1,'vouts':[1]},
                   {'vouts':[True]},{'max_requests':True},{'max_seconds':float('inf')},
                   {'report_root':'/tmp'},{'report_id':'f'*32},{'case_id':'a'*32},{'fixture':'local'},
                   {'base_url':'https://invalid.example'},{'resume_report':'not-an-id'},
                   {'bridge':'pegin','network':'liquid','vout':1}]
        for fields in changes:
            with self.subTest(fields=fields),self.assertRaises(TraceError):
                start_workspace_match(self.server,{**self.body,**fields})
        for fields in ({'txids':[]},{'txids':[BTC]*501},{'txids':['bad']},{'txids':[BTC],'vouts':[1]},
                       {'txids':[BTC],'resume_report':None},{'txids':[BTC],'resume_report':'../escape'}):
            with self.subTest(fields=fields),self.assertRaises(TraceError):
                start_workspace_match(self.server,{'action':'pegin-match','bridge':'pegout','network':'liquid','mode':'batch',**fields})
        self.server.start_job.assert_not_called()

    def test_network_output_lookup_uses_specialized_worker_and_public_credentials_choice(self):
        for public in (False,True):
            for bridge in ('pegin','pegout'):
                start_workspace_outputs(self.server,{'txid':CLAIM,'network':'liquid','bridge':bridge,'public':public})
                args=parser().parse_args(self.server.start_job.call_args.args[0])
                self.assertEqual(args.command,'bridge-outputs');self.assertEqual(args.txid,CLAIM)
                self.assertEqual(args.network,'liquid');self.assertEqual(args.bridge,bridge)
                self.assertEqual(args.public,public)
                self.assertEqual(self.server.start_job.call_args.kwargs['txids'],[CLAIM])

    def test_bridge_cli_propagates_report_owner_budget_and_session_resume(self):
        result={'id':'e'*32}
        with patch('liquid_tracer.bridge_matching.match_bridge',return_value=result) as engine,contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(['bridge-match','--bridge','pegin','--network','liquid','--mode','batch',
                '--txids',CLAIM+','+BTC,'--investigations-dir','/synthetic/workspace','--report-root','/synthetic/session/.pegin-matching',
                '--report-id','e'*32,'--resume-report','f'*32,'--public','--max-requests','19']),0)
        arguments=engine.call_args
        self.assertEqual(arguments.args[0],Path('/synthetic/workspace'))
        self.assertEqual(arguments.kwargs['report_root'],Path('/synthetic/session/.pegin-matching'))
        self.assertEqual(arguments.kwargs['txids'],[CLAIM,BTC]);self.assertEqual(arguments.kwargs['max_requests'],19)
        self.assertEqual(arguments.kwargs['resume_report'],'f'*32)


if __name__ == "__main__":
    unittest.main()
