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

    def test_input_order_failure_is_distinguished_from_engine_failure(self):
        engine = MUTATING_ELK.replace("const fixed = node.layoutOptions['elk.portConstraints'] === 'FIXED_ORDER';", "const fixed = false;")
        diagnostic, _ = self.run_worker(engine)
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
