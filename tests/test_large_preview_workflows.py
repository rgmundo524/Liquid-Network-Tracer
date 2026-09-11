"""Fallback previews remain usable across the CLI and a reopened local UI."""

import json
import unittest
from unittest.mock import patch

from liquid_tracer.cli import layout_preview_run, mermaid_run, verify_export
from liquid_tracer.common import read_json
from liquid_tracer.investigations import read_case
from liquid_tracer.web import LocalServer
from tests import test_web as web_tests


class LargePreviewWorkflowTests(unittest.TestCase):
    setUp = web_tests.LocalWebTests.setUp
    close_server = web_tests.LocalWebTests.close_server
    request = web_tests.LocalWebTests.request
    success = web_tests.LocalWebTests.success
    wait = web_tests.LocalWebTests.wait
    create = web_tests.LocalWebTests.create

    def test_fallback_outputs_are_downloadable_after_restart_without_retracing(self):
        _, case = self.create()
        route = "/api/cases/" + case["id"]
        saved = self.wait(self.success(route + "/actions", {"action": "trace"}, 202))
        path, _ = self.server.case(case["id"])
        archive = path / "runs" / saved["run_id"]
        before = {str(file.relative_to(archive)): file.read_bytes()
                  for file in archive.rglob("*") if file.is_file()}
        original_graph = read_json(archive / "graph.json")
        with patch("liquid_tracer.cli.Esplora", side_effect=AssertionError("No explorer calls")), \
                patch("liquid_tracer.miro._request", side_effect=AssertionError("No Miro calls")), \
                patch("liquid_tracer.elk_layout.MAX_NODES", 1), \
                patch("liquid_tracer.mermaid.MAX_RENDER_NODES", 1):
            layout = layout_preview_run(path)
            mermaid = mermaid_run(path)
        self.assertEqual(layout["layout_algorithm"], "dependency_layers_v1")
        self.assertEqual(layout["fallback_reason"], "size_limit")
        self.assertEqual(mermaid["renderer"], "direct_svg")
        self.assertEqual(mermaid["fallback_reason"], "size_limit")
        for result in (layout, mermaid):
            graph = read_json(result["graph"])
            self.assertEqual({node["id"] for node in graph["nodes"]},
                             {node["id"] for node in original_graph["nodes"]})
            self.assertEqual({edge["id"] for edge in graph["edges"]},
                             {edge["id"] for edge in original_graph["edges"]})
        reopened = LocalServer(self.server.root, self.assets, port=0)
        self.addCleanup(reopened.server_close)
        products = reopened.case_summary(path, read_case(path), detail=True)["artifacts"][saved["run_id"]]
        for kind, action, result in (("elk", "layout", layout), ("mermaid", "mermaid", mermaid)):
            public = self.server.public_result(result, action, path, None)
            product = products[kind]
            self.assertEqual(product["downloads"], public["downloads"])
            self.assertEqual(product["fallback_reason"], "size_limit")
            for key in ("layout_algorithm", "renderer"):
                if key in public:
                    self.assertEqual(product[key], public[key])
            self.assertNotIn(str(path), json.dumps(public))
            self.assertNotIn(str(path), json.dumps(product))
            svg = next(file for file in product["downloads"] if file["name"] == "graph.svg")
            status, content, response = self.request(svg["url"])
            self.assertEqual(status, 200)
            self.assertIn(b"<svg", content)
            self.assertIn("attachment", response.getheader("Content-Disposition"))
        verify_export(archive)
        self.assertEqual(before, {str(file.relative_to(archive)): file.read_bytes()
                                 for file in archive.rglob("*") if file.is_file()})

    def test_browser_only_receives_known_renderer_metadata(self):
        for algorithm in ("elk_layered_v1", "dependency_layers_v1"):
            for reason in ("size_limit", "timeout", "mermaid_size_limit", "mermaid_timeout"):
                report = {"layout_algorithm": algorithm, "fallback_reason": reason,
                          "renderer": "direct_svg", "fallback_notice": "SYNTHETIC-PRIVATE-NOTICE"}
                result = self.server.public_result(report, "miro-preview", None, None)
                self.assertEqual(result, {key: report[key] for key in ("layout_algorithm", "fallback_reason", "renderer")})
        for invalid in ("/private/path", "SYNTHETIC-PRIVATE-ERROR", True, None, {}, []):
            with self.subTest(invalid=invalid):
                report = {key: invalid for key in ("layout_algorithm", "renderer", "fallback_reason", "fallback_notice")}
                self.assertEqual(self.server.public_result(report, "miro-preview", None, None), {})


if __name__ == "__main__":
    unittest.main()
