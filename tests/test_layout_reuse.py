"""Completed ELK previews avoid redundant optimization during ordinary sync."""
import contextlib
import copy
import functools
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from liquid_tracer.cli import main, layout_preview_run, saved_graph, sync_run
from liquid_tracer.common import read_json, save_json
from liquid_tracer.layout_reuse import reusable_elk_preview
from liquid_tracer.miro import sync as real_sync
from liquid_tracer.progress import ProgressReporter, public_progress
from tests.fixtures import A, fixture
from tests.test_miro_sync import FakeMiro


class ReuseTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name); self.case = self.root / 'case'
        self.fixture = self.root / 'fixture.json'; save_json(self.fixture, fixture())
        with contextlib.redirect_stdout(io.StringIO()):
            status = main(['trace', '--case', str(self.case), '--fixture', str(self.fixture),
                           '--seed', A + ':0', '--hops', '1'])
        self.assertEqual(status, 0)
        self.product = layout_preview_run(self.case)
        self.path = Path(self.product['directory'])
        self.saved = read_json(self.path / 'graph.json')
        self.original = saved_graph(self.case)[2]

    def reuse(self, graph=None, style='straight'):
        return reusable_elk_preview(graph or self.original, self.case / 'previews', style)

    def test_preview_then_sync_never_launches_elk_and_leaves_archive_unchanged(self):
        before = {str(p): p.read_bytes() for p in self.case.rglob('*') if p.is_file()}
        events = []
        with patch('liquid_tracer.elk_layout.optimize_graph', side_effect=AssertionError('Do not rerun ELK')), \
             patch('liquid_tracer.cli.Esplora', side_effect=AssertionError('Do not retrace')):
            report = sync_run(self.case, 'latest', 'SYNTHETIC=', dry_run=True, progress=events.append)
        self.assertTrue(report['dry_run'])
        self.assertEqual(report['layout_metrics'], self.saved['layout']['metrics'])
        self.assertIn('reusing_layout', [e['phase'] for e in events])
        self.assertIn('building_plan', [e['phase'] for e in events])
        self.assertFalse(any(e['phase'] == 'optimizing' for e in events))
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.case.rglob('*') if p.is_file()})

    def test_reused_plan_reaches_miro_and_repeat_keeps_ids_and_manual_positions(self):
        remote = FakeMiro()
        adapter = functools.partial(real_sync, token='SYNTHETIC-token', transport=remote, interval=0)
        with patch('liquid_tracer.cli.sync', adapter), \
             patch('liquid_tracer.elk_layout.optimize_graph', side_effect=AssertionError('No ELK')):
            first = sync_run(self.case, 'latest', 'SYNTHETIC=')
            ids = set(remote.items)
            selected = next(v for v in remote.items.values() if v.get('type') == 'shape')
            selected['position'].update(x=9000, y=-8000)
            again = sync_run(self.case, 'latest', 'SYNTHETIC=')
        self.assertGreater(first['created'], 0)
        self.assertEqual(again['created'], 0)
        self.assertEqual(ids, set(remote.items))
        self.assertEqual((selected['position']['x'], selected['position']['y']), (9000, -8000))

    def test_all_semantics_match_and_input_order_does_not_matter(self):
        self.assertEqual(self.reuse(), self.saved)
        shuffled = copy.deepcopy(self.original)
        shuffled['nodes'].reverse(); shuffled['edges'].reverse()
        before = copy.deepcopy(shuffled)
        self.assertEqual(self.reuse(shuffled), self.saved)
        self.assertEqual(shuffled, before)
        for change in ('run', 'namespace', 'color', 'label', 'edge', 'view', 'width', 'stop'):
            graph = copy.deepcopy(self.original)
            if change == 'run': graph['run_id'] = '0' * 16
            elif change == 'namespace': graph['namespace']['case_id'] = '0' * 32
            elif change == 'color': graph['nodes'][0]['color'] = '#123456'
            elif change == 'label': graph['nodes'][0]['label'] += '\nChanged'
            elif change == 'edge': graph['edges'].pop()
            elif change == 'view': graph['graph_options']['view'] = 'starter_connections'
            elif change == 'width': graph['nodes'][0]['width'] += 1
            elif change == 'stop': graph['service_controls']['revision'] += 1
            with self.subTest(change=change): self.assertIsNone(self.reuse(graph))
        self.assertIsNone(self.reuse(style='curved'))

    def test_incomplete_symlinked_and_filtered_previews_are_not_reused(self):
        html = self.path / 'graph.html'; text = html.read_bytes(); html.unlink()
        self.assertIsNone(self.reuse())
        other = self.root / 'other.html'; other.write_bytes(text); html.symlink_to(other)
        self.assertIsNone(self.reuse())
        html.unlink(); html.write_bytes(text)
        alternate = self.path.with_name(self.path.name.replace('-elk-', '-connections-'))
        self.path.rename(alternate)
        self.assertIsNone(self.reuse())

    def test_corrupt_geometry_and_stale_report_are_ignored(self):
        altered = copy.deepcopy(self.saved); altered['nodes'][0]['x'] = float('nan')
        save_json(self.path / 'graph.json', altered)
        self.assertIsNone(self.reuse())
        save_json(self.path / 'graph.json', self.saved)
        report = read_json(self.path / 'layout-report.json'); report['node_count'] += 1
        save_json(self.path / 'layout-report.json', report)
        self.assertIsNone(self.reuse())

    def test_pre_label_layout_previews_are_not_reused(self):
        saved = copy.deepcopy(self.saved)
        saved['layout'].pop('edge_labels')
        for edge in saved['edges']:
            edge.pop('label_layout', None)
        report = read_json(self.path / 'layout-report.json')
        report['layout'] = saved['layout']
        save_json(self.path / 'graph.json', saved)
        save_json(self.path / 'layout-report.json', report)
        self.assertIsNone(self.reuse())

    def test_pre_input_order_layout_previews_are_not_reused(self):
        original_report = read_json(self.path / 'layout-report.json')
        for value in (None, {'version': 0}):
            saved = copy.deepcopy(self.saved)
            if value is None:
                saved['layout'].pop('input_order', None)
            else:
                saved['layout']['input_order'] = value
            report = copy.deepcopy(original_report)
            report['layout'] = saved['layout']
            save_json(self.path / 'graph.json', saved)
            save_json(self.path / 'layout-report.json', report)
            with self.subTest(metadata=value):
                self.assertIsNone(self.reuse())

    def test_missing_preview_still_uses_selected_elk_engine(self):
        (self.path / 'graph.html').unlink()
        from liquid_tracer.elk_layout import optimize_graph
        with patch('liquid_tracer.elk_layout.optimize_graph', wraps=optimize_graph) as worker:
            sync_run(self.case, 'latest', 'SYNTHETIC=', dry_run=True)
        worker.assert_called_once()


