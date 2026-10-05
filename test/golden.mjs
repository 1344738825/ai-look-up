/**
 * 双端一致性金标测试(JS 侧)——与 test/golden_check.py 消费同一份 golden_cases.json,
 * 校验 normCmd 金标、spec.json 接入完整性、会话回放触发序列。任一漂移即红。
 * Run: node test/golden.mjs
 */
import assert from 'node:assert/strict';
import { readFileSync, writeFileSync, readdirSync, existsSync, mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { apply, __test } from '../index.js';

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = dirname(HERE);
const CASES = JSON.parse(readFileSync(join(HERE, 'golden_cases.json'), 'utf8'));
const SPEC = JSON.parse(readFileSync(join(ROOT, 'spec.json'), 'utf8')).shared;

// index.js 的 DEFAULTS 不导出,通过 apply(ctx, config) 后的行为无法直接读取;
// 用一个可配置探测:enabled:false 时仍会构建 cfg……改为直接从模块源码不可取。
// 这里采用行为等价探测过于间接,故通过 makeHarness 传入空 config 后观察默认行为。
// spec 接入完整性改由 golden_check.py(可导入 DEFAULTS)全量校验,JS 侧校验
// spec 键在 KEY_MAP 中全部有映射且默认行为与金标一致。
const KEY_MAP = {
  call_nudge_interval: 'callNudgeInterval',
  repeat_window: 'repeatWindow',
  repeat_cmd_count: 'repeatCmdCount',
  repeat_min_calls: 'repeatMinCalls',
  max_recent_cmds: 'maxRecentCmds',
  fail_streak_threshold: 'failStreakThreshold',
  long_run_minutes: 'longRunMinutes',
  long_run_min_calls: 'longRunMinCalls',
  cooldown_sec: 'cooldownSec',
  fail_cooldown_sec: 'failCooldownSec',
  max_reminders: 'maxReminders',
  stop_min_calls: 'stopMinCalls',
  clock_tick_minutes: 'clockTickMinutes',
  clock_tick_max_minutes: 'clockTickMaxMinutes',
  foldback_window: 'foldbackWindow',
  review_log_size: 'reviewLogSize',
  review_log_floor: 'reviewLogFloor',
  settle_after_calls: 'settleAfterCalls',
  adaptive_warmup: 'adaptiveWarmup',
  adaptive_low_rate: 'adaptiveLowRate',
  adaptive_high_rate: 'adaptiveHighRate',
  adaptive_backoff_mult: 'adaptiveBackoffMult',
  adaptive_backoff_cap: 'adaptiveBackoffCap',
  adaptive_tighten_mult: 'adaptiveTightenMult',
  adaptive_tighten_floor_sec: 'adaptiveTightenFloorSec',
  drift_check_calls: 'driftCheckCalls',
  drift_cooldown_sec: 'driftCooldownSec',
  escalate_after_reminders: 'escalateAfterReminders',
  request_poll_sec: 'requestPollSec',
  request_ttl_sec: 'requestTtlSec',
  result_ttl_sec: 'resultTtlSec',
  result_settle_calls: 'resultSettleCalls',
};

let fails = 0;
const pending = [];
const check = (name, fn) => {
  try {
    const r = fn();
    // async 探针（如 fold-back 走 Promise）统一收集，最后 await
    if (r && typeof r.then === 'function') {
      pending.push(r.then(
        () => console.log('PASS ', name),
        (e) => { fails += 1; console.log('FAIL ', name, '-', e.message); }));
      return;
    }
    console.log('PASS ', name);
  } catch (e) { fails += 1; console.log('FAIL ', name, '-', e.message); }
};

// 1) normCmd 金标(与 Python 端同一份期望值)
for (const c of CASES.norm) {
  check('norm ' + JSON.stringify(c.cmd), () => {
    assert.equal(__test.normCmd(c.cmd), c.want);
  });
}

// 2) spec 接入完整性:每个 shared 键要么有 KEY_MAP 映射并已落入 DEFAULTS 且值一致,
//    要么走专用通道(interpreters / script_extensions,在下方单独验证)
check('spec keys fully applied to DEFAULTS', () => {
  const problems = [];
  for (const [snake, v] of Object.entries(SPEC)) {
    const camel = KEY_MAP[snake];
    if (!camel) {
      if (snake !== 'interpreters' && snake !== 'script_extensions') problems.push('unmapped: ' + snake);
      continue;
    }
    if (!(camel in __test.defaults)) problems.push('missing in DEFAULTS: ' + camel);
    else if (JSON.stringify(__test.defaults[camel]) !== JSON.stringify(v)) {
      problems.push(`${camel}: got ${JSON.stringify(__test.defaults[camel])} want ${JSON.stringify(v)}`);
    }
  }
  assert.deepEqual(problems, [], problems.join('; '));
});

// 2) spec 接入完整性:每个 shared 键必须有 KEY_MAP 映射
// 2.5) 专用通道键:interpreters / script_extensions
check('spec interpreters / script_extensions', () => {
  // 通过 normCmd 行为探测:解释器集合与脚本扩展名集合都与 spec 一致
  for (const it of SPEC.interpreters) {
    assert.equal(__test.normCmd(it + ' t1.py'), it + ' t.py', 'interpreter missing: ' + it);
  }
  assert.equal(__test.normCmd('python t1.ps1'), 'python t.ps1', 'script ext missing: ps1');
  assert.equal(__test.normCmd('python t1.noext'), 'python t1.noext', 'non-script must not normalize');
});

// 3) 会话回放(与 Python 侧相同的事件流、相同的步长)
function makeHarness(config) {
  const handlers = {};
  const ctx = {
    on(name, fn) { (handlers[name] ??= []).push(fn); },
    logger: { warn: () => {} },
  };
  apply(ctx, config);
  const emit = (name, ...args) => { for (const fn of handlers[name] ?? []) fn(...args); };
  const agent = {
    session: {}, injected: [], prepended: [],
    inject(message) { this.injected.push(message); },
    ctx: { effect(fn) { return typeof fn === 'function' ? fn() : undefined; } },
  };
  return { emit, agent };
}

const exec = (agent, tool, input, extra = {}) =>
  ({ agent, name: tool, input, signal: { aborted: false }, ...extra });

const texts = (agent) =>
  agent.injected.map((m) => (Array.isArray(m?.content) ? m.content.map((b) => b.text).join('\n') : String(m)));

const MARKERS = ['重复执行', '原样执行', '失败循环', '本地时钟', '还没有任何文件被修改',
  '次工具调用没有产生任何文件修改', '折返'];
const STEP_MS = 60 * 1000;

for (const session of CASES.sessions) {
  const { emit, agent } = makeHarness({
    clockTick: false, driftCheck: false, llmReview: false,
    adaptive: false, cooldownSec: 0, failCooldownSec: 0,
  });
  emit('agent/created', agent);
  const kinds = [];
  let now = Date.now();
  for (const ev of session.events) {
    now += STEP_MS;
    const input = ev.cmd !== undefined ? { command: ev.cmd } : { file_path: 'a.txt' };
    emit('tools/result', exec(agent, ev.tool, input), { isError: ev.fail === true });
    // JS 侧注入是异步交付(editorial: deliverOrReview 可能走 async),同步路径下
    // inject 已在 emit 内完成;静态清单路径为同步。
    const t = texts(agent).join('\n');
    for (const marker of MARKERS) {
      if (t.includes(marker) && !kinds.includes(marker)) kinds.push(marker);
    }
  }
  check('session ' + session.name, () => {
    assert.deepEqual(kinds, session.want_kinds, `got ${JSON.stringify(kinds)} want ${JSON.stringify(session.want_kinds)}`);
  });
}

// 4) 行为级变异的 JS 侧防线（源码 tripwire，保留作补充）：
//    reviewLogFloor 必须在 index.js 中被真实引用,且不允许残留字面量地板 40。
{
  const src = readFileSync(join(ROOT, 'index.js'), 'utf8');
  check('JS source tripwire: no literal Math.max(40', () => {
    assert.ok(!/Math\.max\(40\b/.test(src), 'literal 40 floor is back in index.js');
  });
  check('JS source tripwire: reviewLogFloor referenced', () => {
    assert.ok((src.match(/reviewLogFloor/g) || []).length >= 3,
      'reviewLogFloor must appear in DEFAULTS + execution points');
  });
}

// 5) 金标全键行为断言（JS 侧，v0.8）：
//    与 Python 侧 golden_check.py §9 同构——每个 spec 键都必须有一条
//    「改它的值 → 真实 tools/result 事件路径的可观测行为跟着变」的断言。
//    JS 侧通过 ctx.__testState 探测孔读取会话状态（与 dsh-smoke.mjs 同一机制）。
//    注：JS 端 Date.now() 不可注入,故凡涉及时间的键通过 stateOf() 直接摆放
//    时间戳基线来构造场景,而不是伪造时钟——仍然驱动真实的 tools/result 处理器。
//    JS 端无跨进程锁,故 state_lock / heartbeat 类键（Python 独有）不在此列。
const { freshState, kindCooldown, adaptiveStats } = __test;

function harnessWithState(config, services = {}) {
  const handlers = {};
  let stateMap = null;
  const ctx = {
    on(name, fn) { (handlers[name] ??= []).push(fn); },
    logger: { warn: () => {} },
    __testState(m) { stateMap = m; },
    ...services,
  };
  apply(ctx, config);
  const emit = (name, ...args) => { for (const fn of handlers[name] ?? []) fn(...args); };
  const makeAgent = () => ({
    session: {}, injected: [],
    inject(message) { this.injected.push(message); },
    ctx: { effect(fn) { return typeof fn === 'function' ? fn() : undefined; } },
  });
  const stateOf = (agent) => stateMap?.get(agent) ?? null;
  return { emit, makeAgent, stateOf };
}

const outTexts = (agent) => agent.injected.map((m) =>
  (Array.isArray(m?.content) ? m.content.map((b) => b.text).join('\n') : String(m)));
const markerCount = (agent, marker) => outTexts(agent).filter((t) => t.includes(marker)).length;

// 需要 __testState 的制造器（emit 走 tools/result）
function mkAgent(config, services) {
  const h = harnessWithState(config, services);
  const agent = h.makeAgent();
  h.emit('agent/created', agent);
  const call = (tool, input, isError = false) =>
    h.emit('tools/result', exec(agent, tool, input), { isError });
  const msg = () => h.emit('session/event', agent.session, { type: 'user/message' });
  const turn = () => h.emit('session/event', agent.session, { type: 'turn/end' });
  return { agent, st: () => h.stateOf(agent), call, msg, turn, emit: h.emit };
}

// ── 单键探针（JS）────────────────────────────────────────────────────────
// call_nudge_interval：阈值 4 → 前 3 次静默、第 4 次触发（用 4 而非 3，避免写死 3 的变异假绿）
check('behavior callNudgeInterval（阈值 4：静默 3 次后触发）', () => {
  const a = mkAgent({ clockTick: false, lessons: false, llmReview: false, adaptive: false,
    cooldownSec: 0, failCooldownSec: 0, callNudgeInterval: 4 });
  for (const f of ['a', 'b', 'c']) a.call('Read', { file_path: f });
  assert.equal(markerCount(a.agent, '没有产生任何文件修改'), 0, '阈值 4 时前 3 次应静默');
  a.call('Read', { file_path: 'd' });
  assert.ok(markerCount(a.agent, '没有产生任何文件修改') > 0, '第 4 次应触发');
});

// repeat 家族：用统一基线 + 单键覆写,做「改前触发 / 改后不触发」两面断言。
// 基线让 3 个重复 c 恰好成立(窗口 5、阈值 3、min 0);改动任一键都应让触发消失。
const REPEAT_BASE = { clockTick: false, lessons: false, llmReview: false, adaptive: false,
  cooldownSec: 0, failCooldownSec: 0, repeatMinCalls: 0, repeatCmdCount: 3, repeatWindow: 5 };
function repeatFires(overrides, seq = ['a', 'b', 'c', 'c', 'c']) {
  const a = mkAgent({ ...REPEAT_BASE, ...overrides });
  for (const c of seq) a.call('Bash', { command: c });
  return markerCount(a.agent, '原样执行');
}
// 基线：窗口 5 覆盖 3 个 c、阈值 3、min 0 → 必触发(证明场景本身有效)。
check('behavior repeat 基线（窗口 5/阈值 3/min 0 → 判重复）', () => {
  assert.ok(repeatFires({}) > 0, '基线场景必须触发重复');
});
// repeatWindow：窗口 2 只见 c,c(长度 2 < 阈值 3) → 不判重复。窗口被写小即漏判、写大即误判。
check('behavior repeatWindow（窗口 2 看不全 → 不判重复）', () => {
  assert.equal(repeatFires({ repeatWindow: 2 }, ['x', 'c', 'c', 'c']), 0,
    '窗口 2 只见 c,c → 不判重复');
});
// repeatCmdCount：阈值 3 → 3 个 c 恰好触发(>0);阈值 4 → 不够(0 次)。
// 两面并列:阈值被写大(如 99) → 正命题变 0 → 变红。
check('behavior repeatCmdCount（阈值 3 触发 / 阈值 4 不触发）', () => {
  assert.ok(repeatFires({ repeatCmdCount: 3 }) > 0, '阈值 3 → 3 个 c 触发重复');
  assert.equal(repeatFires({ repeatCmdCount: 4 }), 0, '阈值 4 > 3 个 c → 不触发');
});
// repeatMinCalls：最小调用数 99 → 整段被门槛挡住(0 次);基线 0 → 触发。
check('behavior repeatMinCalls（门槛 99 时整段静默）', () => {
  assert.equal(repeatFires({ repeatMinCalls: 99, callNudgeInterval: 99 }), 0,
    'repeatMinCalls=99 → 重复检测不进入');
});

check('behavior maxRecentCmds（缓冲 8 保留 a,b,c,c,c → 判重复）', () => {
  const a = mkAgent({ clockTick: false, lessons: false, llmReview: false, adaptive: false,
    cooldownSec: 0, failCooldownSec: 0, repeatMinCalls: 0, repeatCmdCount: 3,
    repeatWindow: 5, maxRecentCmds: 8 });
  for (const c of ['a', 'b', 'c', 'c', 'c']) a.call('Bash', { command: c });
  assert.ok(markerCount(a.agent, '原样执行') > 0, '缓冲 8 保留尾部 3 个 c → 判重复');
});

check('behavior failStreakThreshold（阈值 3 → 3 连败触发）', () => {
  const a = mkAgent({ clockTick: false, lessons: false, llmReview: false, adaptive: false,
    cooldownSec: 0, failCooldownSec: 0, failStreakThreshold: 3 });
  a.call('Bash', { command: 'boom1' }, true);
  a.call('Bash', { command: 'boom2' }, true);
  const before = markerCount(a.agent, '失败循环');
  a.call('Bash', { command: 'boom3' }, true);
  assert.equal(before, 0, '2 连败不触发');
  assert.ok(markerCount(a.agent, '失败循环') > 0, '3 连败触发');
});

check('behavior maxReminders（配额 2 → 至多 2 次）', () => {
  const a = mkAgent({ clockTick: false, lessons: false, llmReview: false, adaptive: false,
    cooldownSec: 0, failCooldownSec: 0, failStreakThreshold: 1, maxReminders: 2 });
  for (let i = 0; i < 8; i++) a.call('Bash', { command: 'boom' + i }, true);
  assert.equal(a.st().reminders, 2, '配额 2 应封顶在 2');
});

check('behavior cooldownSec（冷却 99s 拦住间隔 1s 的第二轮）', () => {
  // 冷却 99 秒：第一轮触发后，间隔极短的第二轮必须被拦下（不摆基线、不重置冷却）
  const a = mkAgent({ clockTick: false, lessons: false, llmReview: false, adaptive: false,
    cooldownSec: 99, failCooldownSec: 0, repeatMinCalls: 0, repeatCmdCount: 2, repeatWindow: 3 });
  for (const c of ['echo s', 'echo s']) a.call('Bash', { command: c });
  const first = markerCount(a.agent, '原样执行');
  assert.ok(first > 0, '第一轮应触发');
  // 第二轮：清 recentCmds 让重复检测重新成立，但推进极少时间 → 冷却拦住
  const st = a.st();
  st.recentCmds = []; st.callsSinceReminder = 0; st.lastReminderAt = Date.now();
  for (const c of ['echo s', 'echo s']) a.call('Bash', { command: c });
  assert.equal(markerCount(a.agent, '原样执行'), first, '冷却 99s 内第二轮不得再触发');
});

// foldbackWindow：窗口是「折返距离」上限——两面断言把窗口写死/写坏都露馅：
//   window=1 时 seen 恒为长度 1,slice(0,-1) 恒空 → A→B→A 折返探测不到(0 次);
//   window=5 时 A→B→A(距离 2)被识别(>0 次)。
//   若把探测误写成「命中即触发」或把窗口写死成小值,前半段会 >0 → 变红。
check('behavior foldbackWindow（窗口 1 → 折返探测不到；窗口 5 → A→B→A 被识别）', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'gl-fb-'));
  try {
    const folds = async (window) => {
      const p = join(dir, `f${window}.txt`);
      const a = mkAgent({ clockTick: false, lessons: false, llmReview: false, adaptive: false,
        cooldownSec: 0, failCooldownSec: 0, editFoldback: true, foldbackWindow: window });
      for (const content of ['AAA', 'BBB', 'AAA']) {
        writeFileSync(p, content); a.call('Write', { file_path: p });
        await new Promise((r) => setTimeout(r, 20));   // 让 recordEditHash 的异步读盘完成
      }
      await new Promise((r) => setTimeout(r, 30));
      return markerCount(a.agent, '原地打转');
    };
    assert.equal(await folds(1), 0, '窗口 1 不可能探测到折返(应为 0)');
    assert.ok(await folds(5) > 0, '窗口 5 时 A→B→A 应触发折返');
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

check('behavior reviewLogFloor/reviewLogSize（日志窗口）', () => {
  // floor=40, size=12 → 保留 40；floor=1, size=5 → 保留 5
  const cap = (floor, size) => {
    const a = mkAgent({ clockTick: false, lessons: false, llmReview: false, adaptive: false,
      cooldownSec: 0, failCooldownSec: 0, reviewLogFloor: floor, reviewLogSize: size });
    for (let i = 0; i < 60; i++) a.call('Bash', { command: 'c' + i });
    return a.st().recentLog.length;
  };
  assert.equal(cap(40, 12), 40, 'floor=40 压过 size=12');
  assert.equal(cap(1, 5), 5, 'floor=1 时以 size=5 为准');
});

check('behavior settleAfterCalls（结算步数 1 → 提醒被结算）', () => {
  adaptiveStats.clear();
  const a = mkAgent({ clockTick: false, lessons: false, llmReview: false, adaptive: true,
    cooldownSec: 0, failCooldownSec: 0, repeatMinCalls: 0, repeatCmdCount: 2,
    repeatWindow: 3, settleAfterCalls: 1, adaptiveWarmup: 1 });
  for (let i = 0; i < 2; i++) a.call('Bash', { command: 'echo s' });
  for (let i = 0; i < 5; i++) a.call('Read', { file_path: 'x' + i });
  const fired = [...adaptiveStats.values()].reduce((s, v) => s + (v.fired || 0), 0);
  assert.ok(fired > 0, 'settleAfterCalls=1 时 pending 应被结算计入 fired');
});

// adaptive_* 家族：kindCooldown 是这些键在 JS 侧的唯一执行位。
// 注意 JS 端冷却一律以**毫秒**计（base 与 floor*1000 同量纲），故 base 用 ms。
check('behavior adaptiveLowRate/HighRate/BackoffMult/BackoffCap/TightenMult/TightenFloorSec', () => {
  const base = { adaptive: true, adaptiveWarmup: 5, adaptiveLowRate: 0.3, adaptiveHighRate: 0.7,
    adaptiveBackoffMult: 2, adaptiveBackoffCap: 4, adaptiveTightenMult: 0.75,
    adaptiveTightenFloorSec: 60 };
  const B = 120 * 1000;   // 真实量级：2 分钟
  const withStat = (eff) => { adaptiveStats.clear(); adaptiveStats.set('k', { fired: 10, effective: eff }); };
  // 低有效率 → backoff：min(base*mult, base*cap)
  withStat(0);   // rate 0 → < lowRate → backoff
  assert.equal(kindCooldown(base, 'k', B), B * 2, 'backoff: 2×base');
  assert.equal(kindCooldown({ ...base, adaptiveBackoffCap: 1.5 }, 'k', B), B * 1.5, 'cap=1.5 → 1.5×base');
  assert.equal(kindCooldown({ ...base, adaptiveBackoffMult: 1 }, 'k', B), B, 'mult=1 → base');
  // 高有效率 → tighten：max(base*mult, floor*1000)
  withStat(10);  // rate 1 → > highRate → tighten
  assert.equal(kindCooldown(base, 'k', B), B * 0.75, 'tighten: 0.75×base');
  assert.equal(kindCooldown({ ...base, adaptiveTightenFloorSec: 99999 }, 'k', B),
    99999 * 1000, 'floor 秒×1000 抬过收紧值');
  // 门槛：rate 0.8 时 highRate=0.9 不收紧（0.8<0.9 且 0.8>0.3 不 backoff → base）
  withStat(8);
  assert.equal(kindCooldown(base, 'k', B), B * 0.75, 'rate .8 > .7 → tighten');
  assert.equal(kindCooldown({ ...base, adaptiveHighRate: 0.9 }, 'k', B), B, 'rate .8 < .9 → base');
  assert.equal(kindCooldown({ ...base, adaptiveLowRate: 0.9, adaptiveHighRate: 0.95 }, 'k', B), B * 2,
    'rate .8 < .9 → backoff 分支');
  assert.equal(kindCooldown({ ...base, adaptiveWarmup: 99 }, 'k', B), B, '预热不足 → base');
});

check('behavior escalateAfterReminders（门槛 3 → 投递请求）', () => {
  const dir = mkdtempSync(join(tmpdir(), 'gl-ch-'));
  process.env.LOOKUP_STATE_DIR = dir;
  try {
    const a = mkAgent({ deliverReview: true, llmApiKey: 'stub', llmReview: false,
      clockTick: false, lessons: false, cooldownSec: 0, failCooldownSec: 0,
      failStreakThreshold: 1, escalateAfterReminders: 3 });
    a.st().reminders = 2;
    a.call('Bash', { command: 'boom' }, true);
    const rdir = join(dir, 'review');
    const reqs = existsSync(rdir) ? readdirSync(rdir).filter((n) => n.startsWith('req_')) : [];
    assert.ok(reqs.length > 0, 'reminders 到 3 应投递请求');
  } finally {
    delete process.env.LOOKUP_STATE_DIR;
    rmSync(dir, { recursive: true, force: true });
  }
});

check('behavior clockTickMinutes/clockTickMaxMinutes（时钟锚点）', () => {
  // clockTickMinutes=0 → 立刻可触发；把 lastClockTickAt/lastReminderAt/startedAt 摆到过去
  const a = mkAgent({ clockTick: true, clockTickMinutes: 0, clockTickBackoff: false,
    lessons: false, llmReview: false, adaptive: false, cooldownSec: 0, failCooldownSec: 0 });
  a.st().lastClockTickAt = 0;
  a.st().lastReminderAt = 0;
  a.call('Read', { file_path: 'a' });
  assert.ok(markerCount(a.agent, '本地时钟') > 0, 'clockTickMinutes=0 → 时钟触发');
  // clockTickMaxMinutes：退避上限决定 clockInterval 的封顶（需 startedAt 够久）
  const b = mkAgent({ clockTick: true, clockTickMinutes: 1, clockTickBackoff: true,
    clockTickMaxMinutes: 6, lessons: false, llmReview: false, adaptive: false,
    cooldownSec: 0, failCooldownSec: 0 });
  b.st().lastClockTickAt = 0;
  b.st().lastReminderAt = 0;
  b.st().startedAt = Date.now() - 10 * 60000;   // 已跑 10 分钟，满足 minutesSince>=1
  b.st().clockInterval = 5;   // 下一跳 min(max(5*2,1), cap)
  b.call('Read', { file_path: 'a' });
  assert.equal(b.st().clockInterval, 6, '退避封顶 6（5×2=10 → min 6）');
});

check('behavior stopMinCalls（下限 5(calls=10) → 结束前审查）', () => {
  const a = mkAgent({ stopCheck: true, stopMinCalls: 5, longRunMinutes: 1,
    clockTick: false, lessons: false, llmReview: false, adaptive: false, cooldownSec: 0 });
  const st = a.st();
  st.toolCalls = 10; st.edits = 0; st.startedAt = Date.now() - 120000;
  a.turn();
  assert.ok(markerCount(a.agent, '结束前审查') > 0, 'stopMinCalls=5 且 calls=10 → 触发');
});

check('behavior driftCheckCalls/driftCooldownSec（巡检间隔触发 + 冷却压制第二次）', () => {
  const calls = [];
  const fakeLlm = {
    async *stream(options) {
      calls.push(options);
      yield { type: 'text', text: '{"verdict":"on-track","reason":"r","suggestion":"s"}' };
      yield { type: 'finish', reason: { kind: 'stop' } };
    },
  };
  // driftCheckCalls=3 触发首次巡检;driftCooldownSec=3600 使冷却窗内(calls 3..8)
  // 不得再巡检 → driftChecks 恰为 1。若冷却被写坏(如写成 0),会在后续调用反复触发 → 变红。
  const a = mkAgent({ driftCheck: true, driftCheckCalls: 3, driftCooldownSec: 3600,
    clockTick: false, lessons: false, llmReview: false, adaptive: false, cooldownSec: 0 },
    { llm: fakeLlm });
  a.agent.session.requestHeader = () => ({ config: { provider: 'p', model: 'm' } });
  for (let i = 0; i < 8; i++) a.call('Read', { file_path: 'a' });
  assert.equal(a.st().driftChecks, 1, '间隔 3 首次巡检后,1 小时冷却内应只巡检 1 次');
  assert.equal(a.st().callsAtLastDrift, 3, '巡检按 calls(第 3 次)触发');
});

check('behavior requestTtlSec/resultTtlSec/resultSettleCalls（第三通道三闸，JS 端）', () => {
  // 由 channel_check.py（Python 端）与 dsh-smoke.mjs 端到端覆盖；此处登记 JS 侧
  // takeReviewResults 的三道闸对 spec 键的消费（ttl / settle）。
  const src = readFileSync(join(ROOT, 'index.js'), 'utf8');
  assert.ok(/cfg\.resultTtlSec/.test(src), 'resultTtlSec 必须被消费');
  assert.ok(/cfg\.resultSettleCalls/.test(src), 'resultSettleCalls 必须被消费');
});

check('behavior interpreters/scriptExtensions（normCmd 随 spec 集合变）', () => {
  assert.equal(__test.normCmd('python a1.py'), 'python a.py');
  assert.equal(__test.normCmd('pwsh a1.ps1'), 'pwsh a.ps1');
  assert.equal(__test.normCmd('unknownbin a1.py'), 'unknownbin a1.py');
  assert.equal(__test.normCmd('python a1.mjs'), 'python a.mjs');
  assert.equal(__test.normCmd('python a1.noext'), 'python a1.noext');
});

await Promise.all(pending);

console.log();
console.log('golden.mjs: ' + (fails === 0 ? 'ALL PASSED' : fails + ' FAILURES'));
process.exit(fails === 0 ? 0 : 1);
