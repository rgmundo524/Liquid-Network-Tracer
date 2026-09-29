import contextlib
import errno
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from liquid_tracer.web import Handler, LocalServer


class BrowserConnection:
    """Exercise the real HTTP handler with deterministic socket failures."""

    def __init__(self, request, *, fail_write=None, error=BrokenPipeError):
        self.reader = io.BytesIO(request)
        self.fail_write = fail_write
        self.error = error
        self.write_count = 0
        self.output = []

    def makefile(self, *args):
        return self.reader

    def settimeout(self, timeout):
        pass

    def sendall(self, data):
        self.write_count += 1
        if self.write_count == self.fail_write:
            raise self.error("Browser connection closed")
        self.output.append(bytes(data))


class BrowserDisconnectTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        assets = root / "assets"
        assets.mkdir()
        (assets / "index.html").write_text("<!doctype html><title>Synthetic UI</title>")
        self.server = LocalServer(root / "cases", assets, port=0)
        self.addCleanup(self.server.server_close)

    def request_bytes(self, path="/", body=None):
        raw = json.dumps(body).encode() if body is not None else b""
        headers = [f"{'POST' if body is not None else 'GET'} {path} HTTP/1.1",
                   "Host: " + self.server.host]
        if body is not None:
            headers += ["Origin: " + self.server.origin,
                        "X-Liquid-CSRF: " + self.server.csrf,
                        "Content-Type: application/json",
                        "Content-Length: " + str(len(raw))]
        return ("\r\n".join(headers) + "\r\n\r\n").encode() + raw

    def handle(self, connection):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            handler = Handler(connection, ("127.0.0.1", 10000), self.server)
        self.assertTrue(handler.close_connection)
        return stderr.getvalue()

    def test_disconnected_response_headers_or_body_do_not_retry_or_log(self):
        for error in (BrokenPipeError, ConnectionResetError):
            for fail_write in (1, 2):
                with self.subTest(error=error.__name__, fail_write=fail_write):
                    connection = BrowserConnection(self.request_bytes(), fail_write=fail_write, error=error)
                    self.assertEqual(self.handle(connection), "")
                    self.assertEqual(connection.write_count, fail_write)
                    self.assertNotIn(b"400", b"".join(connection.output))

    def test_disconnect_during_request_error_response_is_quiet(self):
        for error in (BrokenPipeError, ConnectionResetError):
            for fail_write in (1, 2):
                with self.subTest(error=error.__name__, fail_write=fail_write):
                    connection = BrowserConnection(self.request_bytes("/missing"),
                                                   fail_write=fail_write, error=error)
                    self.assertEqual(self.handle(connection), "")
                    self.assertEqual(connection.write_count, fail_write)

    def test_disconnect_during_generic_error_response_keeps_original_diagnostic(self):
        connection = BrowserConnection(self.request_bytes(), fail_write=1)
        with patch.object(Handler, "get", side_effect=OSError(errno.EIO, "Synthetic disk failure")):
            message = self.handle(connection)
        self.assertEqual(message.count("Local UI request failed:"), 1)
        self.assertIn("Synthetic disk failure", message)
        self.assertNotIn("Browser connection closed", message)
        self.assertEqual(connection.write_count, 1)

    def test_real_request_errors_still_return_their_status_and_message(self):
        connection = BrowserConnection(self.request_bytes("/missing"))
        self.assertEqual(self.handle(connection), "")
        self.assertIn(b"404", connection.output[0])
        self.assertEqual(json.loads(connection.output[1]), {"error": "Page not found"})

        connection = BrowserConnection(self.request_bytes())
        with patch.object(Handler, "get", side_effect=OSError(errno.EIO, "Synthetic disk failure")):
            self.assertIn("Synthetic disk failure", self.handle(connection))
        self.assertIn(b"400", connection.output[0])
        self.assertIn("Request could not be completed", json.loads(connection.output[1])["error"])

    def test_disconnect_while_reading_request_or_body_is_quiet(self):
        for method in ("readline", "read"):
            for error in (BrokenPipeError, ConnectionResetError):
                with self.subTest(method=method, error=error.__name__):
                    connection = BrowserConnection(self.request_bytes("/api/settings", {"settings": {}}))
                    with patch.object(connection.reader, method, side_effect=error("Browser connection closed")), \
                            patch.object(Handler, "post") as post:
                        self.assertEqual(self.handle(connection), "")
                    self.assertEqual(connection.write_count, 0)
                    post.assert_not_called()

    def test_accepted_job_finishes_after_browser_disconnect_and_can_be_polled(self):
        finish = threading.Event()
        self.addCleanup(finish.set)

        def worker(identity, *args):
            if finish.wait(timeout=3):
                with self.server.job_lock:
                    self.server.jobs[identity]["status"] = "succeeded"
                    self.server.active_job = None

        connection = BrowserConnection(self.request_bytes("/api/lookup", {"txids": "a" * 64}), fail_write=1)
        with patch.object(self.server, "run_job", side_effect=worker) as run:
            self.assertEqual(self.handle(connection), "")
            self.assertEqual(len(self.server.jobs), 1)
            identity = next(iter(self.server.jobs))
            self.assertEqual(self.server.jobs[identity]["status"], "running")
            self.assertEqual(self.server.active_job, identity)
            self.assertEqual(connection.write_count, 1)
            finish.set()
            self.server.job_thread.join(timeout=3)
            self.assertFalse(self.server.job_thread.is_alive())
            run.assert_called_once()

        polling = BrowserConnection(self.request_bytes("/api/jobs/" + identity))
        self.assertEqual(self.handle(polling), "")
        self.assertEqual(json.loads(polling.output[1])["status"], "succeeded")


if __name__ == "__main__":
    unittest.main()
