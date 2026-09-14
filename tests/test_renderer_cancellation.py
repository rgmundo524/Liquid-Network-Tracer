import contextlib
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from liquid_tracer.elk_layout import _worker
from liquid_tracer.mermaid import _render
from liquid_tracer.processes import defer_cancellation_during_spawn


def interrupt(signum, frame):
    raise KeyboardInterrupt


@unittest.skipUnless(os.name == "posix", "Renderer process groups require POSIX")
class RendererCancellationTests(unittest.TestCase):
    def test_actual_signal_during_spawn_reaps_renderer_and_its_children(self):
        real_popen = subprocess.Popen
        child_script = (
            "import itertools,pathlib,sys,time\n"
            "p=pathlib.Path(sys.argv[1])\n"
            "for tick in itertools.count(): p.write_text(str(tick)); time.sleep(.01)\n"
        )
        parent_script = (
            "import subprocess,sys\n"
            "child=subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2]])\n"
            "child.wait()\n"
        )
        for engine in ("elk", "mermaid"):
            for signum in (signal.SIGINT, signal.SIGTERM):
                with self.subTest(engine=engine, signum=signum), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    tick_file = root / "child.tick"
                    layout = root / "layout"
                    (layout / "node_modules" / "elkjs").mkdir(parents=True)
                    (layout / "run.mjs").write_text("// Synthetic worker")
                    (layout / "node_modules" / "elkjs" / "package.json").write_text("{}")
                    processes = []
                    returned = []

                    def launch_then_signal(*args, **kwargs):
                        process = real_popen([sys.executable, "-c", parent_script,
                                              child_script, str(tick_file)], **kwargs)
                        processes.append(process)
                        deadline = time.monotonic() + 5
                        while not tick_file.exists() and time.monotonic() < deadline:
                            time.sleep(.01)
                        self.assertTrue(tick_file.exists(), "Synthetic renderer child did not start")
                        # Model cancellation after the OS created the process but
                        # before the caller receives its Popen handle.
                        os.kill(os.getpid(), signum)
                        returned.append(True)
                        return process

                    previous = signal.signal(signum, interrupt)
                    try:
                        module = "elk_layout" if engine == "elk" else "mermaid"
                        with patch.dict(os.environ, {"LIQUID_TRACER_ROOT": str(root),
                                                      "LIQUID_NODE_BIN": sys.executable}), \
                                patch(f"liquid_tracer.{module}.subprocess.Popen", side_effect=launch_then_signal):
                            with self.assertRaises(KeyboardInterrupt):
                                if engine == "elk":
                                    _worker({}, [1])
                                else:
                                    _render([sys.executable], root)
                        self.assertEqual(returned, [True], "Signal handler ran before process ownership")
                        self.assertIs(signal.getsignal(signum), interrupt)
                        self.assertIsNotNone(processes[0].returncode, "Renderer parent was not reaped")
                        tick = tick_file.read_text()
                        time.sleep(.08)
                        self.assertEqual(tick_file.read_text(), tick, "Renderer descendant survived cancellation")
                    finally:
                        signal.signal(signum, previous)
                        for process in processes:
                            with contextlib.suppress(ProcessLookupError):
                                os.killpg(process.pid, signal.SIGKILL)
                            process.wait(timeout=5)

    def test_default_and_ignored_handlers_are_unchanged(self):
        previous_int = signal.signal(signal.SIGINT, signal.SIG_IGN)
        previous_term = signal.signal(signal.SIGTERM, signal.SIG_DFL)
        try:
            with defer_cancellation_during_spawn():
                self.assertEqual(signal.getsignal(signal.SIGINT), signal.SIG_IGN)
                self.assertEqual(signal.getsignal(signal.SIGTERM), signal.SIG_DFL)
            self.assertEqual(signal.getsignal(signal.SIGINT), signal.SIG_IGN)
            self.assertEqual(signal.getsignal(signal.SIGTERM), signal.SIG_DFL)
        finally:
            signal.signal(signal.SIGINT, previous_int)
            signal.signal(signal.SIGTERM, previous_term)

    def test_failed_spawn_restores_handlers(self):
        previous = signal.signal(signal.SIGTERM, interrupt)
        try:
            with self.assertRaisesRegex(OSError, "Synthetic spawn failure"):
                with defer_cancellation_during_spawn():
                    raise OSError("Synthetic spawn failure")
            self.assertIs(signal.getsignal(signal.SIGTERM), interrupt)
        finally:
            signal.signal(signal.SIGTERM, previous)

    def test_worker_threads_do_not_change_signal_handlers(self):
        completed = []

        def run():
            with defer_cancellation_during_spawn():
                completed.append(True)

        with patch("liquid_tracer.processes.signal.signal") as install:
            thread = threading.Thread(target=run)
            thread.start()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            install.assert_not_called()
        self.assertEqual(completed, [True])
