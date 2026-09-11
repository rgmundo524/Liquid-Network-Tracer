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
from liquid_tracer.mermaid import _render, export_mermaid, mermaid_source
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
        self.assertFalse((self.destination / "graph.html.tmp").exists())

    def test_interrupted_final_preview_write_does_not_publish_completion_name(self):
        write_text = Path.write_text

        def interrupt(path, text, *args, **kwargs):
            if path.name in ("graph.html", "graph.html.tmp"):
                write_text(path, text[:20], *args, **kwargs)
                raise KeyboardInterrupt
            return write_text(path, text, *args, **kwargs)

        with patch.dict(os.environ, {"LIQUID_MERMAID_BIN": "/synthetic/mmdc"}), \
                patch("liquid_tracer.mermaid._render", side_effect=self.fake_renderer), \
                patch.object(Path, "write_text", new=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                export_mermaid(tiny_graph(), self.destination)
        self.assertFalse((self.destination / "graph.html").exists())
        self.assertEqual((self.destination / "graph.html.tmp").stat().st_size, 20)
        self.assertTrue((self.destination / "graph.mmd").is_file())
        self.assertTrue((self.destination / "graph.svg").is_file())

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
        cases = [1, FileNotFoundError("PRIVATE PATH")]
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

    def test_cancel_terminates_renderer_child_processes_without_render_deadline(self):
        child_file = self.root / "child.pid"
        heartbeat = self.root / "child.tick"
        script = (
            "import subprocess,sys; "
            "child=subprocess.Popen([sys.executable,'-c',"
            "'import itertools,os,pathlib,sys,time; p=pathlib.Path(sys.argv[1]); p.write_text(str(os.getpid()))\\n"
            "for tick in itertools.count(): p.with_suffix(\".tick\").write_text(str(tick)); time.sleep(.01)',"
            "sys.argv[1]]); child.wait()"
        )
        real_popen = subprocess.Popen
        processes = []

        def cancellable_renderer(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            processes.append(process)

            wait = process.wait

            def cancel(*call_args, **call_kwargs):
                if call_kwargs.get("timeout") == 5:
                    return wait(*call_args, **call_kwargs)
                self.assertEqual(call_args, ())
                self.assertEqual(call_kwargs, {}, "Rendering must have no application time limit")
                deadline = time.monotonic() + 2
                while not heartbeat.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                raise KeyboardInterrupt

            process.wait = cancel
            return process

        with patch("liquid_tracer.mermaid.subprocess.Popen", side_effect=cancellable_renderer):
            with self.assertRaises(KeyboardInterrupt):
                _render([sys.executable, "-c", script, str(child_file)], self.root)
        self.assertTrue(child_file.is_file(), "Synthetic renderer must have launched its child")
        self.assertIsNotNone(processes[0].returncode, "Renderer parent was not reaped")
        # Check activity directly. Some managed runtimes expose a host /proc
        # with different PIDs; an orphan zombie also need not be reaped yet.
        self.assertTrue(heartbeat.is_file(), "Renderer child must have started its heartbeat")
        tick = heartbeat.read_text()
        time.sleep(.1)
        self.assertEqual(heartbeat.read_text(), tick, "Renderer child survived cancellation cleanup")

    def test_invalid_graph_cannot_inject_styles_or_drop_missing_endpoints(self):
        for mutation in (lambda graph: graph["nodes"][0].update(color="red;click n0 bad"),
                         lambda graph: graph["edges"][0].update(target="missing"),
                         lambda graph: graph["nodes"].append(dict(graph["nodes"][0]))):
            graph = tiny_graph()
            mutation(graph)
            with self.assertRaises(TraceError):
                export_mermaid(graph, self.destination)
            self.assertFalse(self.destination.exists())

    def test_renderer_gives_node_and_browser_the_same_heap_without_protocol_deadline(self):
        script = (
            "import json,os,pathlib,sys; "
            "config=json.loads(pathlib.Path(sys.argv[-1]).read_text()); "
            "pathlib.Path('runtime.json').write_text(json.dumps({'config':config,'env':dict(os.environ)}))"
        )
        with patch.dict(os.environ, {"LIQUID_RENDER_HEAP_MB": "8192",
                                     "NODE_OPTIONS": "--require=private-hook.js",
                                     "MIRO_ACCESS_TOKEN": "SYNTHETIC_PRIVATE_TOKEN",
                                     "BLOCKSTREAM_CLIENT_SECRET": "SYNTHETIC_PRIVATE_SECRET",
                                     "PUPPETEER_EXECUTABLE_PATH": "/synthetic/pinned-chromium"}):
            result = _render([sys.executable, "-c", script], self.root)
        self.assertEqual(result.returncode, 0)
        runtime = json.loads((self.root / "runtime.json").read_text())
        self.assertEqual(runtime["config"], {"protocolTimeout": 0,
                                           "args": ["--js-flags=--max-old-space-size=8192"]})
        self.assertEqual(runtime["env"]["NODE_OPTIONS"], "--max-old-space-size=8192")
        self.assertEqual(runtime["env"]["PUPPETEER_EXECUTABLE_PATH"], "/synthetic/pinned-chromium")
        self.assertNotIn("MIRO_ACCESS_TOKEN", runtime["env"])
        self.assertNotIn("BLOCKSTREAM_CLIENT_SECRET", runtime["env"])
        self.assertNotIn("--no-sandbox", runtime["config"]["args"])

    def test_renderer_retains_only_bounded_private_stderr_without_exposing_labels(self):
        script = (
            "import os,sys; os.write(2,b'x' * 200000); "
            "os.write(2,b'\\nFATAL ERROR: Reached heap limit Allocation failed - JavaScript heap out of memory\\n'"
            "b'SYNTHETIC_PRIVATE_LABEL\\n'); sys.exit(1)"
        )
        with patch.dict(os.environ, {"LIQUID_RENDER_HEAP_MB": "8192"}):
            result = _render([sys.executable, "-c", script], self.root)
        self.assertEqual(result.returncode, 1)
        diagnostic = self.root / "mermaid-renderer.log"
        self.assertEqual(diagnostic.stat().st_mode & 0o777, 0o600)
        self.assertLessEqual(diagnostic.stat().st_size, 64 * 1024)
        self.assertIn("SYNTHETIC_PRIVATE_LABEL", diagnostic.read_text())
        self.assertIn("heap out of memory", result.stderr)
        with patch.dict(os.environ, {"LIQUID_MERMAID_BIN": "/synthetic/mmdc"}), \
                patch("liquid_tracer.mermaid._render", return_value=result):
            with self.assertRaises(TraceError) as caught:
                export_mermaid(tiny_graph(), self.destination)
        self.assertNotIn("SYNTHETIC_PRIVATE_LABEL", str(caught.exception))
        self.assertNotIn("installation", str(caught.exception))
        self.assertIn("8,192", str(caught.exception))

    def test_unwritable_optional_log_does_not_hide_renderer_failure(self):
        script = "import sys; sys.stderr.write('RangeError: Maximum call stack size exceeded'); sys.exit(1)"
        original_open = os.open

        def deny_diagnostic(path, *args, **kwargs):
            if Path(path).name == "mermaid-renderer.log":
                raise PermissionError("synthetic diagnostic denied")
            return original_open(path, *args, **kwargs)

        with patch("liquid_tracer.mermaid.os.open", side_effect=deny_diagnostic):
            result = _render([sys.executable, "-c", script], self.root)
        self.assertEqual(result.returncode, 1)
        self.assertIn("Maximum call stack size exceeded", result.stderr)
        self.assertFalse((self.root / "mermaid-renderer.log").exists())

    @unittest.skipUnless(os.environ.get("LIQUID_MERMAID_BIN") or shutil.which("mmdc"),
                         "Mermaid CLI is supplied by devenv")
    def test_installed_mermaid_renders_fixture_with_dates_and_hostile_text(self):
        # This executes the actual pinned mmdc + Chromium in `devenv test`.
        graph = self.traced_graph(include_fees=True)
        graph["nodes"].append({"id": "synthetic:hostile", "kind": "address",
                               "label": 'Literal "quotes" | #34; <script>bad</script>\n`text`',
                               "color": COLORS["address"]})
        # Only synthetic fixture diagnostics may be echoed by this test.
        # Production failures keep raw labels in the private local log.
        real_popen = subprocess.Popen
        fixture_browser_config = None
        if os.environ.get("GITHUB_ACTIONS") == "true":
            # Hosted Ubuntu runners can block Chromium user-namespace
            # sandboxes. This override is confined to synthetic fixture data
            # in CI; production previews and ordinary devenv tests retain the
            # browser's sandbox defaults. See pptr.dev/troubleshooting.
            fixture_browser_config = self.root / "fixture-puppeteer.json"

        def capture_fixture_renderer(*args, **kwargs):
            if fixture_browser_config:
                command = args[0]
                original = Path(command[command.index("--puppeteerConfigFile") + 1])
                config = json.loads(original.read_text())
                config["args"] = [*config.get("args", []), "--no-sandbox"]
                save_json(fixture_browser_config, config)
                command = [*args[0], "--puppeteerConfigFile", str(fixture_browser_config)]
                args = (command, *args[1:])
            return real_popen(*args, **kwargs)

        with patch("liquid_tracer.mermaid.subprocess.Popen", side_effect=capture_fixture_renderer):
            try:
                result = export_mermaid(graph, self.destination)
            except TraceError as error:
                diagnostic = self.destination / "mermaid-renderer.log"
                self.fail(str(error) + "\nSynthetic fixture renderer diagnostics:\n" +
                          (diagnostic.read_text(errors="replace") if diagnostic.is_file() else "unavailable"))
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
