"""Independent local servers keep their ports, tokens, jobs, and built assets."""

import errno
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from liquid_tracer.web import LocalServer, _open_server


class WebInstanceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.assets = self.root / "assets"
        self.assets.mkdir()
        (self.assets / "index.html").write_text("First build")

    def server(self, port=0):
        server = LocalServer(self.root / "cases", self.assets, port)
        self.addCleanup(server.server_close)
        return server

    def test_second_default_instance_uses_another_port_and_independent_session(self):
        first = self.server()
        with patch("liquid_tracer.web.DEFAULT_PORT", first.server_port):
            second = _open_server(self.root / "cases", self.assets)
        self.addCleanup(second.server_close)
        self.assertNotEqual(first.server_port, second.server_port)
        self.assertEqual(second.server_address[0], "127.0.0.1")
        self.assertNotEqual(first.csrf, second.csrf)
        self.assertEqual(second.host, "127.0.0.1:" + str(second.server_port))
        self.assertEqual(second.origin, "http://" + second.host)
        first.jobs["synthetic-job"] = {"id": "synthetic-job", "status": "running"}
        self.assertEqual(first.session()["active_job"], "synthetic-job")
        self.assertEqual(second.session()["active_jobs"], [])
        self.assertEqual(second.jobs, {})
        self.assertEqual(first.root, second.root)

    def test_explicit_occupied_port_is_not_silently_changed(self):
        first = self.server()
        with self.assertRaises(OSError) as raised:
            _open_server(self.root / "cases", self.assets, first.server_port)
        self.assertEqual(raised.exception.errno, errno.EADDRINUSE)

    def test_non_collision_bind_errors_are_not_hidden(self):
        with patch("liquid_tracer.web.LocalServer", side_effect=OSError(errno.EACCES, "denied")) as factory:
            with self.assertRaises(OSError) as raised:
                _open_server(self.root / "cases", self.assets)
        self.assertEqual(raised.exception.errno, errno.EACCES)
        self.assertEqual(factory.call_count, 1)

    def test_busy_default_range_falls_back_to_os_selected_port(self):
        real = LocalServer

        def create(root, assets, port):
            if port:
                raise OSError(errno.EADDRINUSE, "busy")
            return real(root, assets, port)

        with patch("liquid_tracer.web.LocalServer", side_effect=create) as factory:
            server = _open_server(self.root / "cases", self.assets)
        self.addCleanup(server.server_close)
        self.assertGreater(server.server_port, 0)
        self.assertEqual(factory.call_count, 33)

    def test_server_keeps_original_assets_when_shared_pointer_changes(self):
        pointer = self.root / "dist"
        pointer.symlink_to(self.assets, target_is_directory=True)
        first = LocalServer(self.root / "cases", pointer, 0)
        self.addCleanup(first.server_close)
        newer = self.root / "newer"
        newer.mkdir()
        (newer / "index.html").write_text("Second build")
        temporary = self.root / "next-dist"
        temporary.symlink_to(newer, target_is_directory=True)
        temporary.replace(pointer)
        second = LocalServer(self.root / "cases", pointer, 0)
        self.addCleanup(second.server_close)
        self.assertEqual((first.assets / "index.html").read_text(), "First build")
        self.assertEqual((second.assets / "index.html").read_text(), "Second build")


if __name__ == "__main__":
    unittest.main()
