"""Exercise the real terminal handoff while authenticated jobs overlap."""

import os
import pty
import select
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


PROVIDER = r'''
import json, os, sys
from pathlib import Path
request, result, worker = sys.argv[1:]
root, label = json.loads(Path(request).read_text())["arguments"]
Path(root, label + ".prompting").touch()
answer = input("PROMPT-" + label + ": ")
assert answer == "fixture-answer", answer
os.environ["SYNTHETIC_CREDENTIAL"] = "PRIVATE-FIXTURE-VALUE"
os.execv(sys.executable, [sys.executable, worker, request, result])
'''


WORKER = r'''
import json, os, sys, time
from pathlib import Path
from liquid_tracer import web_worker
def fixture_cli(arguments, **kwargs):
    root, label = arguments
    assert not sys.stdin.isatty()
    assert sys.stdin.read() == ""
    assert os.environ["SYNTHETIC_CREDENTIAL"] == "PRIVATE-FIXTURE-VALUE"
    Path(root, label + ".started").touch()
    deadline = time.monotonic() + 15
    while not Path(root, label + ".release").exists():
        assert time.monotonic() < deadline, "worker release timed out"
        time.sleep(.02)
    print(json.dumps({"label": label}))
    return 0
web_worker.cli_main = fixture_cli
raise SystemExit(web_worker.main())
'''


SERVER = r'''
import fcntl, json, os, signal, sys, termios, time
from pathlib import Path
from liquid_tracer import web
from liquid_tracer.investigations import create_investigation
root, provider, worker = sys.argv[1:]
root = Path(root)
fcntl.ioctl(0, termios.TIOCSCTTY, 0)
signal.signal(signal.SIGTTOU, signal.SIG_IGN)
previous = os.tcgetpgrp(0)
web.worker_command = lambda request, result, live=False: [
    sys.executable, provider, str(request), str(result), worker]
server = web.LocalServer(root / "cases", root, port=0)
server.public_result = lambda result, *args: result
def wait_for(predicate):
    deadline = time.monotonic() + 15
    while not predicate():
        assert time.monotonic() < deadline, "server condition timed out"
        time.sleep(.02)
try:
    cases = [create_investigation(root / "cases", label,
             seeds=["a" * 64 + ":0"]) for label in ("first", "second")]
    first = server.start_job([str(root), "first"], action="layout", live=True, case=cases[0])
    wait_for(lambda: (root / "first.prompting").exists())
    second = server.start_job([str(root), "second"], action="layout", live=True, case=cases[1])
    wait_for(lambda: "waiting" in server.jobs[second["id"]]["message"].lower())
    assert not (root / "second.prompting").exists()
    print("SECOND-QUEUED", flush=True)
    wait_for(lambda: (root / "first.started").exists() and (root / "second.started").exists())
    assert server.jobs[first["id"]]["status"] == "running"
    assert server.jobs[second["id"]]["status"] == "running"
    assert os.tcgetpgrp(0) == previous
    assert "SYNTHETIC_CREDENTIAL" not in os.environ
    assert "PRIVATE-FIXTURE-VALUE" not in json.dumps(server.jobs)
    print("BOTH-RUNNING-TERMINAL-RESTORED", flush=True)
    for label in ("first", "second"):
        (root / (label + ".release")).touch()
    wait_for(lambda: all(server.jobs[j["id"]]["status"] == "succeeded" for j in (first, second)))
    print("BOTH-SUCCEEDED", flush=True)
finally:
    server.server_close()
'''


@unittest.skipUnless(os.name == "posix", "Credential prompts use a POSIX terminal")
class CredentialQueueTests(unittest.TestCase):
    def test_prompts_take_turns_but_authenticated_jobs_run_together(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            provider, worker = root / "provider.py", root / "worker.py"
            provider.write_text(PROVIDER)
            worker.write_text(WORKER)
            master, slave = pty.openpty()
            process = subprocess.Popen(
                [sys.executable, "-c", SERVER, str(root), str(provider), str(worker)],
                stdin=slave, stdout=slave, stderr=slave, start_new_session=True,
            )
            os.close(slave)
            received = b""
            answered = set()
            try:
                deadline = time.monotonic() + 25
                while time.monotonic() < deadline:
                    if select.select([master], [], [], .1)[0]:
                        try:
                            data = os.read(master, 65536)
                        except OSError:
                            break
                        if not data:
                            break
                        received += data
                    if b"SECOND-QUEUED" in received and "first" not in answered:
                        os.write(master, b"fixture-answer\n")
                        answered.add("first")
                    if b"PROMPT-second:" in received and "second" not in answered:
                        os.write(master, b"fixture-answer\n")
                        answered.add("second")
                    if process.poll() is not None:
                        break
                output = received.decode(errors="replace")
                self.assertEqual(process.wait(timeout=2), 0, output)
                self.assertEqual(answered, {"first", "second"}, output)
                self.assertIn("BOTH-RUNNING-TERMINAL-RESTORED", output)
                self.assertIn("BOTH-SUCCEEDED", output)
                self.assertNotIn("PRIVATE-FIXTURE-VALUE", output)
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=12)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                os.close(master)
