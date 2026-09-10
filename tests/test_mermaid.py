import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

from liquid_tracer.api import Esplora, Limits
from liquid_tracer.common import TraceError, save_json
from liquid_tracer.export import COLORS, build_graph
from liquid_tracer.mermaid import RENDER_TIMEOUT, _render, export_mermaid, mermaid_source
from liquid_tracer.store import Store
from liquid_tracer.trace import new_state, trace
from tests.fixtures import A, B, fixture


def tiny_graph():
    return {"run_id": "synthetic-run", "nodes": [
        {"id": "tx:synthetic", "kind": "transaction", "label": "TX\n2023-11-14 UTC\nhop 0",
         "color": COLORS["starting_transaction"]},
        {"id": "address:synthetic", "kind": "address", "label": "Synthetic recipient",
         "color": COLORS["seed"]}],
        "edges": [{"id": "out:synthetic:0", "source": "tx:synthetic", "target": "address:synthetic",
                   "role": "seed_output", "label": "vout 0", "quantity": "?? ??"}]}


class MermaidTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.destination = self.root / "preview"

    def traced_graph(self, include_fees=False):
        store = Store(self.root / "case")
        self.addCleanup(store.close)
        fixture_path = self.root / "fixture.json"
        save_json(fixture_path, fixture())
        limits = Limits(max_hops=2)
        api = Esplora(store, "pending", limits, fixture=fixture_path, min_interval=0)
        state = new_state([A + ":0", B + ":0"], api.base, limits, [])
        api.run_id = state["run_id"]
        state = trace(api, state, limits, self.root / "run" / "trace.json")
        return build_graph(state, include_fees=include_fees)

    @staticmethod
    def fake_renderer(command, directory):
        destination = Path(command[command.index("--output") + 1])
        destination.write_text('<svg xmlns="http://www.w3.org/2000/svg"><text>Rendered chart</text></svg>')
        return 0

    def test_real_trace_preserves_roles_dates_shapes_and_all_directions(self):
        graph = self.traced_graph(include_fees=True)
        before = copy.deepcopy(graph)
        source = mermaid_source(graph)
        ids = {node["id"]: f"n{index}" for index, node in enumerate(sorted(graph["nodes"], key=lambda n: n["id"]))}
        for node in graph["nodes"]:
            node_id = ids[node["id"]]
            if node["kind"] == "transaction":
                self.assertIn(f'{node_id}["TX<br/>', source)
            elif node["kind"] == "address":
                self.assertIn(f'{node_id}(("', source)
            else:
                self.assertIn(f'{node_id}{{"', source)
            self.assertIn(f"style {node_id} fill:{node['color']},", source)
        for edge in graph["edges"]:
            self.assertIn(f"{ids[edge['source']]} -->|", source)
            self.assertIn(f"| {ids[edge['target']]}\n", source)
        starting = [node for node in graph["nodes"] if node["id"] in {"tx:" + A, "tx:" + B}]
        self.assertEqual({node["color"] for node in starting}, {COLORS["starting_transaction"]})
        self.assertIn("2023-11-14 UTC", source)
        self.assertIn("vout 0 · ?? ??", source)
        self.assertIn("FEE<br/>", source)
        self.assertEqual(source.count(" -->|"), len(graph["edges"]))
        self.assertEqual(graph, before)
        graph["nodes"].reverse()
        graph["edges"].reverse()
        self.assertEqual(mermaid_source(graph), source)

    def test_fee_visibility_uses_selected_graph_without_inventing_edges(self):
        graph = self.traced_graph()
        source = mermaid_source(graph)
        self.assertNotIn("FEE<br/>", source)
        self.assertEqual(source.count(" -->|"), len(graph["edges"]))

    def test_escaping_cannot_add_directives_nodes_links_or_html(self):
        graph = tiny_graph()
        hostile = '"]\n%%{init: {"securityLevel": "loose"}}%%\nclick n0 "https://example.invalid"\n<script>bad</script>|#34;`\\'
        graph["nodes"][0]["label"] = hostile
        graph["edges"][0]["quantity"] = hostile
        source = mermaid_source(graph)
        self.assertNotIn("<script>", source)
        self.assertNotIn("%%{", source)
        self.assertNotIn('\nclick ', source)
        self.assertNotIn('"https://', source)
        self.assertIn("#35;34#59;", source)
        self.assertIn("#34;#93;<br/>", source)
        self.assertEqual(source.count(" -->|"), 1)
        self.assertEqual(source.count("\n  style "), 2)

    def test_cycles_remain_real_directed_links(self):
        graph = tiny_graph()
        graph["edges"].append({"id": "input:synthetic", "source": "address:synthetic", "target": "tx:synthetic",
                               "role": "traced_input", "label": "vin 0", "quantity": "?? ??"})
        source = mermaid_source(graph)
        self.assertIn('n0 -->|"vin 0 · ?? ??"| n1', source)
        self.assertIn('n1 -->|"vout 0 · ?? ??"| n0', source)

    def test_source_config_mapping_and_portable_html_are_written(self):
        graph = tiny_graph()
        graph["run_id"] = '<script>synthetic</script>'
        with patch.dict(os.environ, {"LIQUID_MERMAID_BIN": "/synthetic/mmdc"}), \
                patch("liquid_tracer.mermaid._render", side_effect=self.fake_renderer) as run:
            result = export_mermaid(graph, self.destination)
        self.assertEqual(set(result), {"directory", "source", "svg", "html", "graph", "node_map"})
        self.assertTrue(all(Path(value).is_absolute() and Path(value).exists() for value in result.values()))
        self.assertEqual(json.loads(Path(result["graph"]).read_text()), graph)
        self.assertEqual(json.loads(Path(result["node_map"]).read_text()), {"n0": "address:synthetic", "n1": "tx:synthetic"})
        preview = Path(result["html"]).read_text()
        self.assertIn("data:image/svg+xml;base64,", preview)
        self.assertNotIn("<script>", preview)
        self.assertNotIn("https://", preview)
        self.assertIn("&lt;script&gt;synthetic&lt;/script&gt;", preview)
        self.assertIn("provided starting transactions", preview)
        self.assertIn("Miro positions and fixed transaction connection sides are not copied", preview)
        self.assertEqual(run.call_args.args[0][0], "/synthetic/mmdc")
        self.assertEqual(run.call_args.args[1], self.destination)
        config = json.loads((self.destination / "mermaid-config.json").read_text())
        self.assertEqual(config["securityLevel"], "strict")
        self.assertFalse(config["htmlLabels"])

    def test_large_graph_config_preserves_edges_above_mermaid_default(self):
        graph = tiny_graph()
        graph["edges"] = [dict(graph["edges"][0], id=f"edge:{index}") for index in range(510)]
        with patch.dict(os.environ, {"LIQUID_MERMAID_BIN": "/synthetic/mmdc"}), \
                patch("liquid_tracer.mermaid._render", side_effect=self.fake_renderer):
            export_mermaid(graph, self.destination)
        config = json.loads((self.destination / "mermaid-config.json").read_text())
        source = (self.destination / "graph.mmd").read_text()
        self.assertGreater(config["maxEdges"], 510)
        self.assertGreater(config["maxTextSize"], len(source))
        self.assertEqual(source.count(" -->|"), 510)

    def test_missing_renderer_keeps_source_and_actionable_error(self):
        with patch.dict(os.environ, {"LIQUID_MERMAID_BIN": ""}), patch("liquid_tracer.mermaid.shutil.which", return_value=None):
            with self.assertRaisesRegex(TraceError, "devenv shell.*graph.mmd"):
                export_mermaid(tiny_graph(), self.destination)
        self.assertTrue((self.destination / "graph.mmd").is_file())
        self.assertFalse((self.destination / "graph.html").exists())

    def test_renderer_failures_keep_source_without_echoing_case_or_secrets(self):
        cases = [1,
                 subprocess.TimeoutExpired([], RENDER_TIMEOUT), FileNotFoundError("PRIVATE PATH")]
        for index, outcome in enumerate(cases):
            destination = self.root / f"failed-{index}"
            kwargs = {"side_effect": outcome} if isinstance(outcome, Exception) else {"return_value": outcome}
            with patch.dict(os.environ, {"LIQUID_MERMAID_BIN": "/synthetic/mmdc"}), \
                    patch("liquid_tracer.mermaid._render", **kwargs):
                with self.assertRaises(TraceError) as caught:
                    export_mermaid(tiny_graph(), destination)
            self.assertIn("graph.mmd", str(caught.exception))
            self.assertNotIn("PRIVATE", str(caught.exception))
            self.assertTrue((destination / "graph.mmd").is_file())
            self.assertFalse((destination / "graph.html").exists())

    def test_success_exit_requires_usable_svg(self):
        for index, output in enumerate((None, "not SVG", '<svg xmlns="http://www.w3.org/2000/svg"/>',
                                        '<svg xmlns="http://www.w3.org/2000/svg" aria-roledescription="error"/>')):
            destination = self.root / f"invalid-{index}"
            def broken_renderer(command, directory):
                if output is not None:
                    Path(command[command.index("--output") + 1]).write_text(output)
                return 0
            with patch.dict(os.environ, {"LIQUID_MERMAID_BIN": "/synthetic/mmdc"}), \
                    patch("liquid_tracer.mermaid._render", side_effect=broken_renderer):
                with self.assertRaisesRegex(TraceError, "usable SVG"):
                    export_mermaid(tiny_graph(), destination)
            self.assertFalse((destination / "graph.svg").exists())
            self.assertFalse((destination / "graph.html").exists())

    def test_existing_export_is_never_overwritten(self):
        self.destination.mkdir()
        existing = self.destination / "graph.mmd"
        existing.write_text("existing analyst work")
        with patch("liquid_tracer.mermaid._render") as run:
            with self.assertRaisesRegex(TraceError, "already exists"):
                export_mermaid(tiny_graph(), self.destination)
        run.assert_not_called()
        self.assertEqual(existing.read_text(), "existing analyst work")

    def test_timeout_terminates_renderer_child_processes(self):
        child_file = self.root / "child.pid"
        script = (
            "import subprocess,sys,pathlib; "
            "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
            "pathlib.Path(sys.argv[1]).write_text(str(child.pid)); child.wait()"
        )
        with patch("liquid_tracer.mermaid.RENDER_TIMEOUT", 0.5):
            with self.assertRaises(subprocess.TimeoutExpired):
                _render([sys.executable, "-c", script, str(child_file)], self.root)
        self.assertTrue(child_file.is_file(), "Synthetic renderer must have launched its child")
        child_pid = int(child_file.read_text())
        status = Path(f"/proc/{child_pid}/stat")
        # An orphan can briefly remain a zombie until the host init reaps it;
        # it must no longer be a running Chromium-equivalent process.
        deadline = time.monotonic() + 1
        while status.exists() and time.monotonic() < deadline:
            try:
                if status.read_text().split(") ", 1)[1].split()[0] == "Z":
                    return
            except FileNotFoundError:
                return
            time.sleep(0.01)
        self.assertFalse(status.exists(), "Renderer child survived timeout cleanup")

    def test_invalid_graph_cannot_inject_styles_or_drop_missing_endpoints(self):
        for mutation in (lambda graph: graph["nodes"][0].update(color="red;click n0 bad"),
                         lambda graph: graph["edges"][0].update(target="missing"),
                         lambda graph: graph["nodes"].append(dict(graph["nodes"][0]))):
            graph = tiny_graph()
            mutation(graph)
            with self.assertRaises(TraceError):
                export_mermaid(graph, self.destination)
            self.assertFalse(self.destination.exists())

    @unittest.skipUnless(os.environ.get("LIQUID_MERMAID_BIN") or shutil.which("mmdc"),
                         "Mermaid CLI is supplied by devenv")
    def test_installed_mermaid_renders_fixture_with_dates_and_hostile_text(self):
        # This executes the actual pinned mmdc + Chromium in `devenv test`.
        graph = self.traced_graph(include_fees=True)
        graph["nodes"].append({"id": "synthetic:hostile", "kind": "address",
                               "label": 'Literal "quotes" | #34; <script>bad</script>\n`text`',
                               "color": COLORS["address"]})
        # Report renderer diagnostics only for this synthetic fixture. Product
        # errors deliberately avoid echoing private investigation labels. The
        # spy preserves the actual timeout and process-group cleanup and never
        # runs the renderer a second time merely to recover its stderr.
        diagnostic_output = []
        real_popen = subprocess.Popen
        fixture_browser_config = None
        if os.environ.get("GITHUB_ACTIONS") == "true":
            # Hosted Ubuntu runners can block Chromium user-namespace
            # sandboxes. This override is confined to synthetic fixture data
            # in CI; production previews and ordinary devenv tests retain the
            # browser's sandbox defaults. See pptr.dev/troubleshooting.
            fixture_browser_config = self.root / "fixture-puppeteer.json"
            save_json(fixture_browser_config, {"args": ["--no-sandbox"]})

        def capture_fixture_renderer(*args, **kwargs):
            if fixture_browser_config:
                command = [*args[0], "--puppeteerConfigFile", str(fixture_browser_config)]
                args = (command, *args[1:])
            process = real_popen(*args, **kwargs)
            communicate = process.communicate

            def capture_communication(*call_args, **call_kwargs):
                output = communicate(*call_args, **call_kwargs)
                diagnostic_output.extend(text for text in output if text)
                return output

            process.communicate = capture_communication
            return process

        with patch("liquid_tracer.mermaid.subprocess.Popen", side_effect=capture_fixture_renderer):
            try:
                result = export_mermaid(graph, self.destination)
            except TraceError as error:
                self.fail(str(error) + "\nSynthetic fixture renderer diagnostics:\n" +
                          "\n".join(diagnostic_output))
        svg = Path(result["svg"]).read_text()
        root = ET.fromstring(svg)
        visible = " ".join(" ".join(root.itertext()).split())
        self.assertIn("2023-11-14 UTC", visible)
        self.assertIn("Literal", visible)
        self.assertIn('Literal "quotes" | #34; <script>bad</script> `text`', visible)
        self.assertIn("FEE", visible)
        self.assertIn(COLORS["starting_transaction"], svg)
        self.assertIn(COLORS["transaction"], svg)
        self.assertFalse(any(element.tag.endswith("}script") or element.tag.endswith("}foreignObject") for element in root.iter()))
        self.assertTrue(Path(result["html"]).is_file())


if __name__ == "__main__":
    unittest.main()
