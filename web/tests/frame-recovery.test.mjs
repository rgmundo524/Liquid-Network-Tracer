import assert from 'node:assert/strict';
import {beforeEach, test} from 'node:test';
import {
  resetFrameRecovery, beginFrameRecovery, frameRecoveryStarted, frameRecoveryComplete,
  frameRecoveryApproval, frameRecoveryDialog,
} from '../src/scripts/frame-recovery.ts';

const board = 'https://miro.com/app/board/test-board/';
const frame = {title: 'Activity <script>unsafe</script> "one"', x: 10, y: 20, width: 500, height: 400};
const report = (extra = {}) => ({schema_version: 1, recovery: 'pending_frame_review',
  review_id: 'a'.repeat(64), run_id: 'interrupted-run', pending_frame: {...frame},
  candidates: [{...frame, id: 'frame-1'}], potential_match_count: 0, can_confirm_absent: false, ...extra});
const ready = result => {
  const version = beginFrameRecovery('case A');
  frameRecoveryStarted('case A', version, 'job-1');
  assert.equal(frameRecoveryComplete('case A', 'job-1', result), true);
};
beforeEach(resetFrameRecovery);

test('frame adoption requires an explicit existing candidate and is consumed once', () => {
  ready(report());
  const html = frameRecoveryDialog('case A', board);
  assert.match(html, /name="frame_item_id" value="frame-1" required/);
  assert.doesNotMatch(html, /<input[^>]* checked|<script>/);
  assert.match(html, /&lt;script&gt;unsafe&lt;\/script&gt;/);
  assert.match(html, /graph objects already created have saved IDs/);
  assert.throws(() => frameRecoveryApproval('case A', '', false), /Select the existing frame/);
  assert.throws(() => frameRecoveryApproval('case A', 'unreviewed-frame', false), /Select the existing frame/);
  assert.throws(() => frameRecoveryApproval('case A', 'frame-1', true), /Select the existing frame/);
  assert.deepEqual(frameRecoveryApproval('case A', 'frame-1', false), {review_id: 'a'.repeat(64), item_id: 'frame-1'});
  assert.throws(() => frameRecoveryApproval('case A', 'frame-1', false), /Review the interrupted frame again/);
});

test('zero matches requires explicit board inspection before absence can be confirmed', () => {
  ready(report({candidates: [], can_confirm_absent: true}));
  const html = frameRecoveryDialog('case A', board);
  assert.match(html, /does not prove the creation failed/);
  assert.match(html, /name="confirm_frame_absent" required/);
  assert.doesNotMatch(html, /<input[^>]* checked|name="frame_item_id"/);
  assert.throws(() => frameRecoveryApproval('case A', '', false), /Inspect the linked board/);
  assert.deepEqual(frameRecoveryApproval('case A', '', true), {review_id: 'a'.repeat(64), confirm_absent: true});
});

test('possible manually changed frames block absence confirmation', () => {
  ready(report({candidates: [], potential_match_count: 2}));
  const html = frameRecoveryDialog('case A', board);
  assert.match(html, /2 frames match only the expected title or position and size/);
  assert.match(html, /Restore its expected title and layout/);
  assert.doesNotMatch(html, /type="submit"|name="confirm_frame_absent"/);
  assert.throws(() => frameRecoveryApproval('case A', '', true), /Inspect the linked board/);
});

test('a new review discards old job completions and page reset discards approval', () => {
  const old = beginFrameRecovery('case A');
  frameRecoveryStarted('case A', old, 'old-job');
  const current = beginFrameRecovery('case A');
  frameRecoveryStarted('case A', old, 'late-old-job');
  frameRecoveryStarted('case A', current, 'current-job');
  assert.equal(frameRecoveryComplete('case A', 'old-job', report()), false);
  assert.equal(frameRecoveryComplete('case A', 'late-old-job', report()), false);
  assert.equal(frameRecoveryComplete('case B', 'current-job', report()), false);
  assert.equal(frameRecoveryComplete('case A', 'current-job', report()), true);
  assert.equal(frameRecoveryDialog('case B', board), '');
  assert.throws(() => frameRecoveryApproval('case B', 'frame-1', false), /Review the interrupted frame again/);
  resetFrameRecovery();
  assert.equal(frameRecoveryDialog('case A', board), '');
  assert.throws(() => frameRecoveryApproval('case A', 'frame-1', false), /Review the interrupted frame again/);
  assert.equal(frameRecoveryComplete('case A', 'current-job', report()), false);
});

test('malformed or conflicting review responses never enable recovery', () => {
  for (const invalid of [
    {review_id: 'invalid'}, {pending_frame: {...frame, width: 0}},
    {candidates: [{...frame, id: '"><script>bad</script>'}]},
    {candidates: [{...frame, id: 'same'}, {...frame, id: 'same'}]},
    {can_confirm_absent: true}, {candidates: [], potential_match_count: 1, can_confirm_absent: true},
  ]) {
    const version = beginFrameRecovery('case A');
    frameRecoveryStarted('case A', version, 'job-1');
    assert.throws(() => frameRecoveryComplete('case A', 'job-1', report(invalid)), /review is incomplete/);
    assert.equal(frameRecoveryDialog('case A', board), '');
  }
});

test('dialog uses the linked Miro board and rejects other origins and embedded credentials', () => {
  ready(report({board_url: 'javascript:alert(1)'}));
  assert.match(frameRecoveryDialog('case A', board), /href="https:\/\/miro.com\/app\/board\/test-board\/"/);
  assert.equal(frameRecoveryDialog('case A', 'javascript:alert(1)'), '');
  assert.equal(frameRecoveryDialog('case A', 'https://miro.com.attacker.test/app/board/test/'), '');
  assert.equal(frameRecoveryDialog('case A', 'https://user:password@miro.com/app/board/test/'), '');
});
