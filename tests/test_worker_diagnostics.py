"""Worker failures retain useful classifications without exposing graph data."""

import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from liquid_tracer.render_runtime import renderer_failure, renderer_failure_code
from tests.test_worker_port_candidates import MUTATING_ELK, NODE, ROOT, install_fake_engine, request_graph


@unittest.skipUnless(NODE, "Node is not installed")
class WorkerDiagnosticTests(unittest.TestCase):
    def run_worker(self, engine, *, request=None, raw=None, production_loader=False):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            worker = root / "run.mjs"
            shutil.copyfile(ROOT / "layout" / "run.mjs", worker)
            if engine is not None:
                install_fake_engine(root, engine)
            if production_loader:
                shutil.copyfile(ROOT / "layout" / "elk-stack-safe.cjs", root / "elk-stack-safe.cjs")
            graph = request_graph()
            graph["branchProfile"] = "flow_weighted"
            result = subprocess.run([NODE, str(worker)],
                                    input=raw if raw is not None else json.dumps(request or {"graph": graph, "seeds": [19]}),
                                    text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertTrue(result.stderr.startswith("LIQUID_ELK_FAILURE "))
        lines = result.stderr.splitlines()
        self.assertEqual(len(lines), 2)
        self.assertNotIn("PRIVATE", lines[0])
        diagnostic = json.loads(lines[0].removeprefix("LIQUID_ELK_FAILURE "))
        self.assertTrue(lines[1].startswith("LIQUID_ELK_TRACE "))
        self.trace = json.loads(lines[1].removeprefix("LIQUID_ELK_TRACE "))
        self.assertLessEqual(len(lines[1].encode()), 64 * 1024 + len("LIQUID_ELK_TRACE "))
        self.assertEqual(self.trace["code"], diagnostic["code"])
        self.assertEqual(self.trace["stage"], diagnostic["stage"])
        self.assertNotIn("PRIVATE", renderer_failure(result.stderr, 1, "ELK", 26902))
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

    def test_native_stack_overflow_retains_function_frames_and_runtime_details(self):
        diagnostic, _ = self.run_worker("""
module.exports = class ELK {
  async layout() { function recursiveLayout() { return recursiveLayout(); } recursiveLayout(); }
};
""")
        self.assertEqual(diagnostic["code"], "stack_limit")
        trace = self.trace
        self.assertEqual(trace["engine_version"], "0.12.0")
        self.assertEqual(trace["engine_build"], "test_stub")
        self.assertEqual(trace["seed"], 19)
        self.assertEqual(trace["input_order_policy"], "geometry")
        self.assertEqual(set(trace["runtime"]), {"node", "v8", "platform", "arch"})
        self.assertTrue(all(trace["runtime"].values()))
        self.assertIn("recursiveLayout", trace["errors"][0]["stack"])
        self.assertEqual(trace["errors"][0]["name"], "RangeError")
        self.assertLessEqual(len(trace["errors"][0]["stack"].splitlines()), 65)

    def test_nested_causes_have_bounded_fields_and_encoded_size(self):
        self.run_worker(r"""
module.exports = class ELK {
  async layout() {
    let error = new Error('root');
    for (let index = 0; index < 9; ++index) {
      error = new Error('\x00'.repeat(5000), {cause: error});
      error.name = 'N'.repeat(500);
      error.stack = '\x00'.repeat(20000);
    }
    throw error;
  }
};
""")
        self.assertEqual(len(self.trace["errors"]), 4)
        self.assertTrue(self.trace["truncated"])
        for error in self.trace["errors"]:
            self.assertEqual(len(error["name"]), 128)
            self.assertEqual(len(error["message"]), 1024)
            self.assertLessEqual(len(error["stack"]), 8192)

    def test_circular_cause_and_throwing_cause_getter_cannot_mask_original_failure(self):
        for cause in ("error.cause = error;",
                      "Object.defineProperty(error, 'cause', {get() {throw new Error('SECONDARY');}});"):
            with self.subTest(cause=cause):
                self.run_worker("module.exports = class ELK { async layout() {"
                                "const error = new TypeError('PRIVATE');" + cause + "throw error;}};")
                self.assertEqual(self.trace["code"], "elk_type_error")
                self.assertTrue(self.trace["truncated"])
                self.assertEqual(len(self.trace["errors"]), 1)
                self.assertEqual(self.trace["errors"][0]["message"], "PRIVATE")
                self.assertNotIn("SECONDARY", json.dumps(self.trace))

    def test_nested_error_stacks_are_retained_without_reclassifying_safe_summary(self):
        diagnostic, _ = self.run_worker("module.exports = class ELK {async layout() {"
                                        "throw new Error('wrapper', {cause: new RangeError("
                                        "'Maximum call stack size exceeded')});}};")
        self.assertEqual(diagnostic["code"], "elk_engine_error")
        self.assertEqual(len(self.trace["errors"]), 2)
        self.assertEqual(self.trace["errors"][1]["name"], "RangeError")
        self.assertIn("Maximum call stack size exceeded", self.trace["errors"][1]["stack"])

    def test_trace_does_not_serialize_graph_or_arbitrary_exception_fields(self):
        self.run_worker("module.exports = class ELK { async layout(graph) {"
                        "const error = new Error('synthetic'); error.graph = graph;"
                        "error.secret = 'NOT_IN_TRACE'; throw error;}};")
        encoded = json.dumps(self.trace)
        self.assertNotIn("NOT_IN_TRACE", encoded)
        self.assertNotIn('"children"', encoded)
        self.assertNotIn("private caption", encoded)

    def test_unicode_line_separators_remain_inside_one_structured_record(self):
        self.run_worker(r"module.exports = class ELK {async layout() {"
                        r"throw new Error('first\u0085second\u2028third\u2029last');}};")
        self.assertEqual(self.trace["errors"][0]["message"], "first\u0085second\u2028third\u2029last")

    def test_setup_and_request_errors_still_have_stacks_before_engine_load(self):
        for kwargs in ({"raw": '{PRIVATE: "PRIVATE"}'}, {}):
            with self.subTest(kwargs=kwargs):
                self.run_worker(None, **kwargs)
                self.assertIsNone(self.trace["engine_version"])
                self.assertTrue(self.trace["errors"][0]["stack"])

    def test_production_loader_rejects_unrecognized_source_as_setup_failure(self):
        diagnostic, _ = self.run_worker(MUTATING_ELK, production_loader=True)
        self.assertEqual(diagnostic, {"version": 1, "stage": "load_engine", "code": "elk_worker_setup"})
        self.assertEqual(self.trace["engine_version"], "0.12.0")
        self.assertEqual(self.trace["engine_build"], "non_minified")
        self.assertTrue(self.trace["errors"][0]["stack"])

    @unittest.skipUnless((ROOT / "layout/node_modules/elkjs/lib/elk-worker.js").is_file(),
                         "local ELK package is not installed")
    def test_real_engine_failure_contains_readable_function_names(self):
        request = {"graph": {"id": "synthetic", "layoutOptions": {"elk.algorithm": "layered"},
                             "children": [{"id": "a", "width": 10, "height": 10}],
                             "edges": [{"id": "e", "sources": ["a"], "targets": ["missing"]}]},
                   "seeds": [1]}
        result = subprocess.run([NODE, str(ROOT / "layout/run.mjs")], input=json.dumps(request),
                                text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 1)
        line = next(line for line in result.stderr.splitlines() if line.startswith("LIQUID_ELK_TRACE "))
        trace = json.loads(line.removeprefix("LIQUID_ELK_TRACE "))
        self.assertEqual(trace["engine_version"], "0.12.0")
        self.assertEqual(trace["engine_build"], "non_minified_iterative_network_simplex_v1")
        self.assertIn("$shapeById", trace["errors"][0]["stack"])
        self.assertIn("elk-worker.js", trace["errors"][0]["stack"])


if __name__ == "__main__":
    unittest.main()
