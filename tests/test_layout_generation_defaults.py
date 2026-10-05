"""CLI generation resolves Trace while archived layouts retain their settings."""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from liquid_tracer.cli import layout_appearance, layout_preview_run, parser, saved_graph, sync_run
from liquid_tracer.common import TraceError, read_json, save_json
from liquid_tracer.investigations import create_investigation, read_case
from tests.test_connections import saved_case


class LayoutGenerationDefaultsTests(unittest.TestCase):
    def test_resolver_preserves_explicit_standard_and_rejects_invalid_values(self):
        self.assertEqual(layout_appearance({}), 'trace')
        self.assertEqual(layout_appearance({'run_defaults': {'layout_style':'standard'}}), 'standard')
        self.assertEqual(layout_appearance({'run_defaults': {'layout_style':'standard'}}, 'trace'), 'trace')
        for invalid in ('default', True, 42):
            with self.subTest(invalid=invalid), self.assertRaises(TraceError):
                layout_appearance({}, invalid)
        self.assertIsNone(parser().parse_args(['layout-preview','--case','x']).layout_style)
        self.assertEqual(parser().parse_args(['layout-preview','--case','x','--layout-style','standard']).layout_style,
                         'standard')

    def test_new_cli_geometry_uses_current_style_without_changing_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            case = create_investigation(Path(temporary), 'Style check')
            _, archive = saved_case(case)
            source = {p.name:p.read_bytes() for p in archive.iterdir() if p.is_file()}
            metadata = read_case(case)
            for stored, expected in ((None,'trace'), ('standard','standard'), ('trace','trace')):
                metadata['run_defaults'] = {} if stored is None else {'layout_style':stored}
                save_json(case/'case.json', metadata)
                case_bytes = (case/'case.json').read_bytes()
                self.assertEqual(saved_graph(case)[2]['graph_options']['layout_style'], expected)
                captured = []
                def arrange(graph, **kwargs):
                    captured.append(copy.deepcopy(graph))
                    graph['layout'] = {'algorithm':'test-only'}
                    return graph
                with patch('liquid_tracer.elk_layout.optimize_graph', side_effect=arrange), \
                        patch('liquid_tracer.cli.ensure_graph_counts', return_value={}), \
                        patch('liquid_tracer.layout_preview.export_layout', return_value={'html':'not-opened'}):
                    layout_preview_run(case)
                    layout_preview_run(case, layout_style='standard')
                self.assertEqual([g['graph_options']['layout_style'] for g in captured], [expected, 'standard'])
                with patch('liquid_tracer.cli.refresh_presentation', return_value=read_json(archive/'miro-plan.json')) as refresh, \
                        patch('liquid_tracer.cli.sync', return_value={}):
                    sync_run(case, 'latest', 'SYNTHETIC=', dry_run=True)
                self.assertEqual(refresh.call_args.kwargs['layout_style'], expected)
                self.assertEqual((case/'case.json').read_bytes(), case_bytes)
                self.assertEqual({p.name:p.read_bytes() for p in archive.iterdir() if p.is_file()}, source)


if __name__ == '__main__':
    unittest.main()