class StageProgressTests(unittest.TestCase):
    def test_elk_stages_and_counts_survive_without_exposing_raw_messages(self):
        event = dict(phase='optimizing', completed=0, total=0, stage='calculating',
                     node_count=1000, edge_count=2000, heap_mb=4096, elapsed_seconds=31,
                     message='PRIVATE', token='PRIVATE')
        result = public_progress(event)
        self.assertEqual(result['stage'], 'calculating')
        self.assertIn('1,000 objects', result['message'])
        self.assertIn('4,096 MiB', result['message'])
        self.assertNotIn('PRIVATE', json.dumps(result))
        for invalid in (True, -1, 2**53, 'secret', [], float('nan')):
            bad = public_progress({**event, 'stage': invalid, 'node_count': invalid, 'heap_mb': invalid})
            self.assertNotIn('stage', bad)
            self.assertNotIn('node_count', bad)
            self.assertNotIn('heap_mb', bad)

    def test_stage_changes_are_not_hidden_by_throttling(self):
        out = io.StringIO()
        with contextlib.redirect_stderr(out), patch('liquid_tracer.progress.time.monotonic', return_value=1):
            reporter = ProgressReporter()
            for stage in ('preparing', 'calculating', 'applying', 'measuring_output', 'ready'):
                reporter(dict(phase='optimizing', completed=0, total=0, stage=stage))
        self.assertEqual(len(out.getvalue().splitlines()), 5)
        self.assertIn('ELK layout completed', out.getvalue())
