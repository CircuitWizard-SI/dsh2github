import test from 'node:test';
import assert from 'node:assert/strict';
import { setTimeout as delay } from 'node:timers/promises';
import { mkdir, mkdtemp, readFile, rm } from 'node:fs/promises';
import { resolve, join } from 'node:path';
import { ApprovalGate } from '../harness-plugin/approval.mjs';
import { apply, authorized } from '../harness-plugin/index.mjs';

function request(session = 'session-a', signal) {
  return { agent: { id: session }, toolName: 'read_file', reason: 'outside workspace', signal };
}

test('exact request allows once and identical receipt reconciles', async () => {
  const gate = new ApprovalGate();
  gate.heartbeat(['session-a']);
  const pending = gate.ask(request(), () => Promise.resolve('unavailable'));
  const row = gate.list()[0];
  const action = { id: row.id, sessionId: row.sessionId, generation: row.generation,
    decision: 'allowed-once', commentId: '1' };
  assert.equal(gate.decide(action).accepted, true);
  assert.deepEqual(gate.decide(action), actionWithReceipt(action));
  assert.equal(await pending, 'allowed-once');
  assert.equal(gate.list().length, 0);
  assert.throws(() => gate.decide({ ...action, decision: 'rejected', commentId: '2' }));
});
function actionWithReceipt(action) { return { ...action, accepted: true }; }

test('deny produces rejected outcome', async () => {
  const gate = new ApprovalGate(); gate.heartbeat(['session-a']);
  const pending = gate.ask(request(), () => Promise.resolve('unavailable'));
  const row = gate.list()[0];
  gate.decide({ ...row, decision: 'rejected', commentId: '1' });
  assert.equal(await pending, 'rejected');
});

test('other session and Host generation cannot decide request', async () => {
  const gate = new ApprovalGate(); gate.heartbeat(['session-a']);
  const pending = gate.ask(request(), () => Promise.resolve('unavailable'));
  const row = gate.list()[0];
  assert.throws(() => gate.decide({ ...row, sessionId: 'other', decision: 'allowed-once', commentId: '1' }));
  assert.throws(() => gate.decide({ ...row, generation: 'old', decision: 'allowed-once', commentId: '1' }));
  assert.throws(() => gate.decide({ ...row, decision: 'allow-always', commentId: '1' }));
  gate.close(); assert.equal(await pending, 'cancelled');
});

test('unenrolled and offline sessions use original local confirmation', async () => {
  const gate = new ApprovalGate();
  assert.equal(await gate.ask(request(), () => Promise.resolve('rejected')), 'rejected');
  gate.heartbeat(['different']);
  assert.equal(await gate.ask(request(), () => Promise.resolve('allowed-once')), 'allowed-once');
  assert.equal(gate.list().length, 0);
});

test('heartbeat loss delegates pending request back to local UI', async () => {
  let now = 0;
  const gate = new ApprovalGate({ now: () => now, leaseMs: 10 });
  gate.heartbeat(['session-a']);
  const pending = gate.ask(request(), () => Promise.resolve('rejected'));
  now = 20;
  await delay(300);
  assert.equal(await pending, 'rejected');
  assert.equal(gate.list().length, 0);
});

test('permission timeout rejects instead of allowing', async () => {
  let now = 0;
  const gate = new ApprovalGate({ now: () => now, leaseMs: 1000, timeoutMs: 10 });
  gate.heartbeat(['session-a']);
  const pending = gate.ask(request(), () => Promise.resolve('allowed-once'));
  now = 20;
  await delay(300);
  assert.equal(await pending, 'rejected');
});

test('task cancellation invalidates request', async () => {
  const gate = new ApprovalGate(); gate.heartbeat(['session-a']);
  const controller = new AbortController();
  const pending = gate.ask(request('session-a', controller.signal), () => Promise.resolve('unavailable'));
  const row = gate.list()[0];
  controller.abort();
  assert.equal(await pending, 'cancelled');
  assert.throws(() => gate.decide({ ...row, decision: 'allowed-once', commentId: '1' }));
});

