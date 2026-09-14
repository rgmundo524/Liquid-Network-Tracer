import http.client
import json
import os
import pty
import select
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from liquid_tracer.cli import verify_export
from liquid_tracer.common import read_json, save_json
from liquid_tracer.web import LocalServer, worker_command


class LocalWebTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.assets = self.base / "assets"
        self.assets.mkdir()
        (self.assets / "index.html").write_text("<!doctype html><title>Synthetic UI</title>")
        self.server = LocalServer(self.base / "cases", self.assets, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def request(self, path, body=None, *, headers=None, raw=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        self.addCleanup(connection.close)
        defaults = {}
        if body is not None or raw is not None:
            defaults = {"Origin": self.server.origin, "X-Liquid-CSRF": self.server.csrf,
                        "Content-Type": "application/json"}
        defaults.update(headers or {})
        connection.request("POST" if body is not None or raw is not None else "GET", path,
                           body=raw if raw is not None else json.dumps(body) if body is not None else None,
                           headers=defaults)
        response = connection.getresponse()
        data = response.read()
        result = json.loads(data) if response.getheader("Content-Type", "").startswith("application/json") else data
        return response.status, result, response

    def success(self, path, body=None, status=200):
        code, data, response = self.request(path, body)
        self.assertEqual(code, status, data)
        self.assertEqual(response.getheader("Cache-Control"), "no-store")
        return data

    def wait(self, job):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            result = self.success("/api/jobs/" + job["id"])
            if result["status"] != "running":
                self.assertEqual(result["status"], "succeeded", result)
                return result["result"]
            time.sleep(.03)
        self.fail("Synthetic local action did not finish")

    def create(self):
        txid = self.success("/api/demo")["txids"][0]
        case = self.success("/api/cases", {"name": "Synthetic local investigation", "source": "demo",
                            "seeds": [txid + ":0"], "board": "", "settings": {"hops": 1}}, 201)
        return txid, case

    def test_session_security_origin_csrf_and_size(self):
        session = self.success("/api/session")
        self.assertEqual(session["cases"], [])
        self.assertTrue(session["csrf"])
        self.assertIsNone(session["active_job"])
        for headers in ({"Host": "attacker.example"}, {"Host": "localhost:" + str(self.server.server_port)},
                        {"Origin": "https://attacker.example"}, {"Sec-Fetch-Site": "cross-site"}):
            self.assertEqual(self.request("/api/session", headers=headers)[0], 403)
        for headers in ({"Origin": ""}, {"X-Liquid-CSRF": "wrong"},
                        {"Origin": "https://attacker.example"}):
            self.assertEqual(self.request("/api/settings", {"settings": {}}, headers=headers)[0], 403)
        self.assertEqual(self.request("/api/settings", {"settings": {}}, headers={"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.request("/api/settings", raw="x" * (65536 + 1))[0], 413)
        self.assertEqual(self.request("/api/settings", raw="[]")[0], 400)
        self.assertEqual(self.request("/api/settings", raw="{")[0], 400)
        self.assertFalse(self.server.root.exists())
        self.assertIn(b"Synthetic UI", self.success("/"))

    def test_demo_lookup_trace_continue_csv_and_reopen_share_saved_cases(self):
        txid = self.success("/api/demo")["txids"][0]
        lookup_job = self.success("/api/lookup", {"source": "demo", "txids": txid + ", " + txid}, 202)
        self.assertIsNone(lookup_job["case_id"])
        self.assertFalse(lookup_job["live"])
        self.assertEqual(lookup_job["action"], "lookup")
        lookup = self.wait(lookup_job)
        self.assertEqual(lookup["transactions"][0]["txid"], txid)
        outputs = lookup["transactions"][0]["outputs"]
        self.assertTrue(any(output["selectable"] for output in outputs))
        _, case = self.create()
        identity = case["id"]
        route = "/api/cases/" + identity
        trace_job = self.success(route + "/actions", {"action": "trace"}, 202)
        recovered_job = self.success("/api/jobs/" + trace_job["id"])
        self.assertEqual(recovered_job["case_id"], identity)
        self.assertFalse(recovered_job["live"])
        self.assertEqual(recovered_job["action"], "trace")
        self.assertEqual(case["seed_count"], 1)
        initial = self.wait(trace_job)
        self.assertEqual(initial["status"], "bounded_complete")
        path = self.server.case(identity)[0]
        archive = path / "runs" / initial["run_id"]
        before = {str(file.relative_to(archive)): file.read_bytes() for file in archive.rglob("*") if file.is_file()}
        second = self.wait(self.success(route + "/actions", {"action": "trace", "settings": {"hops": 1}}, 202))
        self.assertNotEqual(initial["run_id"], second["run_id"])
        detail = self.success(route)
        self.assertEqual(detail["latest_run"], second["run_id"])
        self.assertEqual(len(detail["runs"]), 2)
        self.assertGreaterEqual(detail["latest"]["transaction_count"], 2)
        csv = self.wait(self.success(route + "/actions", {"action": "csv", "run_id": initial["run_id"]}, 202))
        self.assertEqual(csv["run_id"], initial["run_id"])
        self.assertEqual(len(csv["downloads"]), 9)
        self.assertNotIn("directory", csv)
        for file in csv["downloads"]:
            status, data, response = self.request(file["url"])
            self.assertEqual(status, 200)
            self.assertTrue(data)
            self.assertIn("attachment", response.getheader("Content-Disposition"))
        verify_export(archive)
        self.assertEqual(before, {str(file.relative_to(archive)): file.read_bytes() for file in archive.rglob("*") if file.is_file()})
        reopened = LocalServer(self.server.root, self.assets, port=0)
        self.addCleanup(reopened.server_close)
        self.assertEqual(reopened.session()["cases"][0]["latest_run"], second["run_id"])
        self.assertIsNone(reopened.active_job)
        saved = reopened.case_summary(path, read_json(path / "case.json"), detail=True)["artifacts"]
        self.assertEqual(saved[initial["run_id"]]["csv"]["downloads"], csv["downloads"])
        self.assertFalse(saved[initial["run_id"]]["csv"]["include_fees"])
        self.assertNotIn(second["run_id"], saved)
        self.assertNotIn(str(path), json.dumps(saved))
        self.assertEqual(read_json(path / "case.json")["run_defaults"]["hops"], 1)

    def test_saved_downloads_skip_incomplete_wrong_case_and_linked_products(self):
        _, case = self.create()
        route = "/api/cases/" + case["id"]
        traced = self.wait(self.success(route + "/actions", {"action": "trace"}, 202))
        path, metadata = self.server.case(case["id"])
        run_id = traced["run_id"]
        preview = path / "previews" / (run_id + "-mermaid-" + "a" * 8)
        preview.mkdir(parents=True)
        graph = read_json(path / "runs" / run_id / "graph.json")
        for name in ("graph.svg", "graph.mmd", "mermaid-node-map.json", "mermaid-config.json"):
            (preview / name).write_text("SYNTHETIC DOWNLOAD")
        save_json(preview / "graph.json", graph)
        # A renderer failure leaves source/metadata, but not a usable preview.
        self.assertEqual(self.success(route)["artifacts"], {})
        (preview / "graph.html").write_text("<!doctype html><title>Synthetic preview</title>")
        product = self.success(route)["artifacts"][run_id]["mermaid"]
        self.assertEqual(len(product["downloads"]), 6)
        svg = next(file for file in product["downloads"] if file["name"] == "graph.svg")
        status, content, response = self.request(svg["url"])
        self.assertEqual(status, 200)
        self.assertEqual(content, b"SYNTHETIC DOWNLOAD")
        self.assertIn("sandbox;", response.getheader("Content-Security-Policy"))
        self.assertNotIn("allow-popups", response.getheader("Content-Security-Policy"))
        self.assertEqual(response.getheader("Content-Disposition"), 'attachment; filename="graph.svg"')
        self.assertTrue(product["preview_url"].endswith("/graph.html"))
        self.assertFalse(product["include_fees"])
        later = path / "previews" / (run_id + "-mermaid-" + "b" * 8)
        later.mkdir()
        for file in preview.iterdir():
            (later / file.name).write_bytes(file.read_bytes())
        graph["namespace"]["case_id"] = "f" * 32
        save_json(later / "graph.json", graph)
        self.assertEqual(self.success(route)["artifacts"][run_id]["mermaid"], product)
        graph["namespace"]["case_id"] = metadata["case_id"]
        save_json(later / "graph.json", graph)
        (later / "graph.svg").unlink()
        (later / "graph.svg").symlink_to(preview / "graph.svg")
        self.assertEqual(self.success(route)["artifacts"][run_id]["mermaid"], product)

    def test_actions_serialize_and_validate_before_starting_jobs(self):
        _, case = self.create()
        route = "/api/cases/" + case["id"]
        self.server.active_job = "synthetic-busy"
        try:
            for path, body in (("/api/settings", {"settings": {}}), (route + "/settings", {"name": "change"}),
                               (route + "/actions", {"action": "trace"}),
                               ("/api/lookup", {"source": "live", "txids": "bad"})):
                self.assertEqual(self.request(path, body)[0], 409)
        finally:
            self.server.active_job = None
        with patch.object(self.server, "start_job") as start:
            for action in ("shell", "miro-preview", "miro-sync", "miro-organize", "csv"):
                self.assertEqual(self.request(route + "/actions", {"action": action})[0], 400)
            self.assertEqual(self.request(route + "/actions", {"action": "trace", "run_id": "other"})[0], 400)
            self.assertEqual(self.request(route + "/actions", {"action": "trace", "settings": {"hops": -1}})[0], 400)
            start.assert_not_called()
        result = self.success(route + "/settings", {"name": "Renamed", "board": "SYNTHETIC-BOARD", "settings": {"include_fees": True}})
        self.assertEqual(result["name"], "Renamed")
        self.assertTrue(result["run_defaults"]["include_fees"])
        self.assertEqual(self.success("/api/settings", {"settings": {"hops": 2}})["settings"]["hops"], 2)
        self.assertEqual(self.success(route)["run_defaults"]["hops"], 1)

    def test_paths_and_symlinks_cannot_expose_unrelated_files(self):
        _, case = self.create()
        secret = self.base / "sentinel.txt"
        secret.write_text("SYNTHETIC-PRIVATE-SENTINEL")
        (self.assets / "leak.txt").symlink_to(secret)
        (self.assets / "outside").symlink_to(self.base, target_is_directory=True)
        for path in ("/leak.txt", "/outside/sentinel.txt", "/%2e%2e/sentinel.txt", "/api/session?x=1",
                     "/files/" + case["id"] + "/case.json", "/files/" + case["id"] + "/exports/a/../../case.json"):
            status, data, _ = self.request(path)
            self.assertEqual(status, 404, path)
            self.assertNotIn("SYNTHETIC-PRIVATE-SENTINEL", str(data))
        case_path = self.server.case(case["id"])[0]
        directory = case_path / "exports" / ("a" * 16 + "-csv-" + "b" * 8)
        directory.mkdir(parents=True)
        (directory / "nodes.csv").symlink_to(secret)
        self.assertEqual(self.request("/files/" + case["id"] + "/exports/" + directory.name + "/nodes.csv")[0], 404)
        other = self.base / "linked-cases"
        other.mkdir()
        (self.server.root / "outside-case").symlink_to(other, target_is_directory=True)
        self.assertNotIn(self.server.root / "outside-case", self.server.case_paths())

    def test_live_jobs_construct_secret_provider_command_without_browser_credentials(self):
        txid = self.success("/api/demo")["txids"][0]
        with patch.object(self.server, "start_job", return_value={"id": "synthetic", "status": "running"}) as start, \
                patch.dict(os.environ, {"BLOCKSTREAM_CLIENT_SECRET": "SYNTHETIC-NEVER-EXPOSE"}):
            self.success("/api/lookup", {"source": "live", "txids": txid}, 202)
            self.assertTrue(start.call_args.kwargs["live"])
            self.assertEqual(start.call_args.args[0], ["inspect-txs", "--txids", txid])
            self.assertNotIn("SYNTHETIC-NEVER-EXPOSE", json.dumps(self.success("/api/session")))
        with patch.dict(os.environ, {"LIQUID_SECRETSPEC_BIN": "/synthetic/secretspec", "LIQUID_SECRET_PROFILE": "development",
                                    "LIQUID_SECRET_PROVIDER": "protonpass"}):
            command = worker_command(Path("request.json"), Path("result.json"), live=True)
            self.assertEqual(command[0], "/synthetic/secretspec")
            self.assertIn("protonpass", command)
            self.assertIn("development", command)
            self.assertEqual(command[-4:], ["-m", "liquid_tracer.web_worker", "request.json", "result.json"])
        offline = worker_command(Path("request.json"), Path("result.json"), live=False)
        self.assertNotIn("secretspec", offline)

    def test_miro_preview_remains_offline_and_uses_selected_run(self):
        _, case = self.create()
        route = "/api/cases/" + case["id"]
        traced = self.wait(self.success(route + "/actions", {"action": "trace"}, 202))
        self.success(route + "/settings", {"board": "SYNTHETIC-BOARD"})
        original = worker_command
        with patch("liquid_tracer.web.worker_command", wraps=original) as command:
            result = self.wait(self.success(route + "/actions", {"action": "miro-preview", "run_id": traced["run_id"]}, 202))
            self.assertFalse(command.call_args.args[2])
        self.assertEqual(result["run_id"], traced["run_id"])
        self.assertGreater(result["new_items"], 0)
        self.assertTrue(result["remote_preflight_required"])
        self.assertIn("fee_items_to_remove", result)
        self.assertGreaterEqual(result["new_frames"], 2)
        self.assertIn("frames_to_remove", result)
        self.assertNotIn("state_file", result)
        self.assertEqual(self.server.public_result({"conflicts": [{"private": "annotation"}]}, "miro-sync", None, None),
                         {"conflicts_count": 1})

    def test_lookup_preserves_explicit_integer_amounts_above_javascript_precision(self):
        txid = "a" * 64
        amounts = [2 ** 53 + 1, 0, None]
        report = {"transactions": [{"txid": txid, "outputs": [
            {"vout": index, "outpoint": txid + ":" + str(index), "selectable": True, "value": amount}
            for index, amount in enumerate(amounts)]}]}
        result = self.server.public_result(report, "lookup", None, [txid])
        outputs = result["transactions"][0]["outputs"]
        self.assertEqual([output["value_text"] for output in outputs], ["9007199254740993", "0", None])
        self.assertEqual([output["value"] for output in outputs], amounts)
        self.assertNotIn("value_text", report["transactions"][0]["outputs"][0])
        self.assertEqual(self.server.public_result({"conflicts": []}, "miro-sync", None, None),
                         {"conflicts_count": 0})
        self.assertEqual(self.server.public_result({"conflicts": ["one", "two"]}, "miro-sync", None, None),
                         {"conflicts_count": 2})

    def test_failed_lookup_is_sanitized_and_incomplete_run_is_not_listed(self):
        job = self.success("/api/lookup", {"source": "demo", "txids": "a" * 64}, 202)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            status = self.success("/api/jobs/" + job["id"])
            if status["status"] != "running":
                break
            time.sleep(.03)
        self.assertEqual(status["status"], "failed")
        self.assertNotIn("result", status)
        self.assertIn("launching terminal", status["message"])
        self.assertNotIn(str(self.base), json.dumps(status))
        _, case = self.create()
        path = self.server.case(case["id"])[0] / "runs" / ("a" * 16)
        path.mkdir(parents=True)
        (path / "trace.json").write_text(json.dumps({"run_id": path.name, "case_id": case["id"], "status": "running"}))
        self.assertEqual(self.success("/api/cases/" + case["id"])["runs"], [])

    def test_server_shutdown_unwinds_separate_mermaid_renderer_group(self):
        _, case = self.create()
        route = "/api/cases/" + case["id"]
        self.wait(self.success(route + "/actions", {"action": "trace"}, 202))
        pid_file = self.base / "synthetic-renderer.pid"
        renderer = self.base / "synthetic-mmdc"
        renderer.write_text("#!" + sys.executable + "\nimport os, time\nfrom pathlib import Path\n"
                            + "Path(" + repr(str(pid_file)) + ").write_text(str(os.getpid()))\ntime.sleep(60)\n")
        renderer.chmod(0o700)
        with patch.dict(os.environ, {"LIQUID_MERMAID_BIN": str(renderer)}):
            job = self.success(route + "/actions", {"action": "mermaid"}, 202)
            deadline = time.monotonic() + 10
            while not pid_file.exists() and time.monotonic() < deadline:
                time.sleep(.03)
            self.assertTrue(pid_file.is_file(), self.server.jobs[job["id"]])
            pid = int(pid_file.read_text())
            self.server.shutdown()
            self.server.server_close()
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        self.assertEqual(self.server.jobs[job["id"]]["status"], "failed")
        self.assertIsNone(self.server.active_job)

    def test_cancel_mermaid_stops_its_separate_renderer_without_changing_saved_runs(self):
        _, case = self.create()
        route = "/api/cases/" + case["id"]
        traced = self.wait(self.success(route + "/actions", {"action": "trace"}, 202))
        case_path, _ = self.server.case(case["id"])
        archive = case_path / "runs" / traced["run_id"]
        before = {str(path.relative_to(archive)): path.read_bytes()
                  for path in archive.rglob("*") if path.is_file()}
        pid_file = self.base / "cancel-renderer.pid"
        renderer = self.base / "cancel-mmdc"
        renderer.write_text("#!" + sys.executable + "\nimport os, time\nfrom pathlib import Path\n"
                            + "Path(" + repr(str(pid_file)) + ").write_text(str(os.getpid()))\ntime.sleep(60)\n")
        renderer.chmod(0o700)
        with patch.dict(os.environ, {"LIQUID_MERMAID_BIN": str(renderer)}):
            job = self.success(route + "/actions", {"action": "mermaid"}, 202)
            self.assertTrue(job["cancellable"])
            self.assertIsInstance(job["started_at"], float)
            deadline = time.monotonic() + 10
            while not pid_file.exists() and time.monotonic() < deadline:
                time.sleep(.03)
            self.assertTrue(pid_file.is_file(), self.server.jobs[job["id"]])
            cancel = "/api/jobs/" + job["id"] + "/cancel"
            for headers in ({"X-Liquid-CSRF": "wrong"}, {"Origin": "https://attacker.example"}):
                self.assertEqual(self.request(cancel, {}, headers=headers)[0], 403)
            self.assertEqual(self.request("/api/jobs/" + "f" * 32 + "/cancel", {})[0], 404)
            self.assertEqual(self.success("/api/jobs/" + job["id"])["status"], "running")
            response = self.success(cancel, {}, 202)
            self.assertEqual(response["status"], "cancelling")
            self.assertFalse(response["cancellable"])
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                completed = self.success("/api/jobs/" + job["id"])
                if completed["status"] not in ("running", "cancelling"):
                    break
                time.sleep(.03)
        self.assertEqual(completed["status"], "canceled", completed)
        self.assertNotIn("result", completed)
        self.assertIn("unchanged", completed["message"])
        with self.assertRaises(ProcessLookupError):
            os.kill(int(pid_file.read_text()), 0)
        self.assertIsNone(self.server.active_job)
        self.assertEqual(self.request(cancel, {})[0], 409)
        verify_export(archive)
        self.assertEqual(before, {str(path.relative_to(archive)): path.read_bytes()
                                 for path in archive.rglob("*") if path.is_file()})
        self.assertEqual(self.success(route)["artifacts"], {})

    def test_cancel_before_launch_is_idempotent_and_cannot_stop_a_different_job(self):
        with patch("liquid_tracer.web.threading.Thread.start"):
            job = self.server.start_job([], action="layout")
        self.server.job_thread = None
        route = "/api/jobs/" + job["id"] + "/cancel"
        self.assertEqual(self.success(route, {}, 202)["status"], "cancelling")
        self.assertEqual(self.success(route, {}, 202)["status"], "cancelling")
        with patch("liquid_tracer.web.subprocess.Popen") as launch:
            self.server.run_job(job["id"], [], "layout", False, None, None)
            launch.assert_not_called()
        self.assertEqual(self.server.jobs[job["id"]]["status"], "canceled")
        with patch("liquid_tracer.web.threading.Thread.start"):
            new = self.server.start_job([], action="layout")
        self.server.job_thread = None
        try:
            self.assertEqual(self.request(route, {})[0], 409)
            self.assertEqual(self.server.jobs[new["id"]]["status"], "running")
        finally:
            self.server.active_job = None

    def test_cancel_rejects_trace_and_miro_mutations_even_for_nonlive_fixtures(self):
        for action, live in (("trace", False), ("miro-sync", False), ("miro-organize", True)):
            with self.subTest(action=action), patch("liquid_tracer.web.threading.Thread.start"):
                job = self.server.start_job([], action=action, live=live)
            self.server.job_thread = None
            try:
                self.assertFalse(job["cancellable"])
                self.assertEqual(self.request("/api/jobs/" + job["id"] + "/cancel", {})[0], 409)
                self.assertEqual(self.server.jobs[job["id"]]["status"], "running")
            finally:
                self.server.active_job = None

    @unittest.skipUnless(os.name == "posix", "The local devenv terminal is POSIX")
    def test_interactive_worker_receives_terminal_and_foreground_is_restored(self):
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        script = '''
import fcntl, os, signal, subprocess, sys, termios
from liquid_tracer.web import handoff_terminal, restore_terminal
fcntl.ioctl(0, termios.TIOCSCTTY, 0)
signal.signal(signal.SIGTTOU, signal.SIG_IGN)
previous = os.tcgetpgrp(0)
process = subprocess.Popen([sys.executable, '-c', "answer = input('SYNTHETIC-PROMPT: '); print('RECEIVED:' + answer, flush=True)"], process_group=0)
terminal = handoff_terminal(process, True)
process.wait(timeout=5)
restore_terminal(terminal)
print('RESTORED:' + str(os.tcgetpgrp(0) == previous), flush=True)
'''
        process = subprocess.Popen([sys.executable, "-c", script], stdin=slave, stdout=slave, stderr=slave,
                                   start_new_session=True)
        os.close(slave)
        self.addCleanup(lambda: process.kill() if process.poll() is None else None)
        received = b""
        answered = False
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if select.select([master], [], [], .1)[0]:
                try:
                    data = os.read(master, 65536)
                except OSError:
                    break
                received += data
                if b"SYNTHETIC-PROMPT:" in received and not answered:
                    os.write(master, b"fixture-answer\n")
                    answered = True
            if process.poll() is not None and b"RESTORED:" in received:
                break
        self.assertEqual(process.wait(timeout=2), 0, received.decode(errors="replace"))
        self.assertIn(b"RECEIVED:fixture-answer", received)
        self.assertIn(b"RESTORED:True", received)
