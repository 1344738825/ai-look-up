/**
 * Mock-cordis smoke test for the dsh Host plugin (no runtime dependencies).
 * Run with any Node >= 18:  node test/dsh-smoke.mjs
 */
import assert from 'node:assert/strict';
import { writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
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
  const { emit, makeAgent } = makeHarness({ enabled: false, lessons: false });
  const agent = makeAgent();
  emit('agent/created', agent);
  for (let i = 0; i < 40; i++) emit('tools/result', exec(agent, 'Bash', { command: 'python x.py' }), { isError: true });
  assert.equal(agent.injected.length, 0, 'enabled:false must be silent');
  console.log('PASS  enabled:false stays silent');
}

// 6) user/message resets the turn counters (no nudge right after reset)
{
  const { emit, makeAgent } = makeHarness({ lessons: false });
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
  const failingHarness = makeHarness({ clockTick: false, lessons: false });
  const failing = failingHarness.makeAgent();
  failingHarness.emit('agent/created', failing);
  for (let i = 0; i < 3; i++) {
    failingHarness.emit('tools/result', exec(failing, 'Bash', { command: 'python x.py' }), { isError: true });
  }
  const tickingHarness = makeHarness({ clockTickMinutes: 0, lessons: false });
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
  const { emit, makeAgent } = makeHarness({ clockTickMinutes: 0, lessons: false });
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
  const { emit, makeAgent } = makeHarness({ lessons: false });
  const agent = makeAgent();
  emit('agent/created', agent);
  for (let i = 0; i < 6; i++) emit('tools/result', exec(agent, 'Read', { file_path: 'a.txt' }), { isError: false });
  for (let i = 0; i < 3; i++) emit('tools/result', exec(agent, 'Bash', { command: 'git status' }), { isError: false });
  assert.equal(agent.injected.length, 1, 'no async detour without llm');
  assert(texts(agent).some((t) => t.includes('中途自我审查')), 'static checklist injected');
  assert(!texts(agent).some((t) => t.includes('独立审查')), 'no reviewer verdict without llm');
  console.log('PASS  no llm service means static checklist, no async detour');
}

// 12) fold-back: file content returning to a seen state fires the nudge
{
  const { emit, makeAgent } = makeHarness({});
  const agent = makeAgent();
  emit('agent/created', agent);
  const path = join(tmpdir(), 'lookup-fb-' + Date.now() + '.txt');
  await writeFile(path, 'AAA');
  emit('tools/result', exec(agent, 'Write', { file_path: path }), { isError: false });
  await writeFile(path, 'BBB');
  emit('tools/result', exec(agent, 'Write', { file_path: path }), { isError: false });
  await writeFile(path, 'AAA');
  emit('tools/result', exec(agent, 'Write', { file_path: path }), { isError: false });
  await new Promise((r) => setTimeout(r, 30));
  assert(texts(agent).some((t) => t.includes('原地打转')), 'ping-pong edits must trigger fold-back');
  console.log('PASS  fold-back edit detection fires on ping-pong edits');
}

// 13) goal-drift patrol injects only on drifting/stuck verdicts
{
  const driftCalls = [];
  const fakeLlm = {
    async *stream(options) {
      driftCalls.push(options);
      yield { type: 'text', text: '{"verdict":"drifting","reason":"查禁卡表已从核验手段变成独立目标","suggestion":"收束回卡包分解建议"}' };
      yield { type: 'finish', reason: { kind: 'stop' } };
    },
  };
  const { emit, makeAgent } = makeHarness({ driftCheckCalls: 2, driftCooldownSec: 3600 }, { llm: fakeLlm });
  const agent = makeAgent();
  agent.session.requestHeader = () => ({ config: { provider: 'd', model: 'm' } });
  emit('agent/created', agent);
  emit('session/event', agent.session, { type: 'user/message', data: { content: [{ type: 'text', text: '核验卡包分解建议' }] } });
  for (let i = 0; i < 3; i++) emit('tools/result', exec(agent, 'Read', { file_path: 'a' }), { isError: false });
  await new Promise((r) => setTimeout(r, 20));
  const out = texts(agent);
  assert(out.some((t) => t.includes('目标漂移') && t.includes('喧宾夺主')), 'drift verdict must inject');
  assert.equal(driftCalls.length, 1, 'cooldown must hold within the patrol window');
  assert(driftCalls[0].system.includes('目标漂移审查员'), 'patrol uses the drift-specific system prompt');
  assert(driftCalls[0].messages[0].content[0].text.includes('核验卡包分解建议'), 'patrol material includes the original goal');
  console.log('PASS  goal-drift patrol injects on drifting verdict, silent on cooldown');
}

// 14) lesson ledger: failure loops register pitfalls, new agents get the top ones
{
  const { emit, makeAgent } = makeHarness({});
  const agent = makeAgent();
  emit('agent/created', agent);
  for (let i = 0; i < 3; i++) {
    emit('tools/result', exec(agent, 'Bash', { command: 'python bad_thing.py --flag' }), { isError: true });
  }
  assert(texts(agent).some((t) => t.includes('失败循环')), 'fail-loop must fire first');
  const rookie = makeAgent();
  emit('agent/created', rookie);
  assert(texts(rookie).some((t) => t.includes('已知坑位') && t.includes('bad_thing')),
    'a freshly created agent must receive the registered lesson');
  console.log('PASS  lesson ledger registers pitfalls and briefs new agents');
}

