"""Saved-plot navigation changes HTTP presentation, never archived evidence."""

import contextlib
import io
from html.parser import HTMLParser
import unittest
from unittest.mock import patch

from liquid_tracer.preview_toolbar import add_preview_toolbar
from liquid_tracer.web import Handler, LocalServer
from liquid_tracer.workflow_api import _plot_summary, plot_artifact
from tests.test_web_disconnects import BrowserConnection
from tests import test_workflow_summaries


HTML = b'<!doctype html><html><head><title>Saved</title></head><body><svg></svg></body></html>'


class Tags(HTMLParser):
    def __init__(self, document):
        super().__init__()
        self.tags = []
        self.feed(document.decode())

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


class PreviewToolbarTests(unittest.TestCase):
    report = test_workflow_summaries.WorkflowSummaryTests.report

    def setUp(self):
        test_workflow_summaries.WorkflowSummaryTests.setUp(self)
        self.directory = self.report()
        (self.directory / "graph.html").write_bytes(HTML)
        # Exercise the real HTTP handler without opening a socket or service.
        self.server = LocalServer.__new__(LocalServer)
        self.server.root = self.root
        self.server.host = "127.0.0.1:4321"
        self.server.origin = "http://" + self.server.host
        self.server.csrf = "synthetic-csrf"
        self.route = f"/files/{self.identity}/previews/{self.directory.name}/graph.html"

    def request(self, route, *, headers=None, method="GET"):
        fields = {"Host": self.server.host, **(headers or {})}
        request = (f"{method} {route} HTTP/1.1\r\n" + "".join(
            f"{key}: {value}\r\n" for key, value in fields.items()) + "\r\n").encode()
        connection = BrowserConnection(request)
        with contextlib.redirect_stderr(io.StringIO()):
            Handler(connection, ("127.0.0.1", 10000), self.server)
        head, body = b"".join(connection.output).split(b"\r\n\r\n", 1)
        lines = head.decode().split("\r\n")
        return int(lines[0].split()[1]), dict(line.split(": ", 1) for line in lines[1:]), body

    def test_served_toolbar_links_exact_preview_without_rewriting_files(self):
        before = {path.name: path.read_bytes() for path in self.directory.iterdir()}
        with patch("liquid_tracer.plots.reviewed_plot") as review:
            status, headers, body = self.request(self.route)
        self.assertEqual(status, 200)
        review.assert_called_once_with(self.case, self.directory.name)
        tags = Tags(body).tags
        link = next(attrs for tag, attrs in tags if tag == "a")
        self.assertEqual(link["href"], f"/#case/{self.identity}/preview/{self.directory.name}/miro")
        self.assertEqual(link["target"], "_blank")
        self.assertIn("noopener", link["rel"].split())
        self.assertIn(b"Use this preview in Miro", body)
        self.assertNotIn("Content-Disposition", headers)
        self.assertEqual(headers["Content-Length"], str(len(body)))
        policy = headers["Content-Security-Policy"]
        for value in ("default-src 'none'", "allow-popups", "allow-popups-to-escape-sandbox", "allow-same-origin"):
            self.assertIn(value, policy)
        for value in ("allow-scripts", "allow-forms", "script-src"):
            self.assertNotIn(value, policy)
        self.assertFalse(any(tag in {"script", "form", "iframe"} for tag, _ in tags))
        self.assertEqual(before, {path.name: path.read_bytes() for path in self.directory.iterdir()})

    def test_download_uses_original_bytes_and_same_verification(self):
        with patch("liquid_tracer.plots.reviewed_plot") as review:
            status, headers, body = self.request(self.route + "?download=1")
        self.assertEqual(status, 200)
        self.assertEqual(body, HTML)
        self.assertEqual(headers["Content-Disposition"], 'attachment; filename="graph.html"')
        review.assert_called_once_with(self.case, self.directory.name)
        self.assertEqual((self.directory / "graph.html").read_bytes(), HTML)

    def test_download_links_are_separate_from_plain_preview_url(self):
        for artifact in (plot_artifact(self.case, self.directory.name, verified=True),
                         _plot_summary(self.case, self.directory, self.identity)["artifact"]):
            self.assertEqual(artifact["preview_url"], self.route)
            links = {item["name"]: item["url"] for item in artifact["downloads"]}
            self.assertEqual(links["graph.html"], self.route + "?download=1")
            self.assertTrue(all("?" not in url for name, url in links.items() if name != "graph.html"))

    def test_other_preview_files_and_legacy_graphs_are_not_wrapped(self):
        for name in ("details.html", "graph.svg"):
            (self.directory / name).write_bytes(HTML)
        legacy = self.case / "previews" / ("a" * 16 + "-elk-00000001")
        legacy.mkdir()
        (legacy / "graph.html").write_bytes(HTML)
        urls = [self.route.replace("graph.html", name) for name in ("details.html", "graph.svg")]
        urls.append(self.route.replace(self.directory.name, legacy.name))
        with patch("liquid_tracer.plots.reviewed_plot"):
            for route in urls:
                with self.subTest(route=route):
                    status, _, body = self.request(route)
                    self.assertEqual(status, 200)
                    self.assertEqual(body, HTML)
                    self.assertEqual(self.request(route + "?download=1")[0], 404)

    def test_download_query_remains_narrow_and_case_ids_are_validated(self):
        invalid = [self.route + suffix for suffix in (
            "?download=0", "?download=1&extra=1", "?download=1&download=1", "?cursor=x")]
        invalid += [self.route.replace(self.identity, "not-a-case") + "?download=1",
                    self.route.replace("previews", "exports") + "?download=1",
                    self.route.replace(self.directory.name, self.directory.name.upper()) + "?download=1",
                    "/api/session?download=1"]
        with patch("liquid_tracer.plots.reviewed_plot") as review:
            for route in invalid:
                with self.subTest(route=route):
                    self.assertEqual(self.request(route)[0], 404)
            review.assert_not_called()

    def test_security_and_invalid_saved_files_are_not_bypassed(self):
        for suffix in ("", "?download=1"):
            self.assertEqual(self.request(self.route + suffix, headers={"Host": "attacker.example"})[0], 403)
            self.assertEqual(self.request(self.route + suffix, headers={"Origin": "https://attacker.example"})[0], 403)
            self.assertEqual(self.request(self.route + suffix, method="POST")[0], 403)
            status, _, body = self.request(self.route + suffix)
            self.assertEqual(status, 400)  # Synthetic manifest is intentionally not valid.
            self.assertNotIn(b"Use this preview in Miro", body)

    def test_dynamic_link_segments_cannot_inject_markup(self):
        body = add_preview_toolbar(HTML, '\"><script>x</script>&', '\" onclick="x/#?')
        tags = Tags(body).tags
        self.assertFalse(any(tag == "script" for tag, _ in tags))
        links = [attrs for tag, attrs in tags if tag == "a"]
        self.assertEqual(len(links), 1)
        self.assertTrue(all("onclick" not in attrs for attrs in links))
        self.assertIn("%22%3E%3Cscript%3Ex%3C%2Fscript%3E%26", links[0]["href"])
        self.assertIn("%22%20onclick%3D%22x%2F%23%3F", links[0]["href"])


if __name__ == "__main__":
    unittest.main()
