/**
 * 双端一致性金标测试(JS 侧)——与 test/golden_check.py 消费同一份 golden_cases.json,
 * 校验 normCmd 金标、spec.json 接入完整性、会话回放触发序列。任一漂移即红。
 * Run: node test/golden.mjs
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
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
};

let fails = 0;
const check = (name, fn) => {
  try { fn(); console.log('PASS ', name); }
  catch (e) { fails += 1; console.log('FAIL ', name, '-', e.message); }
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

// 4) 行为级变异的 JS 侧防线(源码 tripwire):
//    reviewLogFloor 必须在 index.js 中被真实引用,且不允许残留字面量地板 40。
//    局限:源码检查≠行为断言;JS 侧行为级探针受限于 state 不导出,
//    后续可通过 __test 暴露 state 工厂补齐(见回信)。
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

console.log();
console.log('golden.mjs: ' + (fails === 0 ? 'ALL PASSED' : fails + ' FAILURES'));
process.exit(fails === 0 ? 0 : 1);
