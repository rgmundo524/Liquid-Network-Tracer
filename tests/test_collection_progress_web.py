"""Real collection progress crosses the worker IPC boundary while still running."""

import sys
import time
import unittest
from unittest.mock import patch

from tests import test_web


class CollectionProgressWebTests(unittest.TestCase):
    setUp = test_web.LocalWebTests.setUp
    close_server = test_web.LocalWebTests.close_server
    request = test_web.LocalWebTests.request
    success = test_web.LocalWebTests.success
    wait = test_web.LocalWebTests.wait
    create = test_web.LocalWebTests.create

    def test_real_collection_reports_each_hop_before_fetching_and_can_reconnect(self):
        _, case = self.create()
        worker = self.base / "gated_collection_worker.py"
        worker.write_text('''import sys, time
from pathlib import Path
from liquid_tracer.progress import ProgressReporter
from liquid_tracer.web_worker import main
gates = Path(sys.argv[3])
original = ProgressReporter.__call__
seen = set()
def report(self, event):
    original(self, event)
    if event.get("phase") != "collecting" or event["completed"] in seen:
        return
    hop = event["completed"]
    seen.add(hop)
    deadline = time.monotonic() + 12
    while not (gates / ("release-" + str(hop))).exists():
        if time.monotonic() > deadline:
            raise RuntimeError("Synthetic hop gate timed out")
        time.sleep(.02)
ProgressReporter.__call__ = report
raise SystemExit(main(sys.argv[1:3]))
''')

        def command(request, result, live):
            self.assertFalse(live, "The real collection must use its saved fixture")
            return [sys.executable, str(worker), str(request), str(result), str(self.base)]

        with patch("liquid_tracer.web.worker_command", side_effect=command):
            job = self.success("/api/cases/" + case["id"] + "/actions",
                               {"action": "trace", "hops": 2}, 202)
            for hop in (0, 1, 2):
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    current = self.success("/api/jobs/" + job["id"])
                    progress = current.get("progress", {})
                    if progress.get("phase") == "collecting" and progress["completed"] == hop:
                        break
                    self.assertEqual(current["status"], "running", current)
                    time.sleep(.03)
                else:
                    self.fail("No live progress arrived for hop " + str(hop))
                self.assertEqual(current["status"], "running")
                self.assertEqual(progress["total"], 2)
                self.assertIn("Processing hop", progress["message"])
                # Reopening the page discovers the same job and current hop.
                self.assertEqual(self.success("/api/session")["active_job"], job["id"])
                self.assertEqual(self.success("/api/jobs/" + job["id"])["progress"], progress)
                (self.base / ("release-" + str(hop))).touch()
            result = self.wait(job)
        self.assertEqual(result["status"], "bounded_complete")
        final = self.success("/api/jobs/" + job["id"])
        self.assertEqual(final["progress"]["phase"], "exporting_collection")
        self.assertEqual((final["progress"]["completed"], final["progress"]["total"]), (1, 1))
        self.assertEqual(self.success("/api/cases/" + case["id"])["latest"]["collected_hops"], 2)


if __name__ == "__main__":
    unittest.main()