// 15) on-track gating: mechanical kinds keep the full checklist, fuzzy kinds get the short confirm
{
  const fakeLlm = {
    async *stream() {
      yield { type: 'text', text: '{"verdict":"on-track","reason":"未发现异常","suggestion":"继续"}' };
      yield { type: 'finish', reason: { kind: 'stop' } };
    },
  };
  const mech = makeHarness({ lessons: false }, { llm: fakeLlm });
  const mechAgent = mech.makeAgent();
  mechAgent.session.requestHeader = () => ({ config: { provider: 'd', model: 'm' } });
  mech.emit('agent/created', mechAgent);
  for (let i = 0; i < 3; i++) mech.emit('tools/result', exec(mechAgent, 'Bash', { command: 'python x.py' }), { isError: true });
  await new Promise((r) => setTimeout(r, 20));
  const mechOut = texts(mechAgent);
  assert(mechOut.some((t) => t.includes('失败循环') && t.includes('根因')), 'mechanical kind keeps full checklist');
  assert(!mechOut.some((t) => t.includes('仍在正轨')), 'mechanical kind must not be waved through');

  const fuzzy = makeHarness({ lessons: false, callNudgeInterval: 5 }, { llm: fakeLlm });
  const fuzzyAgent = fuzzy.makeAgent();
  fuzzyAgent.session.requestHeader = () => ({ config: { provider: 'd', model: 'm' } });
  fuzzy.emit('agent/created', fuzzyAgent);
  for (let i = 0; i < 5; i++) fuzzy.emit('tools/result', exec(fuzzyAgent, 'Read', { file_path: 'a' }), { isError: false });
  await new Promise((r) => setTimeout(r, 20));
  const fuzzyOut = texts(fuzzyAgent);
  assert(fuzzyOut.some((t) => t.includes('仍在正轨') && t.includes('保持节奏')), 'fuzzy kind gets the short confirm');
  assert(!fuzzyOut.some((t) => t.includes('① 对照')), 'short confirm replaces the checklist');
  console.log('PASS  on-track short confirm only applies to fuzzy triggers');
}

// 16) lessons force re-injection every 5 prompts (mid-session registration reaches the live agent)
{
  const { emit, makeAgent } = makeHarness({});
  const agent = makeAgent();
  emit('agent/created', agent);
  for (let i = 0; i < 3; i++) emit('tools/result', exec(agent, 'Bash', { command: 'python bad_thing.py' }), { isError: true });
  const briefsBefore = texts(agent).filter((t) => t.includes('已知坑位')).length;
  for (let n = 1; n <= 5; n++) emit('session/event', agent.session, { type: 'user/message' });
  const briefsAfter = texts(agent).filter((t) => t.includes('已知坑位')).length;
  assert(briefsAfter > briefsBefore, 'the 5th prompt must force a lessons re-injection');
  console.log('PASS  lessons force re-injection on the 5th prompt');
}

// 17) P0-1 counterexample: distinct greps / branch switches must NOT collapse into repeat-cmds
{
  const { emit, makeAgent } = makeHarness({});
  const agent = makeAgent();
  emit('agent/created', agent);
  for (let i = 0; i < 5; i++) emit('tools/result', exec(agent, 'Read', { file_path: 'a.txt' }), { isError: false });
  const greps = [
    'grep -rn "foo" a.txt', 'grep -rn "bar" b.txt', 'git checkout main',
    'git checkout dev', 'npm run build', 'npm run build2',
  ];
  for (const c of greps) emit('tools/result', exec(agent, 'Bash', { command: c }), { isError: false });
  // 11 次调用零编辑会合法触发 no-output 提醒;这里只断言 repeat 类判定不出现
  assert(!texts(agent).some((t) => t.includes('重复执行') || t.includes('原样执行')),
    'distinct commands must not be judged as grinding');
  console.log('PASS  distinct greps / branch switches stay silent (P0-1)');
}

// 18) P0-1 true positive kept: numbered peek scripts still collapse and trigger
{
  const { emit, makeAgent } = makeHarness({});
  const agent = makeAgent();
  emit('agent/created', agent);
  for (let i = 0; i < 5; i++) emit('tools/result', exec(agent, 'Read', { file_path: 'a.txt' }), { isError: false });
  for (const c of ['python _peek2.py', 'python _peek3.py', 'python _peek4.py']) {
    emit('tools/result', exec(agent, 'Bash', { command: c }), { isError: false });
  }
  assert(texts(agent).some((t) => t.includes('重复执行')), 'numbered probe scripts must still trigger repeat-cmds');
  console.log('PASS  numbered peek scripts still trigger repeat-cmds (P0-1 true positive)');
}

// 19) P0-1 side-effect ruling: different python -c bodies must NOT collapse
{
  const { emit, makeAgent } = makeHarness({});
  const agent = makeAgent();
  emit('agent/created', agent);
  for (let i = 0; i < 5; i++) emit('tools/result', exec(agent, 'Read', { file_path: 'a.txt' }), { isError: false });
  for (const c of [
    "python -c \"print(len(open('a.txt').read()))\"",
    "python -c \"print(len(open('b.txt').read()))\"",
    'python -c "print(sum(range(10)))"',
  ]) emit('tools/result', exec(agent, 'Bash', { command: c }), { isError: false });
  assert(!texts(agent).some((t) => t.includes('重复执行') || t.includes('原样执行')),
    'distinct -c bodies must not be judged as grinding');
  console.log('PASS  distinct python -c bodies stay silent (P0-1 side effect)');
}

console.log('ALL DSH SMOKE TESTS PASSED');
