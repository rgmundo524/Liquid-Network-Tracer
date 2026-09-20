"""Parallel scheduling must not force a completed preview to be recalculated."""

import contextlib
import copy
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from liquid_tracer.cli import layout_preview_run, main, saved_graph, sync_run
from liquid_tracer.common import read_json, save_json
from liquid_tracer.layout_reuse import _complete_search, reusable_elk_preview
from liquid_tracer.layout_search import layout_seeds
from tests.fixtures import A, fixture
from tests.test_elk_layout import synthetic_candidate


class ParallelSearchReuseTests(unittest.TestCase):
    def test_parallel_preview_reused_with_different_worker_setting(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            case, source = root / "case", root / "fixture.json"
            save_json(source, fixture())
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(["trace", "--case", str(case), "--fixture", str(source),
                                       "--seed", A + ":0", "--hops", "1"]), 0)

            def measured_worker(request, seeds, progress=None, **kwargs):
                if progress:
                    progress({"phase": "optimizing", "stage": "memory_measured", "peak_rss_mb": 200,
                              "completed": 0, "total": 0})
                return synthetic_candidate(request, seeds)

            with patch.dict(os.environ, {"LIQUID_ELK_WORKERS": "2", "LIQUID_RENDER_HEAP_MB": "4096"}), \
                    patch("liquid_tracer.render_runtime._available_cpu_count", return_value=8), \
                    patch("liquid_tracer.elk_layout._worker", side_effect=measured_worker):
                product = layout_preview_run(case, layout_attempts=3)
            graph = read_json(product["graph"])
            self.assertEqual(graph["layout"]["search"]["execution"], "parallel")
            self.assertEqual(graph["layout"]["search"]["worker_count"], 2)
            original = saved_graph(case)[2]
            self.assertEqual(reusable_elk_preview(original, case / "previews", layout_attempts=3), graph)
            with patch.dict(os.environ, {"LIQUID_ELK_WORKERS": "1"}), \
                    patch("liquid_tracer.elk_layout.optimize_graph", side_effect=AssertionError("Must reuse")):
                report = sync_run(case, "latest", "SYNTHETIC=", dry_run=True, layout_attempts=3)
            self.assertEqual(report["layout_metrics"], graph["layout"]["metrics"])

    def test_historical_serial_and_completed_parallel_metadata_remain_compatible(self):
        search = {"attempt_count": 3, "attempted_count": 3, "successful_count": 3, "failed_count": 0,
                  "seeds": list(layout_seeds(3)), "failed_attempts": [], "candidate_count": 3,
                  "selected_seed": 1, "execution": "sequential"}
        metrics = copy.deepcopy(search)
        self.assertTrue(_complete_search(search, metrics, 3))
        parallel = {**search, "execution": "parallel", "worker_count": 2, "memory_retry_count": 1,
                    "peak_rss_mb": 300}
        self.assertTrue(_complete_search(parallel, metrics, 3))
        for fields in ({"execution": "unknown"}, {"worker_count": True}, {"worker_count": 4},
                       {"worker_count": 1}, {"memory_retry_count": -1}, {"memory_retry_count": 4},
                       {"memory_retry_count": True}, {"peak_rss_mb": True}, {"peak_rss_mb": 0},
                       {"peak_rss_mb": 2147483648}, {"execution": "sequential"}):
            with self.subTest(fields=fields):
                self.assertFalse(_complete_search({**parallel, **fields}, metrics, 3))


if __name__ == "__main__":
    unittest.main()
