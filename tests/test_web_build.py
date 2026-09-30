"""Concurrent launchers install once and never replace an active UI build."""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from liquid_tracer import web_build


class WebBuildTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for name in ("layout", "web"):
            directory = self.root / name
            directory.mkdir()
            (directory / "package.json").write_text('{"name":"' + name + '"}')
            (directory / "package-lock.json").write_text('{}')
        (self.root / "web" / "src").mkdir()
        (self.root / "web" / "src" / "page.astro").write_text("first page")
        self.npm = self.root / "fake-npm"
        self.npm.write_text(f"#!{sys.executable}\n" + '''
import json
import os
from pathlib import Path
import sys
import time

directory = Path.cwd()
root = directory.parent
with (root / "events.jsonl").open("a") as handle:
    handle.write(json.dumps([directory.name, *sys.argv[1:]]) + "\\n")
hold = root / f"hold-{directory.name}-{sys.argv[1]}"
if hold.exists():
    (root / "npm.pid").write_text(str(os.getpid()))
    while hold.exists():
        time.sleep(0.01)
time.sleep(0.08)
if sys.argv[1] == "ci":
    (directory / "node_modules").mkdir(exist_ok=True)
else:
    if (root / "fail-build").exists():
        raise SystemExit(9)
    output = Path(sys.argv[sys.argv.index("--outDir") + 1])
    output.mkdir(exist_ok=True)
    content = (directory / "src" / "page.astro").read_text()
    (output / "index.html").write_text(content)
    (output / "asset.js").write_text(content)
''')
        self.npm.chmod(0o755)

    def build(self):
        return web_build.build_web(self.root, str(self.npm))

    def events(self):
        import json
        return [json.loads(line) for line in (self.root / "events.jsonl").read_text().splitlines()]

    def launcher(self):
        return subprocess.Popen([sys.executable, "-m", "liquid_tracer.web_build", "web",
                                 "--root", str(self.root), "--npm", str(self.npm)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def npm_pid(self):
        deadline = time.monotonic() + 5
        path = self.root / "npm.pid"
        while time.monotonic() < deadline:
            if path.is_file() and path.read_text():
                return int(path.read_text())
            time.sleep(0.01)
        self.fail("npm did not reach the blocked stage")

    def test_two_processes_install_and_build_once_then_reuse(self):
        command = [sys.executable, "-m", "liquid_tracer.web_build", "web",
                   "--root", str(self.root), "--npm", str(self.npm)]
        processes = [subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                     for _ in range(2)]
        try:
            results = [process.communicate(timeout=15) for process in processes]
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.communicate()
        for process, (out, err) in zip(processes, results):
            self.assertEqual(process.returncode, 0, out + err)
        events = self.events()
        self.assertEqual(len(events), 3, events)
        self.assertEqual([event[:2] for event in events], [["layout", "ci"], ["web", "ci"], ["web", "run"]])
        self.assertIn("--ignore-scripts", events[0])
        self.assertNotIn("--ignore-scripts", events[1])
        self.assertTrue((self.root / "web" / "dist").is_symlink())
        self.assertEqual((self.root / "web" / "dist" / "index.html").read_text(), "first page")
        self.assertIn("Waiting for another Liquid Tracer instance", "".join(out for out, _ in results))
        self.assertIn("Reusing the completed", "".join(out for out, _ in results))

    def test_rebuild_keeps_resolved_assets_and_publishes_new_version(self):
        old = self.build()
        pinned = (self.root / "web" / "dist").resolve()
        self.assertEqual(pinned, old)
        (self.root / "web" / "src" / "page.astro").write_text("second page")
        new = self.build()
        self.assertNotEqual(old, new)
        self.assertEqual((pinned / "index.html").read_text(), "first page")
        self.assertEqual((pinned / "asset.js").read_text(), "first page")
        self.assertEqual((self.root / "web" / "dist" / "index.html").read_text(), "second page")
        self.assertEqual(len(self.events()), 4)

    def test_failed_build_keeps_published_version_and_removes_staging(self):
        old = self.build()
        (self.root / "web" / "src" / "page.astro").write_text("second page")
        (self.root / "fail-build").touch()
        with self.assertRaises(subprocess.CalledProcessError):
            self.build()
        self.assertEqual((self.root / "web" / "dist").resolve(), old)
        self.assertEqual(list((self.root / web_build._CACHE_NAME / "web").glob("building-*")), [])
        (self.root / "fail-build").unlink()
        self.assertNotEqual(self.build(), old)

    def test_cancelled_build_releases_lock_and_cleans_staging(self):
        self.build()
        (self.root / "web" / "src" / "page.astro").write_text("second page")
        with patch.object(web_build, "_run", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.build()
        self.assertEqual(list((self.root / web_build._CACHE_NAME / "web").glob("building-*")), [])
        # A subsequent acquisition/build completes without a stale lock.
        self.build()

    def test_killed_launcher_leaves_lock_with_live_installer(self):
        hold = self.root / "hold-layout-ci"
        hold.touch()
        first = self.launcher()
        second = None
        try:
            child_pid = self.npm_pid()
            first.kill()
            first.wait(timeout=5)
            os.kill(child_pid, 0)  # The installer remains alive after SIGKILL.
            with (self.root / web_build._CACHE_NAME / "layout.lock").open("a") as handle:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            second = self.launcher()
            hold.unlink()
            out, err = second.communicate(timeout=15)
            self.assertEqual(second.returncode, 0, out + err)
            first.communicate(timeout=5)
            self.assertEqual((self.root / "web" / "dist" / "index.html").read_text(), "first page")
        finally:
            hold.unlink(missing_ok=True)
            for process in (first, second):
                if process is not None:
                    if process.poll() is None:
                        process.kill()
                    process.communicate(timeout=10)

    def test_sigterm_stops_build_before_cleanup_and_releases_lock(self):
        old = self.build()
        (self.root / "web" / "src" / "page.astro").write_text("second page")
        hold = self.root / "hold-web-run"
        hold.touch()
        process = self.launcher()
        try:
            child_pid = self.npm_pid()
            process.terminate()
            out, err = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 130, out + err)
            with self.assertRaises(ProcessLookupError):
                os.kill(child_pid, 0)
            self.assertEqual((self.root / "web" / "dist").resolve(), old)
            self.assertEqual(list((self.root / web_build._CACHE_NAME / "web").glob("building-*")), [])
            with (self.root / web_build._CACHE_NAME / "web.lock").open("a") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            hold.unlink(missing_ok=True)
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=10)

    def test_signal_inside_spawn_replays_after_child_ownership_and_reaps_it(self):
        popen = subprocess.Popen
        children = []

        def signal_before_return(*args, **kwargs):
            child = popen(*args, **kwargs)
            children.append(child)
            os.kill(os.getpid(), signal.SIGINT)
            return child

        with web_build._lock(self.root, "layout") as lock_fd:
            with patch.object(web_build.subprocess, "Popen", side_effect=signal_before_return):
                with self.assertRaises(KeyboardInterrupt):
                    web_build._run([sys.executable, "-c", "import time; time.sleep(30)"], self.root, lock_fd)
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].returncode)
        with self.assertRaises(ProcessLookupError):
            os.kill(children[0].pid, 0)
        with (self.root / web_build._CACHE_NAME / "layout.lock").open("a") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_source_public_config_and_lock_changes_invalidate_cache(self):
        web = self.root / "web"
        original = web_build._source_key(web)
        for name in ("astro.config.mjs", "package-lock.json", "tsconfig.json"):
            path = web / name
            previous = path.read_bytes() if path.exists() else None
            path.write_text("changed")
            self.assertNotEqual(web_build._source_key(web), original, name)
            if previous is None:
                path.unlink()
            else:
                path.write_bytes(previous)
        (web / "public").mkdir()
        (web / "public" / "icon.svg").write_text("public asset")
        self.assertNotEqual(web_build._source_key(web), original)

    def test_build_outputs_and_env_files_do_not_change_key(self):
        web = self.root / "web"
        original = web_build._source_key(web)
        for name in ("dist", "node_modules", ".astro"):
            (web / name).mkdir()
            (web / name / "generated.txt").write_text("generated")
        (web / ".env").write_text("SECRET=test-placeholder")
        self.assertEqual(web_build._source_key(web), original)

    def test_legacy_directory_is_retained_when_publishing(self):
        old = self.root / "web" / "dist"
        old.mkdir()
        (old / "index.html").write_text("legacy page")
        self.build()
        legacy = list((self.root / web_build._CACHE_NAME / "web").glob("legacy-*"))
        self.assertEqual(len(legacy), 1)
        self.assertEqual((legacy[0] / "index.html").read_text(), "legacy page")
        self.assertTrue(old.is_symlink())


if __name__ == "__main__":
    unittest.main()