test('bearer token requires exact constant-time match', () => {
  assert.equal(authorized('Bearer abc', 'abc'), true);
  assert.equal(authorized('Bearer abc-more', 'abc'), false);
  assert.equal(authorized(undefined, 'abc'), false);
});

test('real local HTTP carrier rejects unauthenticated and browser requests, preserves global model', async t => {
  const base = resolve('.test-runtime');
  await mkdir(base, { recursive: true });
  const home = await mkdtemp(join(base, 'host-'));
  const originalHome = process.env.DSH_HOME;
  process.env.DSH_HOME = home;
  const cleanup = [];
  let savedGlobal = 0, selectedSession = 0, promptCount = 0;
  const controller = {
    agents: { selectForNextRequest: () => { selectedSession++; } },
    resolveAgent: async () => ({ agent: { id: 'session-a' } }),
    prompt: async (_request, signal) => { signal.throwIfAborted(); promptCount++; return { accepted: true }; },
    list: async () => ({ items: [{ sessionId: 'session-a' }] })
  };
  const ctx = {
    sessionController: controller,
    sessionQuery: {
      readTitleSnapshots: async () => [{ sessionId: 'session-a', status: 'fulfilled', value: { title: { title: 'Example' } } }],
      readSession: async () => ({ events: [
        { type: 'system/message', seq: 0, time: 1, data: { message: { content: [{ type: 'text', text: 'hidden system' }] } } },
        { type: 'user/message', seq: 1, time: 2, data: { source: { kind: 'user', rpcId: 'request' }, content: [{ type: 'text', text: 'visible input' }] } },
        { type: 'assistant/message', seq: 2, time: 3, data: { message: { content: [
          { type: 'reasoning', text: 'hidden reasoning' }, { type: 'text', text: 'visible reply' }] } } }
      ] })
    },
    agents: new Map(),
    agentDefaultModel: { currentSelection: () => ({ provider: 'local', model: 'old' }), saveSelection: () => { savedGlobal++; } },
    llm: { listModels: async () => [{ id: 'new' }], resolveCallConfig: async x => x },
    on: () => () => {},
    effect: factory => { cleanup.push(factory()); }
  };
  t.after(async () => {
    for (const dispose of cleanup) dispose();
    if (originalHome === undefined) delete process.env.DSH_HOME; else process.env.DSH_HOME = originalHome;
    await rm(home, { recursive: true, force: true });
  });
  await apply(ctx);
  const endpoint = JSON.parse(await readFile(join(home, 'dsh2github/endpoint.json'), 'utf8'));
  const url = 'http://127.0.0.1:' + endpoint.port;
  const headers = { Authorization: 'Bearer ' + endpoint.token, 'Content-Type': 'application/json' };
  assert.equal((await fetch(url + '/health', { method: 'POST', body: '{}' })).status, 403);
  assert.equal((await fetch(url + '/health', { method: 'POST', headers: { ...headers, Origin: 'https://example.invalid' }, body: '{}' })).status, 403);
  const health = await (await fetch(url + '/health', { method: 'POST', headers, body: '{}' })).json();
  assert.equal(health.protocol, 1);
  const sessions = await (await fetch(url + '/sessions', { method: 'POST', headers, body: '{}' })).json();
  assert.equal(sessions.items[0].title, 'Example');
  const snapshot = await (await fetch(url + '/session', { method: 'POST', headers, body: JSON.stringify({ sessionId: 'session-a' }) })).json();
  assert.deepEqual(snapshot.events.map(x => x.text), ['visible input', 'visible reply']);
  assert.equal(JSON.stringify(snapshot).includes('hidden'), false);
  const result = await (await fetch(url + '/prompt', { method: 'POST', headers,
    body: JSON.stringify({ sessionId: 'session-a', requestId: 'request', mode: 'queue', text: 'hello', selection: { provider: 'local', model: 'new' } }) })).json();
  assert.equal(result.accepted, true);
  assert.equal(selectedSession, 1); assert.equal(promptCount, 1); assert.equal(savedGlobal, 0);
});
