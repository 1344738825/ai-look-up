/**
 * Mock-cordis smoke test for the dsh Host plugin (no runtime dependencies).
 * Run with any Node >= 18:  node test/dsh-smoke.mjs
 */
import assert from 'node:assert/strict';
import { apply } from '../index.js';

function makeHarness(config, services = {}) {
  const handlers = {};
  const ctx = {
    on(name, fn) { (handlers[name] ??= []).push(fn); },
    logger: { warn: (...a) => console.error('[warn]', ...a) },
    ...services,
  };
  apply(ctx, config);
  const emit = (name, ...args) => {
    for (const fn of handlers[name] ?? []) fn(...args);
  };
  const makeAgent = () => ({
    session: {},
    injected: [],
    inject(message) { this.injected.push(message); },
    ctx: { effect(fn) { return typeof fn === 'function' ? fn() : undefined; } },
  });
  return { emit, makeAgent };
}

const exec = (agent, tool, input, extra = {}) =>
  ({ agent, name: tool, input, signal: { aborted: false }, ...extra });

/** Flatten injected messages to their text, the caller-visible payload. */
const texts = (agent) =>
  agent.injected.map((m) => (Array.isArray(m?.content) ? m.content.map((b) => b.text).join('\n') : String(m)));

// 1) three consecutive failures must fire the failure-loop reminder
{
  const { emit, makeAgent } = makeHarness({});
  const agent = makeAgent();
  emit('agent/created', agent);
  for (let i = 0; i < 3; i++) {
    emit('tools/result', exec(agent, 'Bash', { command: 'python x.py' }), { isError: true });
  }
  assert(texts(agent).some((t) => t.includes('失败循环')), '3 consecutive errors must inject');
  console.log('PASS  consecutive failures inject failure-loop reminder');
}

// 2) a success in between breaks the streak (needs 3 fresh failures again)
{
  const { emit, makeAgent } = makeHarness({});
  const agent = makeAgent();
  emit('agent/created', agent);
  emit('tools/result', exec(agent, 'Bash', { command: 'python x.py' }), { isError: true });
  emit('tools/result', exec(agent, 'Bash', { command: 'python x.py' }), { isError: true });
  emit('tools/result', exec(agent, 'Read', { file_path: 'a.txt' }), { isError: false });
  emit('tools/result', exec(agent, 'Bash', { command: 'python x.py' }), { isError: true });
  emit('tools/result', exec(agent, 'Bash', { command: 'python x.py' }), { isError: true });
  assert(!texts(agent).some((t) => t.includes('失败循环')), 'streak broken by success must not inject');
  emit('tools/result', exec(agent, 'Bash', { command: 'python x.py' }), { isError: true });
  assert(texts(agent).some((t) => t.includes('失败循环')), '3 fresh failures after success must inject');
  console.log('PASS  success breaks the failure streak, fresh streak of 3 injects');
}

// 3) exact-repeat: 3 identical bash commands (after min calls) inject once
{
  const { emit, makeAgent } = makeHarness({});
  const agent = makeAgent();
  emit('agent/created', agent);
  for (let i = 0; i < 6; i++) emit('tools/result', exec(agent, 'Read', { file_path: 'a.txt' }), { isError: false });
  for (let i = 0; i < 3; i++) emit('tools/result', exec(agent, 'Bash', { command: 'git status' }), { isError: false });
  assert(texts(agent).some((t) => t.includes('中途自我审查') && t.includes('原样执行 3 次')), 'exact repeat must inject');
  console.log('PASS  exact-repeat trigger injects');
}

// 4) local clock anchor fires with clockTickMinutes=0
{
  const { emit, makeAgent } = makeHarness({ clockTickMinutes: 0 });
  const agent = makeAgent();
  emit('agent/created', agent);
  emit('tools/result', exec(agent, 'Read', { file_path: 'a.txt' }), { isError: false });
  assert(texts(agent).some((t) => t.includes('本地时钟')), 'clock anchor must inject');
  console.log('PASS  local clock anchor injects');
}

// 5) enabled:false is fully silent
{
  const { emit, makeAgent } = makeHarness({ enabled: false });
  const agent = makeAgent();
  emit('agent/created', agent);
  for (let i = 0; i < 40; i++) emit('tools/result', exec(agent, 'Bash', { command: 'python x.py' }), { isError: true });
  assert.equal(agent.injected.length, 0, 'enabled:false must be silent');
  console.log('PASS  enabled:false stays silent');
}

// 6) user/message resets the turn counters (no nudge right after reset)
{
  const { emit, makeAgent } = makeHarness({});
  const agent = makeAgent();
  agent.session = {};
  emit('agent/created', agent);
  for (let i = 0; i < 25; i++) emit('tools/result', exec(agent, 'Read', { file_path: 'a.txt' }), { isError: false });
  emit('session/event', agent.session, { type: 'user/message' });
  for (let i = 0; i < 25; i++) emit('tools/result', exec(agent, 'Read', { file_path: 'a.txt' }), { isError: false });
  assert.equal(agent.injected.length, 0, 'counters must reset on user message');
  console.log('PASS  user/message resets turn counters');
}

