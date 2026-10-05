import assert from 'node:assert/strict';
import {test} from 'node:test';
import {createTaskNotifications} from '../src/scripts/task-notifications.ts';

const preferenceKey = 'liquid-tracer:system-notifications:v1';
const seenKey = 'liquid-tracer:task-notifications:v1';

function fixture({permission = 'default', answer = 'granted', storage = new Map()} = {}) {
  const sent = [];
  let requested = 0, focused = 0, opened = 0;
  class Notification {
    static permission = permission;
    static async requestPermission() {requested++; Notification.permission = answer; return answer;}
    constructor(title, options) {this.title = title; this.options = options; sent.push(this);}
    close() {this.closed = true;}
  }
  const host = {Notification, focus() {focused++;}, localStorage: {
    getItem(key) {return storage.get(key) ?? null;}, setItem(key, value) {storage.set(key, value);},
  }};
  const manager = createTaskNotifications(host);
  const task = (id = 'job', status = 'succeeded') => ({id, status, investigation: 'Investigation A', action: 'Collect data', open() {opened++;}});
  return {host, manager, sent, task, storage, get requested() {return requested;}, get focused() {return focused;}, get opened() {return opened;}};
}

test('permission is requested only by explicit enable and persists across reloads', async () => {
  const f = fixture();
  assert.equal(f.manager.control().label, 'Notifications off');
  assert.equal(f.requested, 0);
  assert.equal(f.manager.notify(f.task('before-enable')), false);
  assert.equal(f.requested, 0);
  assert.match(await f.manager.toggle(), /enabled for task results/);
  assert.equal(f.requested, 1);
  assert.equal(f.storage.get(preferenceKey), 'on');
  assert.equal(f.manager.control().enabled, true);
  const reloaded = createTaskNotifications(f.host);
  assert.equal(reloaded.control().enabled, true);
  assert.equal(f.requested, 1);
  assert.equal(reloaded.notify(f.task('before-enable')), false);
  assert.match(await reloaded.toggle(), /turned off/);
  assert.equal(f.storage.get(preferenceKey), 'off');
  assert.equal(reloaded.notify(f.task('after-disable')), false);
});

test('each terminal result is shown once across polls, tabs, and reloads; clicking opens its investigation', async () => {
  const f = fixture({permission: 'granted'});
  await f.manager.toggle();
  for (const status of ['succeeded', 'failed', 'canceled']) {
    assert.equal(f.manager.notify(f.task(status, status)), true);
    assert.equal(f.manager.notify(f.task(status, status)), false);
    assert.equal(createTaskNotifications(f.host).notify(f.task(status, status)), false);
  }
  assert.equal(f.sent.length, 3);
  assert.deepEqual(f.sent.map(item => item.title), ['Liquid Tracer · Task finished', 'Liquid Tracer · Task failed', 'Liquid Tracer · Task canceled']);
  assert.equal(f.sent[0].options.tag, 'liquid-tracer-task-succeeded');
  assert.match(f.sent[0].options.body, /Investigation A\nCollect data/);
  f.sent[0].onclick();
  assert.equal(f.sent[0].closed, true);
  assert.equal(f.focused, 1);
  assert.equal(f.opened, 1);
});

test('already granted browser permission still requires opting in to this application', () => {
  const f = fixture({permission: 'granted'});
  assert.equal(f.manager.notify(f.task()), false);
  assert.equal(f.sent.length, 0);
  assert.equal(f.manager.control().enabled, false);
});

test('denied permission explains browser settings without repeatedly prompting', async () => {
  const f = fixture({permission: 'denied'});
  assert.equal(f.manager.control().label, 'Notifications blocked');
  assert.match(await f.manager.toggle(), /browser's site permissions/);
  assert.equal(f.requested, 0);
  assert.equal(f.manager.notify(f.task()), false);
});

test('a dismissed or rejected permission request leaves notifications off', async () => {
  for (const answer of ['default', 'denied']) {
    const f = fixture({answer});
    await f.manager.toggle();
    assert.equal(f.manager.control().enabled, false);
    assert.equal(f.manager.notify(f.task()), false);
  }
  const f = fixture();
  f.host.Notification.requestPermission = async () => {throw new Error('Blocked');};
  assert.match(await f.manager.toggle(), /could not enable/);
  assert.equal(f.manager.control().enabled, false);
});

test('unsupported or insecure browser keeps in-app path available without throwing', async () => {
  for (const host of [{}, {Notification: class {}, isSecureContext: false}]) {
    const manager = createTaskNotifications(host);
    assert.equal(manager.control().disabled, true);
    assert.match(await manager.toggle(), /In-app notifications still appear/);
    assert.equal(manager.notify({id: 'job', status: 'succeeded', investigation: 'A', action: 'Plot', open() {}}), false);
  }
});

test('restricted storage and native notification constructor failures do not fail tasks', async () => {
  const f = fixture({permission: 'granted'});
  Object.defineProperty(f.host, 'localStorage', {get() {throw new Error('Storage disabled');}});
  const manager = createTaskNotifications(f.host);
  await manager.toggle();
  assert.equal(manager.notify(f.task()), true);
  assert.equal(manager.notify(f.task()), false);
  f.host.Notification = class {static permission = 'granted'; constructor() {throw new Error('Unsupported platform');}};
  assert.equal(manager.notify(f.task('broken')), false);
  assert.equal(manager.control().disabled, true);
});

test('remembering existing terminal history is silent and duplicate memory stays bounded', async () => {
  const f = fixture({permission: 'granted', storage: new Map([[seenKey, '{broken json']])});
  await f.manager.toggle();
  f.manager.remember('old-job');
  assert.equal(f.manager.notify(f.task('old-job')), false);
  for (let i = 0; i < 300; i++) f.manager.remember(`history-${i}`);
  assert.equal(JSON.parse(f.storage.get(seenKey)).length, 256);
  assert.equal(f.sent.length, 0);
});
