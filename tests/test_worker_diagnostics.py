"""Worker failures retain useful classifications without exposing graph data."""

import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from liquid_tracer.render_runtime import renderer_failure, renderer_failure_code
from tests.test_worker_port_candidates import MUTATING_ELK, NODE, ROOT, request_graph


@unittest.skipUnless(NODE, "Node is not installed")
class WorkerDiagnosticTests(unittest.TestCase):
    def run_worker(self, engine, *, request=None, raw=None):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            worker = root / "run.mjs"
            shutil.copyfile(ROOT / "layout" / "run.mjs", worker)
            module = root / "node_modules" / "elkjs" / "lib" / "elk.bundled.js"
            if engine is not None:
                module.parent.mkdir(parents=True)
                module.write_text(engine)
            graph = request_graph()
            graph["branchProfile"] = "flow_weighted"
            result = subprocess.run([NODE, str(worker)],
                                    input=raw if raw is not None else json.dumps(request or {"graph": graph, "seeds": [19]}),
                                    text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertNotIn("PRIVATE", result.stderr)
        self.assertTrue(result.stderr.startswith("LIQUID_ELK_FAILURE "))
        diagnostic = json.loads(result.stderr.removeprefix("LIQUID_ELK_FAILURE "))
        self.assertEqual(renderer_failure_code(result.stderr, 1), diagnostic["code"])
        return diagnostic, result.stderr

    def test_known_engine_failures_keep_fixed_classification_and_seed_context(self):
        examples = {
            "java.lang.IllegalArgumentException": "elk_illegal_argument",
            "java.lang.IllegalStateException": "elk_illegal_state",
            "java.lang.NullPointerException": "elk_null_pointer",
            "java.lang.ArrayIndexOutOfBoundsException": "elk_index_error",
            "java.lang.AssertionError": "elk_assertion",
            "org.eclipse.elk.core.UnsupportedGraphException": "elk_unsupported_graph",
            "org.eclipse.elk.core.UnsupportedConfigurationException": "elk_unsupported_configuration",
            "TypeError": "elk_type_error",
            "ReferenceError": "elk_reference_error",
            "java.lang.StackOverflowError": "stack_limit",
            "JavaScript heap out of memory": "heap_exhausted",
            "cannot allocate memory": "memory_exhausted",
            "unexpected worker error": "elk_engine_error",
        }
        for exception, code in examples.items():
            with self.subTest(exception=exception):
                message = json.dumps(exception + ": PRIVATE-ID /PRIVATE/path token=PRIVATE")
                diagnostic, stderr = self.run_worker(
                    "module.exports = class ELK {async layout() {throw new Error(" + message + ");}};")
                self.assertEqual(diagnostic, {"version": 1, "stage": "geometry_layout", "seed": 19,
                                             "branch_profile": "flow_weighted", "input_order_policy": "geometry",
                                             "code": code})
                self.assertNotIn("PRIVATE", renderer_failure(stderr, 1, "ELK", 26902))

    def test_failure_of_attachment_rerun_identifies_the_second_layout(self):
        engine = MUTATING_ELK.replace("const stamp =", "if (this.calls === 1) throw new TypeError('PRIVATE');\nconst stamp =")
        diagnostic, stderr = self.run_worker(engine)
        self.assertEqual(diagnostic["code"], "elk_type_error")
        self.assertEqual(diagnostic["stage"], "traced_first_layout")
        self.assertEqual(diagnostic["input_order_policy"], "traced_first")
        self.assertIn("calculating the traced-first layout", renderer_failure(stderr, 1, "ELK", 26902))

    def test_invalid_request_and_profile_never_echo_input(self):
        diagnostic, _ = self.run_worker(MUTATING_ELK, raw='{PRIVATE: "PRIVATE"}')
        self.assertEqual(diagnostic, {"version": 1, "stage": "read_request", "code": "elk_invalid_request"})
        graph = request_graph()
        graph["branchProfile"] = "PRIVATE"
        diagnostic, _ = self.run_worker(MUTATING_ELK, request={"graph": graph, "seeds": [19]})
        self.assertEqual(diagnostic, {"version": 1, "stage": "validate_request", "code": "elk_invalid_request"})

    def test_malformed_ordered_rerun_remains_fatal(self):
        for corruption in (
            "graph.children = [];",
            "node.ports = node.ports.filter(port => port.id !== 'w1');",
            "node.ports.find(port => port.id === 'w1').y = NaN;",
            "node.ports.find(port => port.id === 'w1').y = Infinity;",
            "node.ports.find(port => port.id === 'w1').id = 'w0';",
        ):
            with self.subTest(corruption=corruption):
                engine = MUTATING_ELK.replace("return graph;", "if (fixed) {" + corruption + "}\nreturn graph;")
                diagnostic, _ = self.run_worker(engine)
                self.assertEqual(diagnostic["code"], "elk_input_order")
                self.assertEqual(diagnostic["stage"], "validate_input_order")

    def test_west_order_mismatch_cannot_mask_malformed_east_ports(self):
        engine = MUTATING_ELK.replace(
            "return graph;",
            "if (fixed) {node.ports.filter(port => port.id.startsWith('w'))"
            ".forEach(port => {port.y = 50;});"
            "node.ports.find(port => port.id === 'e0').y = NaN;}\nreturn graph;")
        diagnostic, _ = self.run_worker(engine)
        self.assertEqual(diagnostic["code"], "elk_input_order")
        self.assertEqual(diagnostic["stage"], "validate_input_order")

    def test_order_mismatch_cannot_hide_unserializable_rerun_edges(self):
        engine = MUTATING_ELK.replace(
            "return graph;",
            "if (fixed) {node.ports.filter(port => port.id.startsWith('w'))"
            ".forEach(port => {port.y = 50;});graph.edges = null;}\nreturn graph;")
        diagnostic, _ = self.run_worker(engine)
        self.assertEqual(diagnostic["code"], "elk_invalid_output")
        self.assertEqual(diagnostic["stage"], "validate_input_order")

    def test_order_mismatch_cannot_mask_missing_later_constrained_node(self):
        graph = request_graph()
        graph["children"].append({
            "id": "later_transaction", "x": 500, "y": 0, "width": 160, "height": 160,
            "layoutOptions": {},
            "ports": [{"id": "later_w0", "x": 0, "y": 20,
                       "layoutOptions": {"elk.port.side": "WEST"}},
                      {"id": "later_w1", "x": 0, "y": 40,
                       "layoutOptions": {"elk.port.side": "WEST"}}],
        })
        graph["inputPortOrders"]["later_transaction"] = ["later_w0", "later_w1"]
        engine = MUTATING_ELK.replace(
            "return graph;",
            "if (fixed) {node.ports.filter(port => port.id.startsWith('w'))"
            ".forEach(port => {port.y = 50;});graph.children.pop();}\nreturn graph;")
        diagnostic, _ = self.run_worker(engine, request={"graph": graph, "seeds": [19]})
        self.assertEqual(diagnostic["code"], "elk_input_order")
        self.assertEqual(diagnostic["stage"], "validate_input_order")

    def test_malformed_engine_output_remains_fatal_adapter_failure(self):
        for returned in ("null", "{children:null,edges:[]}", "{children:[],edges:null}"):
            with self.subTest(returned=returned):
                diagnostic, stderr = self.run_worker(
                    "module.exports = class ELK {async layout() {return " + returned + ";}};")
                self.assertEqual(diagnostic["code"], "elk_invalid_output")
                self.assertEqual(diagnostic["stage"], "order_constraints")
                self.assertIn("could not validate or encode", renderer_failure(stderr, 1, "ELK", 26902))

    def test_malformed_rerun_output_remains_fatal(self):
        engine = MUTATING_ELK.replace("const stamp =", "if (this.calls === 1) return null;\nconst stamp =")
        diagnostic, _ = self.run_worker(engine)
        self.assertEqual(diagnostic["code"], "elk_invalid_output")
        self.assertEqual(diagnostic["stage"], "validate_input_order")

    def test_missing_engine_invalid_export_and_constructor_failure_are_setup_errors(self):
        for engine in (None, "module.exports = {};", "module.exports = class ELK {};", "module.exports = PRIVATE syntax error;",
                       "module.exports = class ELK {constructor() {throw new Error('PRIVATE');}};"):
            with self.subTest(engine=engine):
                diagnostic, stderr = self.run_worker(engine)
                self.assertEqual(diagnostic, {"version": 1, "stage": "load_engine", "code": "elk_worker_setup"})
                self.assertIn("could not load or initialize", renderer_failure(stderr, 1, "ELK", 26902))


if __name__ == "__main__":
    unittest.main()