// 7) every injected nudge is a full dsh UserMessage, never a bare string
{
  const failingHarness = makeHarness({ clockTick: false });
  const failing = failingHarness.makeAgent();
  failingHarness.emit('agent/created', failing);
  for (let i = 0; i < 3; i++) {
    failingHarness.emit('tools/result', exec(failing, 'Bash', { command: 'python x.py' }), { isError: true });
  }
  const tickingHarness = makeHarness({ clockTickMinutes: 0 });
  const ticking = tickingHarness.makeAgent();
  tickingHarness.emit('agent/created', ticking);
  tickingHarness.emit('tools/result', exec(ticking, 'Read', { file_path: 'a.txt' }), { isError: false });

  const all = [...failing.injected, ...ticking.injected];
  assert.equal(all.length, 2, 'expected one failure-loop nudge and one clock nudge');
  const ids = new Set();
  for (const m of all) {
    assert.equal(typeof m, 'object', 'injected value must be a message object');
    assert.equal(m.role, 'user', 'injected message role must be user');
    assert.equal(typeof m.id, 'string');
    assert(m.id.length > 0, 'injected message needs a non-empty id');
    assert.equal(m.source.kind, 'plugin:ai-look-up', 'injected message needs a producer-owned source kind');
    assert(Array.isArray(m.content) && m.content[0].type === 'text' && typeof m.content[0].text === 'string',
      'injected message content must be a text block');
    ids.add(m.id);
  }
  assert.equal(ids.size, all.length, 'message ids must be unique');
  console.log('PASS  injected nudges are valid UserMessage objects with unique ids');
}

// 8) inbox fallback path also receives a message object
{
  const { emit, makeAgent } = makeHarness({ clockTickMinutes: 0 });
  const agent = makeAgent();
  delete agent.inject;
  agent.prepended = [];
  agent.inbox = { prepend(target, message) { agent.prepended.push([target, message]); } };
  emit('agent/created', agent);
  emit('tools/result', exec(agent, 'Read', { file_path: 'a.txt' }), { isError: false });
  assert.equal(agent.prepended.length, 1, 'fallback must prepend once');
  const [target, message] = agent.prepended[0];
  assert.equal(target, 'next-step');
  assert.equal(typeof message, 'object', 'fallback must prepend a message object');
  assert.equal(message.role, 'user');
  assert.equal(message.source.kind, 'plugin:ai-look-up');
  console.log('PASS  inbox.prepend fallback receives a message object');
}

// 9) independent reviewer: a working llm service turns the nudge into a verdict
{
  const seen = [];
  const fakeLlm = {
    async *stream(options) {
      seen.push(options);
      yield { type: 'text', text: '{"verdict":"stuck","reason":"同样的命令反复执行且无新信息","suggestion":"换一种方法或先汇报"}' };
      yield { type: 'finish', reason: { kind: 'stop' } };
    },
  };
  const { emit, makeAgent } = makeHarness({}, { llm: fakeLlm });
  const agent = makeAgent();
  agent.session.requestHeader = () => ({ config: { provider: 'deepseek', model: 'deepseek-chat' } });
  emit('agent/created', agent);
  for (let i = 0; i < 6; i++) emit('tools/result', exec(agent, 'Read', { file_path: 'a.txt' }), { isError: false });
  for (let i = 0; i < 3; i++) emit('tools/result', exec(agent, 'Bash', { command: 'git status' }), { isError: false });
  await new Promise((r) => setTimeout(r, 20));
  const out = texts(agent);
  assert(out.some((t) => t.includes('独立审查') && t.includes('空跑确认')), 'verdict must replace the static checklist');
  assert.equal(seen.length, 1, 'exactly one reviewer call');
  assert.equal(seen[0].provider, 'deepseek', 'reviewer follows the session provider');
  assert.equal(seen[0].model, 'deepseek-chat', 'reviewer follows the session model');
  assert.equal(seen[0].temperature, 0, 'reviewer runs at temperature 0');
  assert(seen[0].messages[0].content[0].text.includes('git status'), 'review material includes the recent calls');
  const last = agent.injected.at(-1);
  assert.equal(last.role, 'user', 'verdict is still a proper UserMessage');
  console.log('PASS  independent reviewer injects its verdict through the llm service');
}

// 10) reviewer failure falls back to the static checklist
{
  const fakeLlm = {
    async *stream() {
      yield { type: 'finish', reason: { kind: 'error', failure: { code: 'boom', message: 'provider down' } } };
    },
  };
  const { emit, makeAgent } = makeHarness({}, { llm: fakeLlm });
  const agent = makeAgent();
  agent.session.requestHeader = () => ({ config: { provider: 'deepseek', model: 'deepseek-chat' } });
  emit('agent/created', agent);
  for (let i = 0; i < 6; i++) emit('tools/result', exec(agent, 'Read', { file_path: 'a.txt' }), { isError: false });
  for (let i = 0; i < 3; i++) emit('tools/result', exec(agent, 'Bash', { command: 'git status' }), { isError: false });
  await new Promise((r) => setTimeout(r, 20));
  const out = texts(agent);
  assert(out.some((t) => t.includes('中途自我审查') && t.includes('原样执行 3 次')), 'static checklist must be the fallback');
  assert(!out.some((t) => t.includes('独立审查')), 'no verdict may be injected after a reviewer failure');
  console.log('PASS  reviewer failure falls back to the static checklist');
}

// 11) without the llm service the static checklist is used immediately
{
  const { emit, makeAgent } = makeHarness({});
  const agent = makeAgent();
  emit('agent/created', agent);
  for (let i = 0; i < 6; i++) emit('tools/result', exec(agent, 'Read', { file_path: 'a.txt' }), { isError: false });
  for (let i = 0; i < 3; i++) emit('tools/result', exec(agent, 'Bash', { command: 'git status' }), { isError: false });
  assert.equal(agent.injected.length, 1, 'no async detour without llm');
  assert(texts(agent).some((t) => t.includes('中途自我审查')), 'static checklist injected');
  assert(!texts(agent).some((t) => t.includes('独立审查')), 'no reviewer verdict without llm');
  console.log('PASS  no llm service means static checklist, no async detour');
}

console.log('ALL DSH SMOKE TESTS PASSED');
