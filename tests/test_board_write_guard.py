"""Live publishers share deletion authority across case-backed mappings."""
import unittest
from unittest.mock import Mock

from liquid_tracer.board_deletion import board_write_lock, delete_board
from liquid_tracer.common import TraceError
from liquid_tracer.investigations import create_investigation
from liquid_tracer.miro import make_plan, publish, sync, sync_frames
from tests import test_board_deletion
from tests.test_miro_sync import FakeMiro, graph
from tests.test_miro_frame_sync import framed_graph


class BoardWriteGuardTests(unittest.TestCase):
    setUp = test_board_deletion.DeleteBoardTests.setUp

    def test_deleted_target_is_blocked_by_all_publishers_even_from_another_case(self):
        other = create_investigation(self.root, 'Other case')
        state = other / 'miro' / 'separate-state.json'
        plan = make_plan(framed_graph())
        sync(plan, self.board['board_id'], state,
             token='test', transport=FakeMiro(), interval=0)
        delete_board(self.case, self.board['id'], self.board['board_id'], transport=self.remote)
        transport = Mock(side_effect=AssertionError('Deleted boards must receive no requests'))
        for publisher in (sync, sync_frames, publish):
            with self.subTest(publisher=publisher.__name__), self.assertRaisesRegex(TraceError, 'deleted'):
                publisher(plan, self.board['board_id'], state,
                          token='test', transport=transport, interval=0)
        transport.assert_not_called()

    def test_inflight_other_case_board_lock_blocks_sync_before_remote_access(self):
        other = create_investigation(self.root, 'Other case')
        state = other / 'miro' / 'separate-state.json'
        transport = Mock(side_effect=AssertionError('Busy boards must receive no requests'))
        with board_write_lock(self.case, self.board['board_id']):
            with self.assertRaisesRegex(TraceError, 'busy'):
                sync(make_plan(graph()), self.board['board_id'], state,
                     token='test', transport=transport, interval=0)
        transport.assert_not_called()


if __name__ == '__main__':
    unittest.main()
