/**
 * Mock-cordis smoke test for the dsh Host plugin (no runtime dependencies).
 * Run with any Node >= 18:  node test/dsh-smoke.mjs
 */
import assert from 'node:assert/strict';
import { apply } from '../index.js';

function makeHarness(config) {
  const handlers = {};
  const ctx = {
    on(name, fn) { (handlers[name] ??= []).push(fn); },
    logger: { warn: (...a) => console.error('[warn]', ...a) },
  };
  apply(ctx, config);
  const emit = (name, ...args) => {
    for (const fn of handlers[name] ?? []) fn(...args);
  };
  const makeAgent = () => ({
    session: {},
    injected: [],
    inject(text) { this.injected.push(text); },
    ctx: { effect(fn) { return typeof fn === 'function' ? fn() : undefined; } },
  });
  return { emit, makeAgent };
}

const exec = (agent, tool, input, extra = {}) =>
  ({ agent, name: tool, input, signal: { aborted: false }, ...extra });

// 1) three consecutive failures must fire the failure-loop reminder
{
  const { emit, makeAgent } = makeHarness({});
  const agent = makeAgent();
  emit('agent/created', agent);
  for (let i = 0; i < 3; i++) {
    emit('tools/result', exec(agent, 'Bash', { command: 'python x.py' }), { isError: true });
  }
  assert(agent.injected.some((t) => t.includes('失败循环')), '3 consecutive errors must inject');
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
  assert(!agent.injected.some((t) => t.includes('失败循环')), 'streak broken by success must not inject');
  emit('tools/result', exec(agent, 'Bash', { command: 'python x.py' }), { isError: true });
  assert(agent.injected.some((t) => t.includes('失败循环')), '3 fresh failures after success must inject');
  console.log('PASS  success breaks the failure streak, fresh streak of 3 injects');
}

// 3) exact-repeat: 3 identical bash commands (after min calls) inject once
{
  const { emit, makeAgent } = makeHarness({});
  const agent = makeAgent();
  emit('agent/created', agent);
  for (let i = 0; i < 6; i++) emit('tools/result', exec(agent, 'Read', { file_path: 'a.txt' }), { isError: false });
  for (let i = 0; i < 3; i++) emit('tools/result', exec(agent, 'Bash', { command: 'git status' }), { isError: false });
  assert(agent.injected.some((t) => t.includes('中途自我审查') && t.includes('原样执行 3 次')), 'exact repeat must inject');
  console.log('PASS  exact-repeat trigger injects');
}

// 4) local clock anchor fires with clockTickMinutes=0
{
  const { emit, makeAgent } = makeHarness({ clockTickMinutes: 0 });
  const agent = makeAgent();
  emit('agent/created', agent);
  emit('tools/result', exec(agent, 'Read', { file_path: 'a.txt' }), { isError: false });
  assert(agent.injected.some((t) => t.includes('本地时钟')), 'clock anchor must inject');
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

console.log('ALL DSH SMOKE TESTS PASSED');
