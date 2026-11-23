/**
 * AI Look-Up — Host plugin for the DeepSeek Harness (cordis bundle).
 *
 * Watches the agent's behavioral rhythm and injects a self-review nudge when
 * aimless grinding is detected: the exact same command repeated, variant
 * commands that normalize to the same shape, long runs with zero file edits,
 * dense calls with zero output, and failure loops. Also anchors the agent's
 * unreliable sense of time by injecting the real local clock periodically.
 *
 * v0.4: when a trigger fires and the `llm` service is present, an independent
 * reviewer LLM call (same provider/model route as the main agent, temperature
 * 0, strict JSON verdict) judges the recent behavior; its verdict is injected
 * instead of the static checklist. Any reviewer failure falls back to the
 * static checklist, so the plugin degrades gracefully.
 *
 * Extension points (see the cordis-plugin-development skill):
 * - `tools/result`      observe final tool outcomes
 * - `session/event`     reset per-turn state on `user/message`, wrap-up check on `turn/end`
 * - `agent.inject()`    mid-run context, enters the next admitted step
 * - `llm` (optional)    independent reviewer calls
 */

import { randomUUID, createHash } from 'node:crypto';
import { readFileSync, writeFileSync, mkdirSync, readdirSync, rmSync, renameSync } from 'node:fs';
import { tmpdir } from 'node:os';

const PRODUCTIVE_TOOLS = new Set(['Edit', 'Write', 'MultiEdit', 'NotebookEdit', 'ApplyPatch']);
const PRODUCTIVE_ALIASES = new Set([
  'edit', 'write', 'multiedit', 'notebookedit', 'applypatch',
  'write_file', 'edit_file', 'create_file', 'replace_file_content',
  'multi_replace_file_content', 'str_replace_editor',
  'str_replace_based_edit_tool', 'patch', 'file_edit', 'file_write',
]);
const SHELL_ALIASES = new Set([
  'bash', 'run_command', 'run_shell_command', 'execute_command',
  'shell', 'terminal', 'cmd',
]);
const PATH_KEYS = ['file_path', 'path', 'notebook_path', 'TargetFile', 'target_file', 'Path', 'filePath'];
const CMD_KEYS = ['command', 'cmd', 'script', 'CommandLine', 'command_line'];

const isProductiveTool = (tool) => {
  const t = String(tool ?? '');
  return PRODUCTIVE_TOOLS.has(t) || PRODUCTIVE_ALIASES.has(t.toLowerCase());
};

const isShellTool = (tool) => {
  const t = String(tool ?? '');
  return t === 'Bash' || SHELL_ALIASES.has(t.toLowerCase());
};

import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

// ---- spec.json: single source of truth shared with the Python hook ----
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
  lessons_ttl_days: 'lessonsTtlDays',
  lesson_guard_hits: 'lessonGuardHits',
  round_replan_rounds: 'roundReplanRounds',
  round_replan_min_calls: 'roundReplanMinCalls',
  step_replan_calls: 'stepReplanCalls',
  escalate_after_reminders: 'escalateAfterReminders',
  request_poll_sec: 'requestPollSec',
  request_ttl_sec: 'requestTtlSec',
  result_ttl_sec: 'resultTtlSec',
  result_settle_calls: 'resultSettleCalls',
};

function loadSpec() {
  try {
    const spec = JSON.parse(readFileSync(join(dirname(fileURLToPath(import.meta.url)), 'spec.json'), 'utf8'));
    if (spec && typeof spec === 'object' && spec.shared && typeof spec.shared === 'object') return spec.shared;
  } catch { /* fall back to built-in defaults below */ }
  return {};
}
const SPEC = loadSpec();

const DEFAULTS = {
  enabled: true,
  callNudgeInterval: 30,
  repeatWindow: 5,
  repeatCmdCount: 3,
  repeatMinCalls: 8,
  failStreakThreshold: 3,
  failCooldownSec: 180,
  longRunMinutes: 25,
  longRunMinCalls: 15,
  cooldownSec: 300,
  maxReminders: 12,
  stopCheck: true,
  stopMinCalls: 40,
  clockTick: true,
  clockTickMinutes: 5,
  clockTickBackoff: true,
  clockTickMaxMinutes: 60,
  maxRecentCmds: 8,
  reviewLogFloor: 40,
  settleAfterCalls: 10,
  adaptiveWarmup: 5,
  adaptiveLowRate: 0.3,
  adaptiveHighRate: 0.7,
  adaptiveBackoffMult: 2,
  adaptiveBackoffCap: 4,
  adaptiveTightenMult: 0.75,
  adaptiveTightenFloorSec: 60,
  // independent reviewer (v0.4): one LLM call per trigger, strict JSON verdict
  llmReview: true,
  llmTimeoutMs: 20000,
  reviewLogSize: 12,
  // fold-back edit detection (v0.5): file content returning to a seen state
  editFoldback: true,
  foldbackWindow: 5,
  // adaptive reminder effectiveness (v0.5): effective reminders fire more often
  adaptive: true,
  // goal-drift patrol (v0.5): every N calls the reviewer checks goal alignment
  driftCheck: true,
  driftCheckCalls: 30,
  driftCooldownSec: 900,
  // ── 轮次里程碑·强制全局重规划提醒(v0.9)──
  // 与负向触发器互补:负向只报"故障",健康地钻牛角尖(每步有新信息、偶尔改文件)
  // 永远不会命中。里程碑不看行为好坏,到点强制抬头。有意不走 fire():不计升级
  // 阶梯、不占 maxReminders 配额、无时间冷却、不参与有效性结算(定期保养,非故障)。
  // roundReplan: 连续 N 个工作回合未重规划 → 新回合开始时注入;
  // roundReplanMinCalls: 步数下限,纯寒暄轮不推进里程碑;
  // stepReplan: 每 N 次工具调用无条件触发(单 prompt 马拉松也会被抬头)。
  roundReplan: true,
  roundReplanRounds: 8,
  roundReplanMinCalls: 10,
  stepReplan: true,
  stepReplanCalls: 80,
  // 用户可见性(v0.9):静默 ≠ 无感。提醒类注入末尾附呈报指令,要求 AI 在下一次
  // 可见回复开头用一行向用户简报「AI 抬头提醒了我:<原因>,我接下来:<动作>」。
  // 时钟锚点与坑位登记不附呈报,避免刷屏。
  showToUser: true,
  // lesson ledger (v0.5): failures register pitfalls, new agents get the top ones
  lessons: true,
  // lesson TTL (v0.8.4): pitfalls not re-hit within N days are dropped; 0 = keep forever.
  // Without it, dev-time test data occupies injection slots forever by hits ranking.
  lessonsTtlDays: 7,
  // pre-execution lesson guard: block exact repeat of a failing command or pattern hit >= lessonGuardHits
  lessonGuard: true,
  lessonGuardHits: 2,
  // ── 第三通道:投递请求 + 独立 watcher（v0.7）──
  // deliverReview 默认关:它要花用户自己的 API 额度,只有显式开启才投递。
  deliverReview: false,
  // watcher 用的自有 key(投递前置条件之一);空则永不投递。
  llmApiKey: '',
  escalateAfterReminders: 3,
  requestPollSec: 2,
  requestTtlSec: 900,
  resultTtlSec: 900,
  resultSettleCalls: 60,
};

for (const [snake, v] of Object.entries(SPEC)) {
  const camel = KEY_MAP[snake];
  if (camel && camel in DEFAULTS) DEFAULTS[camel] = v;
}

const DRIFT_LABEL = {
  'on-track': '仍在服务主线',
  'drifting': '子任务喧宾夺主',
  'stuck': '已明显偏离主线',
};

const REVIEW_DRIFT_SYSTEM = [
  '你是 AI 编码代理的目标漂移审查员。主代理看不到你的存在。长上下文任务最常见的失败是:',
  '最初为核验某个决定而展开的子任务,做着做着变成了独立目标,喧宾夺主。',
  '你只依据最初任务描述与最近工具调用记录判断:当前行为是否仍在服务最初目标,',
  '还是某个子任务已经扩张成了事实上的新目标。只输出一个 JSON 对象,不要输出任何其他文字:',
  '{"verdict":"on-track|drifting|stuck","reason":"一句话:当前行为与最初目标的关系","suggestion":"一句话:如何收束"}',
  'on-track=仍在服务主线;drifting=子任务喧宾夺主;stuck=已明显偏离主线。',
].join('\n');

/**
 * Reminder-effectiveness stats, keyed by trigger kind. Module scope so it
 * survives across sessions within one host process; the ZCode side persists
 * the same structure to disk (adaptive.json).
 */
const adaptiveStats = new Map();

/**
 * Lesson ledger (module scope, per host process): failure loops register the
 * repeating command pattern; freshly created agents receive the top lessons
 * so known pitfalls survive conversation compaction. The ZCode side persists
 * the same structure to disk (lessons.json).
 */
const lessonsLedger = new Map();

function registerLesson(pattern, correction, raw = '', editsAt = null) {
  if (!pattern) return;
  const now = Date.now();
  const cleanRaw = raw ? rawCmd(raw) : '';
  const prev = lessonsLedger.get(pattern);
  if (prev) {
    prev.hits += 1;
    prev.correction = correction;
    prev.lastSeen = now;
    if (cleanRaw) prev.lastRaw = cleanRaw;
    if (editsAt !== null && editsAt !== undefined) prev.editsAt = Number(editsAt);
  } else {
    const item = { pattern, correction, hits: 1, lastSeen: now, lastRaw: cleanRaw };
    if (editsAt !== null && editsAt !== undefined) item.editsAt = Number(editsAt);
    lessonsLedger.set(pattern, item);
  }
}

/** 命令跑通 → 对应坑位扣减一次,扣到 0 即移除。账本不再只进不出。 */
function redeemLesson(cmd, cfg = null) {
  const c = cfg ?? DEFAULTS;
  if (!c.lessons) return;
  const n = normCmd(cmd);
  if (!n) return;
  const prev = lessonsLedger.get(n);
  if (!prev) return;
  prev.lastRaw = '';
  prev.hits -= 1;
  if (prev.hits <= 0) lessonsLedger.delete(n);
}

/**
 * 命中坑位后若期间发生过编辑,判定为"修复后验证"→ 把 block 降级为 warn。
 * currentEdits 为 null(调用方未提供)或账本未记录 editsAt 时行为不变。
 */
function guardAction(action, entry, currentEdits) {
  if (action !== 'block' || currentEdits === null || currentEdits === undefined) return action;
  if (!entry || entry.editsAt === undefined || entry.editsAt === null) return action;
  return currentEdits > entry.editsAt ? 'warn' : action;
}

function checkLessonGuard(cmd, cfg = null, currentEdits = null) {
  const c = cfg ?? DEFAULTS;
  if (!c.enabled || !c.lessons || !c.lessonGuard) return null;
  const n = normCmd(cmd);
  const r = rawCmd(cmd);
  if (!n && !r) return null;
  const ttlDays = c.lessonsTtlDays || 0;
  const deadline = ttlDays > 0 ? Date.now() - ttlDays * 86400000 : 0;
  const alive = [...lessonsLedger.values()].filter((e) => !deadline || (e.lastSeen || 0) >= deadline);
  const guardHits = Math.max(1, Number(c.lessonGuardHits ?? 2));
  for (const entry of alive) {
    if (r && entry.lastRaw && r === entry.lastRaw) {
      return { action: guardAction('block', entry, currentEdits), entry };
    }
    if (n && entry.pattern === n) {
      if ((entry.hits || 1) >= guardHits) {
        return { action: guardAction('block', entry, currentEdits), entry };
      }
      return { action: 'warn', entry };
    }
  }
  return null;
}

function lessonsMessage(state, force = false, cfg = null) {
  if (!lessonsLedger.size) return null;
  // TTL: pitfalls not re-hit within lessonsTtlDays are dropped (0 = keep forever)
  const ttlDays = (cfg && cfg.lessonsTtlDays) || 0;
  const deadline = ttlDays > 0 ? Date.now() - ttlDays * 86400000 : 0;
  const alive = [...lessonsLedger.values()].filter((e) => !deadline || (e.lastSeen || 0) >= deadline);
  if (!alive.length) return null;
  const top = alive.sort((a, b) => b.hits - a.hits).slice(0, 3);
  const sig = top.map((e) => e.pattern + 'x' + e.hits).join('|');
  if (state.lessonsSig === sig && !force) return null;
  state.lessonsSig = sig;
  const lines = ['📚 【AI 抬头 · 已知坑位】请勿重复:'];
  top.forEach((e, i) => lines.push((i + 1) + '. [' + e.hits + ' 次] ' + e.pattern + ' —— ' + e.correction));
  return lines.join('\n');
}

const STALE_AFTER_MS = 2 * 60 * 60 * 1000;

const REVIEW_SYSTEM = [
  '你是 AI 编码代理的独立行为审查员。主代理看不到你的存在,你只依据给它的任务描述与最近工具调用记录做判断。',
  '判断标准:最近的调用是否持续带来新信息、是否朝着任务目标推进;同样的命令反复执行、长时间没有任何文件修改、连续失败后仍在重试,都是空跑迹象。',
  '只输出一个 JSON 对象,不要输出任何其他文字:',
  '{"verdict":"on-track|drifting|stuck","reason":"一句话依据","suggestion":"一句话建议"}',
  'on-track=仍在正轨;drifting=有偏航/空转迹象;stuck=确认空跑或卡死。',
].join('\n');

const VERDICT_LABEL = {
  'on-track': '仍在正轨',
  'drifting': '有偏航迹象',
  'stuck': '空跑确认',
};

// 模糊型触发:统计信号可能误报,on-track 短确认有意义。
// 机械证据型(重复/折返/失败循环/结束前审查)的触发条件本身就是"非正轨"的证据,
// 审查者回 on-track 时忽略判定、保留完整静态清单,避免自相矛盾。
const FUZZY_KINDS = new Set(['no-output', 'long-run']);

const clipGoal = (goal, head = 120, tail = 80) => {
  goal = goal || '';
  if (goal.length <= head + tail) return goal;
  return goal.slice(0, head) + ' … ' + goal.slice(-tail);
};

// 短确认词:这类回复只是"继续推进"的许可,不构成新任务,不能冲掉初始目标。
const SHORT_ACKS = new Set([
  '继续', '好的', '好', '可以', '行', '都做吧', '没问题', '是的', '对',
  'ok', 'okay', 'yes', 'y', 'go', 'go ahead', 'continue', 'proceed',
  'sure', 'do it',
]);
const PUNCT_RX = /[\s\u3000,，。.!！?？~～、:：;；'"“”‘’()（）\[\]【】]+/g;

function isSubstantiveGoal(text) {
  const s = String(text ?? '').trim();
  if (!s) return false;
  if (s.length < 10 && SHORT_ACKS.has(s.replace(PUNCT_RX, '').toLowerCase())) return false;
  return true;
}

/** 双层目标维护:initialGoal 保最初实质任务,goal 只被实质指令覆盖。 */
function updateGoals(state, text) {
  const g = String(text ?? '').trim();
  if (!g) return;
  const curInitial = state.initialGoal || '';
  if (!curInitial) {
    state.initialGoal = g.slice(0, 400);
  } else if (!isSubstantiveGoal(curInitial) && isSubstantiveGoal(g)) {
    state.initialGoal = g.slice(0, 400);
  }
  if (isSubstantiveGoal(g) || !state.goal) state.goal = g.slice(0, 400);
}

const localHm = (ts) => new Date(ts).toTimeString().slice(0, 5);
const minutesSince = (state, now) => Math.max(0, (now - state.startedAt) / 60000);

function mergeConfig(config) {
  const cfg = { ...DEFAULTS };
  if (config && typeof config === 'object') {
    for (const key of Object.keys(DEFAULTS)) {
      if (config[key] !== undefined) cfg[key] = config[key];
    }
  }
  return cfg;
}

const _escapeRe = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
const SCRIPT_RX = new RegExp('\\.(?:' + (SPEC.script_extensions ?? ['py', 'js', 'ts', 'mjs', 'cjs', 'sh', 'ps1', 'bat']).map(_escapeRe).join('|') + ')$', 'i');
const INTERPRETERS = new Set(SPEC.interpreters ?? ['python', 'python3', 'py', 'node', 'bash', 'sh', 'pwsh', 'powershell', 'perl', 'ruby']);
const SEQ_RX = /[\d_-]+(?=\.\w+$)/;
const INLINE_FLAGS = new Set(['-c', '-m', '-e', '-p', '--eval', '--print']);

function normCmd(cmd) {
  let s = String(cmd ?? '').trim().toLowerCase();
  if (!s) return '';
  s = s.replace(/\s+/g, ' ');
  // 只切"准备动作"(&& || ;)——管道 | 是数据流,不切
  const parts = s.split(/&&|\|\||;/);
  s = (parts[parts.length - 1] || s).trim();
  const toks = s.split(' ').filter(Boolean);
  if (!toks.length) return '';
  let head = (toks[0] ?? '').replace(/\\/g, '/').split('/').pop().replace(/^["']|["']$/g, '');
  head = head.replace(/\.exe$/, '');

  // 解释器 + 脚本文件:归一化脚本名里的序号(_peek2.py → _peek.py)
  if (INTERPRETERS.has(head) && toks.length > 1) {
    let idx = 1;
    while (idx < toks.length) {
      const tClean = (toks[idx] ?? '').replace(/^["']|["']$/g, '');
      if (INLINE_FLAGS.has(tClean)) return s; // -c / -m 等内联或模块执行不截断
      if (tClean.startsWith('-')) { idx += 1; continue; }
      break;
    }
    if (idx < toks.length) {
      const second = (toks[idx] ?? '').replace(/\\/g, '/').split('/').pop().replace(/^["']|["']$/g, '');
      if (SCRIPT_RX.test(second)) {
        const stripped = second.replace(SEQ_RX, '');
        const hasSeq = stripped !== second;
        const rest = toks.slice(idx + 1).map((t) => t.replace(/^["']|["']$/g, ''));
        const subcmds = rest.filter((t) => t && !t.startsWith('-'));
        if (!hasSeq && subcmds.length) {
          return (head + ' ' + stripped + ' ' + subcmds.join(' ')).trim();
        }
        return (head + ' ' + stripped).trim();
      }
    }
    return s;   // 非脚本形态不截断:任何 token 截断都会制造误报(-c 脚本/build2/git 分支)
  }
  return s;
}

const rawCmd = (cmd) => String(cmd ?? '').trim().replace(/\s+/g, ' ');

function extractCmd(exec) {
  const input = exec?.input ?? exec?.arguments ?? exec?.args ?? exec?.toolInput;
  if (typeof input === 'string') return input;
  if (input && typeof input === 'object') {
    for (const key of CMD_KEYS) {
      if (typeof input[key] === 'string') return input[key];
    }
  }
  return '';
}

const SEARCH_ONLY_TOOLS = new Set(['grep', 'glob', 'webfetch', 'websearch', 'search',
  'grep_search', 'find_by_name', 'find_files']);
const READ_TOOLS = new Set(['read', 'read_file', 'view_file']);
const RANGE_KEYS = ['offset', 'limit', 'start_line', 'end_line', 'StartLine', 'EndLine'];

/**
 * 非 Shell 只读工具的重复签名 [norm, raw];无法判定重复时返回 ['', '']。
 *
 * 只对两类生效:A) 搜索/网络/查询类; B) 带显式行号范围的 Read。
 * 裸 Read(仅 file_path)刻意不参与——它是各宿主最常见的"推进步数"垫脚调用。
 */
function nonShellRepeatSig(tool, exec) {
  const input = exec?.input ?? exec?.arguments ?? exec?.args ?? exec?.toolInput ?? {};
  if (!input || typeof input !== 'object') return ['', ''];
  const tLow = String(tool ?? '').toLowerCase();
  if (SEARCH_ONLY_TOOLS.has(tLow)) {
    const q = input.pattern ?? input.query ?? input.url ?? input.path ?? '';
    if (!q) return ['', ''];
    const norm = tLow + ':' + rawCmd(String(q)).toLowerCase().slice(0, 80);
    let dumped;
    try {
      dumped = JSON.stringify(Object.keys(input).sort()
        .reduce((o, k) => { o[k] = input[k]; return o; }, {}));
    } catch {
      dumped = String(q);
    }
    return [norm, tLow + ':' + rawCmd(dumped)];
  }
  if (READ_TOOLS.has(tLow)) {
    if (!RANGE_KEYS.some((k) => input[k] !== undefined && input[k] !== null && input[k] !== '')) {
      return ['', ''];
    }
    const p = extractPath(input).replace(/\\/g, '/').toLowerCase();
    if (!p) return ['', ''];
    const rng = RANGE_KEYS.filter((k) => input[k] !== undefined && input[k] !== null)
      .map((k) => k + '=' + input[k]).join(',');
    const sig = 'read:' + p + '#' + rng;
    return [sig, sig];
  }
  return ['', ''];
}

function extractPath(input) {
  if (!input || typeof input !== 'object') return '';
  for (const key of PATH_KEYS) {
    if (input[key]) return String(input[key]);
  }
  return '';
}

function toolNameOf(exec) {
  for (const key of ['name', 'toolName', 'tool']) {
    if (typeof exec?.[key] === 'string') return exec[key];
  }
  return 'Unknown';
}

function briefOf(exec, cmd, tool) {
  if (isShellTool(tool) && cmd) return cmd.slice(0, 100);
  const input = exec?.input ?? exec?.arguments ?? exec?.args ?? exec?.toolInput;
  if (input && typeof input === 'object') {
    const p = extractPath(input);
    if (p) return p.slice(0, 100);
    for (const key of ['url', 'query']) {
      if (input[key]) return String(input[key]).slice(0, 100);
    }
  }
  if (cmd) return cmd.slice(0, 100);
  try {
    return JSON.stringify(input ?? {}).slice(0, 80);
  } catch {
    return '';
  }
}

const firstLine80 = (text) => {
  const s = String(text ?? '').trim();
  if (!s) return '';
  return s.split(/\r?\n/)[0].trim().slice(0, 80).replace(/\n/g, ' ');
};

/**
 * 从失败结果里提取一行错误摘要(前 80 字),供审查材料直接引用根因。
 * 顺序:result.error(对象取 .message) → result.message → result.stderr → result.output。
 */
function extractErr(result) {
  if (!result) return '';
  const candidates = [result.error, result.message, result.stderr, result.output];
  for (const v of candidates) {
    if (!v) continue;
    if (typeof v === 'object') {
      const inner = v.message ?? v.error ?? v.stderr ?? v.output ?? '';
      if (inner) return firstLine80(inner);
      continue;
    }
    return firstLine80(v);
  }
  return '';
}

function freshState() {
  const now = Date.now();
  return {
    startedAt: now, lastEventAt: now,
    sessionId: 'default',
    toolCalls: 0, byTool: {},
    edits: 0, editsSinceReminder: 0, callsSinceReminder: 0,
    failStreak: 0, totalFailures: 0,
    // 夹心式失败:同一归一化命令在无修改、无该命令成功期间的连败 + 穿插只读段数
    cmdFailNorm: '', cmdFailStreak: 0, cmdFailInterleaves: 0, lastWasCmdFail: false,
    recentCmds: [], recentLog: [],
    goal: '',
    // 双层目标:initialGoal 保最初实质任务,短确认回复不覆盖
    initialGoal: '',
    reminders: 0, lastReminderAt: 0, lastFailReminderAt: 0, lastTrigger: '',
    stopBlocks: 0, promptResets: 0, lastClockTickAt: 0, clockTicks: 0,
    reviewInFlight: false, lessonsSig: '',
    fileHashes: {}, pendingReminders: [],
    driftChecks: 0, lastDriftAt: 0, callsAtLastDrift: 0,
    clockInterval: 0,
    replanRounds: 0, callsAtReplan: 0, replans: 0,
  };
}

function resetStretch(state, cfg, now) {
  state.startedAt = now;
  state.callsSinceReminder = 0;
  state.editsSinceReminder = 0;
  state.failStreak = 0;
  state.cmdFailNorm = '';
  state.cmdFailStreak = 0;
  state.cmdFailInterleaves = 0;
  state.lastWasCmdFail = false;
  state.recentCmds = [];
  state.clockInterval = 0;
  // P2-2：reminders 必须在这里清零，否则同一会话累计 maxReminders 次后
  // 配额永久耗尽，用户开新任务也再无提醒（新任务静默失守）。
  state.reminders = 0;
  // 注意:这里绝不能动 lastReminderAt / lastFailReminderAt——证据窗口已清零,
  // 新回合本就不可能立刻触发;把冷却基准拉到 now 会给每个新回合装填一个
  // 完整的 cooldownSec 静默窗,回合开头 5 分钟内的空跑全部漏报。
}

function repeatHit(state, cfg) {
  const window = state.recentCmds.slice(-cfg.repeatWindow);
  if (window.length < cfg.repeatCmdCount) return null;
  const last = window[window.length - 1].n;
  if (!last) return null;
  const count = window.filter((c) => c.n === last).length;
  return count >= cfg.repeatCmdCount ? { prefix: last, count } : null;
}

function exactRepeatHit(state, cfg) {
  const window = state.recentCmds.slice(-cfg.repeatWindow);
  if (window.length < cfg.repeatCmdCount) return null;
  const last = window[window.length - 1].r ?? '';
  if (!last) return null;
  const count = window.filter((c) => (c.r ?? '') === last).length;
  return count >= cfg.repeatCmdCount ? { raw: last, count } : null;
}

/**
 * 统一发火口:所有触发器的状态维护都必须走这里,不许各写一份。
 *
 * ★ v0.8 收编 fail-loop:此前 JS 侧 fail-loop 绕过本函数、在 880-931 行
 * 手工复刻了整套状态维护(F1 就是漏抄 3 项)。现在它同 Python 侧一样走本函数。
 *
 * @param {number|null} failAt       pending 快照里的连败数(默认取当前 failStreak)
 * @param {string|null} stampKey     额外要戳的冷却键名(如 'lastFailReminderAt')
 * @param {string} cooldownKey       冷却以哪个键为准(fail-loop 用独立冷却)
 */
function fire(state, cfg, now, kind, text, cooldownMs, failAt = null,
              stampKey = null, cooldownKey = 'lastReminderAt') {
  if (!cfg.enabled) return null;
  if (state.reminders >= cfg.maxReminders) return null;
  if (now - (state[cooldownKey] ?? 0) < cooldownMs) return null;
  state.reminders += 1;
  state.lastReminderAt = now;
  if (stampKey) state[stampKey] = now;
  state.lastTrigger = kind;
  state.callsSinceReminder = 0;
  state.editsSinceReminder = 0;
  const cur = state.recentCmds;
  state.pendingReminders.push({
    kind,
    callsAt: state.toolCalls,
    editsAt: state.edits,
    fail: failAt ?? state.failStreak,
    failsAt: state.totalFailures ?? 0,
    norm: cur.length ? cur[cur.length - 1].n : '',
  });
  state.pendingReminders = state.pendingReminders.slice(-10);
  return text;
}

/** Adaptive cooldown per trigger kind: ineffective reminders back off, effective ones tighten. */
function kindCooldown(cfg, kind, base) {
  const stats = adaptiveStats.get(kind);
  if (!stats || stats.fired < cfg.adaptiveWarmup) return base;
  const rate = stats.effective / Math.max(1, stats.fired);
  if (rate < cfg.adaptiveLowRate) return Math.min(base * cfg.adaptiveBackoffMult, base * cfg.adaptiveBackoffCap);
  if (rate > cfg.adaptiveHighRate) return Math.max(base * cfg.adaptiveTightenMult, cfg.adaptiveTightenFloorSec * 1000);
  return base;
}

/** Settle reminders fired ≥10 calls ago: did the behavior that triggered this kind actually stop? */
function isEffective(state, p) {
  const kind = p.kind;
  if (kind === 'repeat-cmds' || kind === 'exact-repeat') {
    const cur = state.recentCmds;
    return cur.length > 0 && cur[cur.length - 1].n !== (p.norm ?? '');
  }
  if (kind === 'fail-loop') {
    // failStreak is cleared by any success; compare the cumulative failure count instead
    return (state.totalFailures ?? 0) === (p.failsAt ?? -1);
  }
  if (kind === 'no-output' || kind === 'long-run' || kind === 'edit-foldback') {
    return state.edits - p.editsAt > 0;
  }
  return state.edits - p.editsAt > 0 || (state.totalFailures ?? 0) === (p.failsAt ?? -1);
}

function evaluatePending(state, cfg) {
  if (!state.pendingReminders.length) return;
  const remaining = [];
  let changed = false;
  for (const p of state.pendingReminders) {
    if (state.toolCalls - p.callsAt < cfg.settleAfterCalls) { remaining.push(p); continue; }
    const effective = isEffective(state, p);
    const stats = adaptiveStats.get(p.kind) ?? { fired: 0, effective: 0 };
    stats.fired += 1;
    if (effective) stats.effective += 1;
    adaptiveStats.set(p.kind, stats);
    changed = true;
  }
  if (changed || remaining.length !== state.pendingReminders.length) {
    state.pendingReminders = remaining;
  }
}

/**
 * Record the file content hash after each Edit/Write; content returning to a
 * previously seen state (ping-pong edits) is fold-back grinding. Returns
 * {hit, path}, or null when the file cannot be read.
 * 必须同步读:tools/result 事件与下一次写盘之间没有任何顺序保证,异步读会把
 * 后一次写入的内容当成这一次的指纹,折返永远判不出来(Python 侧同点位也是同步读)。
 */
function recordEditHash(state, cfg, exec) {
  const input = exec?.input ?? exec?.arguments ?? exec?.args ?? exec?.toolInput;
  if (!input || typeof input !== 'object') return null;
  const rawPath = extractPath(input);
  if (!rawPath) return null;
  const path = String(rawPath);
  let hash = '';
  try {
    const content = readFileSync(path);
    hash = createHash('md5').update(content).digest('hex').slice(0, 12);
  } catch {
    return null;
  }
  const seen = state.fileHashes[path] ?? [];
  const hit = seen.length >= 1 && seen.slice(0, -1).includes(hash);
  seen.push(hash);
  state.fileHashes[path] = seen.slice(-cfg.foldbackWindow);
  const paths = Object.keys(state.fileHashes);
  if (paths.length > 16) {
    for (const k of paths.slice(0, paths.length - 16)) delete state.fileHashes[k];
  }
  return { hit, path };
}

function nudgeText(state, cfg, now, specific) {
  const lines = [
    '🔔 【AI 抬头 · 中途自我审查】时钟 ' + localHm(now)
      + ',本段 ' + minutesSince(state, now).toFixed(0) + ' 分钟/'
      + state.callsSinceReminder + ' 次调用,修改 ' + state.editsSinceReminder + ' 次。停一下,自查:',
    '① 对照最初目标是否偏移?② 最近 5 次调用有无新信息?若无 → 正在空跑。',
  ];
  if (specific) lines.push('③ ' + specific);
  lines.push('→ 一句话结论:继续 / 换方法 / 先向用户汇报,然后再继续。');
  const nextRem = state.reminders + 1;
  if (nextRem >= cfg.escalateAfterReminders) {
    lines.push('⚠️ 第 ' + nextRem + ' 次提醒仍无改善——停止当前方法,直接向用户汇报卡点。');
  }
  const rl = reportLine(cfg);
  if (rl) lines.push(rl);
  return lines.join('\n');
}

const clockText = (state, now) => '🕐 【AI 抬头 · 本地时钟】' + localHm(now)
  + ',已进行 ' + minutesSince(state, now).toFixed(0) + ' 分钟(开始于 ' + localHm(state.startedAt)
  + ')。耗时预估/汇报以此为准,勿自估。';

/** Shared reset point for both milestone triggers: either one firing zeroes both counters. */
function replanReset(st) {
  st.replanRounds = 0;
  st.callsAtReplan = st.toolCalls;
  st.replans += 1;
}

/** Milestone reminder text; rounds/steps are the true accumulation since the last re-plan. */
function replanText(rounds, steps, cfg) {
  const lines = [
    '🧭 【AI 抬头 · 轮次里程碑】已连续 ' + rounds + ' 个回合 / ' + steps
      + ' 次调用没做全局重规划。停一下,先重新规划全局:',
    '① 跳出当前子问题,回到最初目标:整体进展到哪一步?剩余路径有哪些?',
    '② 检视当前方法是否在钻牛角尖(局部最优):列出 2~3 条备选路线并比较;',
    '③ 若当前路线仍是最优,一句话说明依据后继续;否则换路线,或向用户汇报卡点。',
  ];
  if (rounds >= 2 * cfg.roundReplanRounds || steps >= 2 * cfg.stepReplanCalls) {
    lines.push('⚠️ 本里程碑拖到阈值的两倍才触发——上次的重规划提醒八成被无视了。'
      + '本轮必须先规划再动手;若上轮规划后仍走的原路线,这次请直接向用户汇报卡点。');
  }
  const rl = reportLine(cfg);
  if (rl) lines.push(rl);
  return lines.join('\n');
}

/** 用户可见性呈报指令(与 Python 侧 report_line 同一文案)。 */
function reportLine(cfg) {
  if (!cfg.showToUser) return '';
  return '📣 【呈报】请在你的下一次可见回复开头,用一行向用户简报本提醒:'
    + '「AI 抬头提醒了我:<触发原因>,我接下来:<一句话动作>」。仅此一行,不要展开。';
}

function buildReviewMaterial(state, cfg, now, kind, detail) {
  const log = state.recentLog;
  // 先按"连续同工具段"折叠、再取最近 N 段:关键旧证据不因新同形刷屏被挤出窗口
  const runs = [];
  for (const e of log) {
    if (runs.length && runs[runs.length - 1][0].tool === e.tool) runs[runs.length - 1].push(e);
    else runs.push([e]);
  }
  const kept = runs.slice(-cfg.reviewLogSize);
  const logLines = kept.map((run) => {
    const t = run[0].tool ?? '?';
    const fails = run.filter((e) => !e.ok).length;
    const head = (run[0].brief || '无').slice(0, 40);
    const tail = (run[run.length - 1].brief || '').slice(0, 40);
    let line;
    if (run.length > 1) {
      const mark = fails ? '✗' + fails + '/' + run.length + ', ' : '';
      line = t + '×' + run.length + '(' + mark + head + ' … ' + tail + ')';
    } else {
      line = '[' + t + (fails ? ' ✗' : '') + '] ' + head;
    }
    if (fails) {
      // 失败段带上最后一条错误摘要:审查者不必只凭"某命令失败了"猜根因
      const lastErr = [...run].reverse().find((e) => e.err)?.err;
      if (lastErr) line += ' [错误: ' + lastErr + ']';
    }
    return line;
  });
  const dist = [...Object.entries(state.byTool)].sort((a, b) => b[1] - a[1]).slice(0, 4)
    .map(([k, v]) => k + '×' + v).join(', ');
  const ig = state.initialGoal || '';
  const lg = state.goal || '';
  const taskLine = (ig && lg && ig !== lg)
    ? '【任务】' + clipGoal(ig) + ' (当前指令: ' + clipGoal(lg, 60, 40) + ')'
    : '【任务】' + clipGoal(lg || ig);
  return [
    '【审查材料】触发: ' + kind,
    taskLine,
    '【统计】' + minutesSince(state, now).toFixed(0) + ' 分钟/' + state.toolCalls
      + ' 次调用/修改 ' + state.edits + ',连败 ' + state.failStreak + ',累计败 '
      + state.totalFailures + '。',
    '【分布】' + (dist || '无'),
    '【最近调用(旧→新)】',
    ...logLines,
    detail ? '【细节】' + detail : '',
  ].filter(Boolean).join('\n');
}

function parseVerdict(text) {
  const start = text.indexOf('{');
  const end = text.lastIndexOf('}');
  if (start < 0 || end <= start) return null;
  try {
    const obj = JSON.parse(text.slice(start, end + 1));
    if (!VERDICT_LABEL[obj.verdict]) return null;
    return {
      verdict: obj.verdict,
      reason: String(obj.reason ?? '').slice(0, 300),
      suggestion: String(obj.suggestion ?? '').slice(0, 300),
    };
  } catch {
    return null;
  }
}

const verdictText = (v) => (v.verdict === 'on-track')
  ? '🔎 【AI 抬头 · 独立审查】仍在正轨(' + (v.reason || '无异常') + ')——保持节奏。'
  : '🔎 【AI 抬头 · 独立审查】' + (VERDICT_LABEL[v.verdict] ?? v.verdict)
    + '(' + (v.reason || '未给出') + ')建议:' + (v.suggestion || '未给出');

/**
 * Build one injectable user message. `agent.inject()` and
 * `agent.inbox.prepend()` both take a UserMessage; a bare string is stored
 * verbatim as an `agent/inbox/spliced` payload that the session reader
 * rejects (history load fails) and the live loop cannot interpret.
 */
function nudgeMessage(text) {
  return {
    id: randomUUID(),
    role: 'user',
    content: [{ type: 'text', text }],
    source: { kind: 'plugin:ai-look-up' },
  };
}

/**
 * Deliver a nudge. `agent.inject()` is the documented mid-run channel; the
 * inbox prepend is the fallback for agents without an inject entry point.
 */
function deliver(agent, ctx, text) {
  const message = nudgeMessage(text);
  try {
    if (typeof agent.inject === 'function') {
      agent.inject(message);
      return true;
    }
  } catch (error) {
    ctx?.logger?.warn?.('[ai-look-up] agent.inject failed: %o', error);
  }
  try {
    if (agent.inbox && typeof agent.inbox.prepend === 'function') {
      agent.inbox.prepend('next-step', message);
      return true;
    }
  } catch (error) {
    ctx?.logger?.warn?.('[ai-look-up] inbox prepend failed: %o', error);
  }
  return false;
}

// ─────────────────────────────────────────────────────────────────────────────
// 第三通道:投递请求 + 独立 watcher（v0.7）
// 协议契约见 hooks/REVIEW_CHANNEL.md。钩子只负责「写请求单 / 取回结论」,
// 调 LLM 由宿主外面的独立 watcher 进程完成——主 agent 全程不在链路上。
// ─────────────────────────────────────────────────────────────────────────────

/** 投递区目录。与 Python 钩子共用同一布局（同 env 覆盖键,便于测试指向临时目录）。 */
function channelStateDir() {
  const base = process.env.LOOKUP_STATE_DIR || join(tmpdir(), 'zcode-ai-look-up');
  return base;
}

function reviewDir() {
  const d = join(channelStateDir(), 'review');
  try { mkdirSync(d, { recursive: true }); } catch { /* best-effort */ }
  return d;
}

/** 文件名安全化:request_id 里可能带路径分隔符或非 ASCII。 */
const idSafe = (s) => String(s ?? '').replace(/[^A-Za-z0-9_.-]/g, '_').slice(0, 160);

function safeUnlink(path) {
  try { rmSync(path, { force: true }); } catch { /* best-effort */ }
}

/**
 * 投递一次末档审查。返回 requestId,或 null(未投)。
 * 幂等:同一 requestId 的请求单已存在则跳过——同一场景重复触发天然去重。
 */
function deliverReviewRequest(cfg, st, now, kind, detail) {
  if (!cfg.deliverReview) return null;
  if (!String(cfg.llmApiKey ?? '').trim()) return null;
  const sid = st.sessionId || 'default';
  const rid = idSafe(sid + '-' + st.toolCalls + '-' + kind);
  const path = join(reviewDir(), 'req_' + rid + '.json');
  try {
    readFileSync(path);          // 已投递过 → 幂等跳过
    return rid;
  } catch { /* 不存在才继续 */ }
  const req = {
    v: 1,
    request_id: rid,
    session_id: sid,
    kind,
    detail: detail || '',
    created_at: now / 1000,      // 秒,与 Python 侧一致
    tool_calls: st.toolCalls,
    prompt_resets: st.promptResets,
    goal_snapshot: clipGoal(st.goal),
    system: REVIEW_SYSTEM,
    material: buildReviewMaterial(st, cfg, now, kind, detail),
  };
  try {
    const tmp = path + '.tmp';
    writeFileSync(tmp, JSON.stringify(req, null, 1), 'utf8');
    renameSync(tmp, path);       // 原子落盘:watcher 不会读到半截文件
  } catch {
    return null;
  }
  return rid;
}

/** 把结论格式化成注入文本。必须标注场景,不能只说"N 步前"。 */
function formatReviewResult(res, stepsBack, cfg = null) {
  const label = VERDICT_LABEL[res.verdict] ?? res.verdict ?? '';
  const lines = [
    '🎯 【AI 抬头 · 独立审查者】判定: ' + label,
    '该结论基于 ' + stepsBack + ' 步前「' + ((res.detail || '未知触发') + '').slice(0, 60) + '」的现场。',
    '漂移表现: ' + (res.reason || '(未给出)'),
    '收束建议: ' + (res.suggestion || '(未给出)'),
    '请对照最初任务,决定:把当前子任务收敛回主线 / 明确其为目标之一并告知用户 / 放弃。',
  ];
  const rl = cfg ? reportLine(cfg) : '';
  if (rl) lines.push(rl);
  return lines.join('\n');
}

/**
 * 扫投递区取回结论。返回待注入文本数组(可能为空)。
 * 三道闸(见 REVIEW_CHANNEL.md):场景指纹 / 过期 / 步数;
 * 不过闸只删文件不注入(静默作废)。
 */
function takeReviewResults(st, cfg, now) {
  const texts = [];
  let names;
  try { names = readdirSync(reviewDir()); } catch { return texts; }
  const sid = st.sessionId || 'default';
  for (const name of names) {
    if (!name.startsWith('res_') || !name.endsWith('.json')) continue;
    const p = join(reviewDir(), name);
    let res;
    try { res = JSON.parse(readFileSync(p, 'utf8')); } catch { safeUnlink(p); continue; }
    if (!res || typeof res !== 'object') { safeUnlink(p); continue; }
    // 不属于本会话的结论不要碰(可能是另一个会话的)
    if (res.session_id && String(res.session_id) !== String(sid)) continue;
    // 闸1: 场景指纹——用户换过指令则结论作废
    if ((res.prompt_resets ?? 0) !== (st.promptResets ?? 0)) { safeUnlink(p); continue; }
    // 闸2: 过期。finished_at 缺失或非数值一律作废——不可验证时效的结论不注入。
    // (Number('abc') 为 NaN,NaN > ttl 恒为 false,会把坏数据放过去,故显式判 finite。)
    const finishedAt = Number(res.finished_at);
    if (!Number.isFinite(finishedAt) || now / 1000 - finishedAt > cfg.resultTtlSec) { safeUnlink(p); continue; }
    // 闸3: 步数——结论对应几十步前的现场,AI 已走远
    const stepsBack = st.toolCalls - Number(res.tool_calls ?? 0);
    if (stepsBack > cfg.resultSettleCalls) { safeUnlink(p); continue; }
    if (res.verdict === 'error') { safeUnlink(p); continue; }  // 失败不注入,静默丢弃
    texts.push(formatReviewResult(res, Math.max(0, stepsBack), cfg));
    safeUnlink(p);
  }
  return texts;
}

/**
 * Independent reviewer on the optional `llm` service. The stream chunk shape
 * (text blocks + terminal finish) follows the shipped auto-review plugin;
 * BlockAssembler is intentionally not imported so this package stays
 * dependency-free and degrades to the static checklist without the service.
 */
function makeReviewer(ctx) {
  let llm = null;
  // cordis 系宿主对未声明注入的服务访问会直接抛错(cannot get property "llm"
  // without inject),必须把直读放进 try 内;抛错则降级到下面的 inject 路径。
  try {
    llm = (ctx && typeof ctx.llm === 'object') ? ctx.llm : null;
  } catch { /* cordis: service access outside inject is forbidden */ }
  try {
    if (llm === null && typeof ctx?.inject === 'function') {
      // inject 回调签名随 cordis 版本可能是 (svc) 或 (ctx2, svc),两种都兜住:
      // 参数不像服务就回退到注入上下文内的 ctx.llm 直读(注入作用域内合法)。
      ctx.inject(['llm'], (a, b) => {
        const svc = (a && typeof a.stream === 'function') ? a
          : (b && typeof b.stream === 'function') ? b : null;
        if (svc) { llm = svc; return; }
        try {
          const viaCtx = (ctx && typeof ctx.llm === 'object') ? ctx.llm : null;
          if (viaCtx) llm = viaCtx;
        } catch { /* service not ready yet */ }
      });
    }
  } catch (error) {
    ctx?.logger?.warn?.('[ai-look-up] llm service injection failed: %o', error);
  }
  return {
    get ready() { return llm !== null; },
    async review(agent, material, signal, system = REVIEW_SYSTEM) {
      const header = agent?.session?.requestHeader?.();
      const provider = header?.config?.provider ?? '';
      const model = header?.config?.model ?? '';
      if (llm === null || typeof llm.stream !== 'function') throw new Error('llm service unavailable');
      if (!provider || !model) throw new Error('no request-header route on this session');
      const options = {
        provider, model,
        system,
        messages: [{ role: 'user', content: [{ type: 'text', text: material }] }],
        temperature: 0,
        signal,
      };
      let text = '';
      for await (const chunk of llm.stream(options)) {
        if (chunk?.type === 'text' && typeof chunk.text === 'string') text += chunk.text;
        if (chunk?.type === 'finish') {
          const kind = chunk.reason?.kind;
          if (kind === 'error' || kind === 'aborted') {
            throw new Error('reviewer stream ended with ' + kind
              + ': ' + (chunk.reason?.failure?.message ?? ''));
          }
        }
      }
      if (!text.trim()) throw new Error('reviewer returned no text');
      return text;
    },
  };
}

export function apply(ctx, config) {
  const cfg = mergeConfig(config);
  const reviewer = makeReviewer(ctx);
  const state = new Map(); // agent object -> session state
  let agentMissingWarned = false; // tools/result 不挂 agent 时只警告一次
  // 仅供测试读取会话状态的探测孔：宿主不设 __testState 时完全不暴露。
  if (ctx && ctx.__testState) {
    try { ctx.__testState(state); } catch { /* 探测孔失败不影响运行 */ }
  }

  const stateOf = (agent) => {
    let st = state.get(agent);
    if (st === undefined) {
      st = freshState();
      state.set(agent, st);
    }
    return st;
  };

  /**
   * Deliver a fired reminder: through the independent reviewer when possible,
   * otherwise the static checklist. Never blocks the tool-result stream; the
   * review lands via agent.inject() when it completes.
   */
  const deliverOrReview = (agent, st, kind, detail, fallbackText, system = REVIEW_SYSTEM) => {
    const now = Date.now();
    // 第三通道投递:只在升级阶梯末档(提醒了还不改)才投——把作弊面从
    // "内容可篡改"压到"行为可跳过",且成本受控(多数会话到不了这一档)。
    if (st.reminders >= cfg.escalateAfterReminders) {
      deliverReviewRequest(cfg, st, now, kind, detail);
    }
    if (cfg.llmReview && reviewer.ready && !st.reviewInFlight) {
      st.reviewInFlight = true;
      const material = buildReviewMaterial(st, cfg, now, kind, detail);
      const signal = typeof AbortSignal !== 'undefined' && AbortSignal.timeout
        ? AbortSignal.timeout(cfg.llmTimeoutMs) : undefined;
      reviewer.review(agent, material, signal, system).then((raw) => {
        st.reviewInFlight = false;
        const verdict = parseVerdict(raw);
        if (!verdict) { deliver(agent, ctx, fallbackText); return; }
        if (verdict.verdict === 'on-track') {
          // 机械证据型忽略 on-track(触发条件本身即"非正轨"证据),模糊型用短确认
          deliver(agent, ctx, FUZZY_KINDS.has(kind) ? verdictText(verdict) : fallbackText);
          return;
        }
        // 非 on-track:机械证据型保留完整静态清单并附判定(与 ZCode 对齐);模糊型整体替换
        deliver(agent, ctx, FUZZY_KINDS.has(kind)
          ? verdictText(verdict)
          : fallbackText + '\n' + verdictText(verdict));
      }).catch((error) => {
        st.reviewInFlight = false;
        ctx?.logger?.warn?.('[ai-look-up] reviewer fell back to static checklist: %o', error);
        deliver(agent, ctx, fallbackText);
      });
      return;
    }
    deliver(agent, ctx, fallbackText);
  };

  ctx.on('agent/created', (agent) => {
    state.set(agent, freshState());
    if (cfg.lessons) {
      const known = lessonsMessage(stateOf(agent), false, cfg);
      if (known) deliver(agent, ctx, known);
    }
    try {
      agent.ctx?.effect?.(() => () => state.delete(agent));
    } catch (error) {
      ctx?.logger?.warn?.('[ai-look-up] agent cleanup registration failed: %o', error);
    }
  });

  const guardExec = (exec) => {
    const tool = toolNameOf(exec);
    if (!isShellTool(tool)) return null;
    const currentEdits = exec?.agent ? (stateOf(exec.agent)?.edits ?? null) : null;
    const hit = checkLessonGuard(extractCmd(exec), cfg, currentEdits);
    if (!hit) return null;
    const { action, entry } = hit;
    if (action === 'block') {
      const reason = [
        '🛑 【AI 抬头 · 坑位拦截】命令命中已登记 ' + entry.hits + ' 次的失败坑位 `' + entry.pattern + '`，已阻止重复执行。',
        '历史教训: ' + (entry.correction || ''),
        '请先修改命令参数/脚本逻辑或换一种方法；若确需绕过，请先向用户说明根因。',
        reportLine(cfg),
      ].filter(Boolean).join('\n');
      return { decision: 'block', reason };
    }
    if (action === 'warn' && exec?.agent) {
      deliver(exec.agent, ctx, [
        '⚠️ 【AI 抬头 · 坑位预警】该命令归一化模式 `' + entry.pattern + '` 曾触发失败循环(' + entry.hits + ' 次)。',
        '历史教训: ' + (entry.correction || ''),
      ].join('\n'));
    }
    return null;
  };
  try {
    if (typeof ctx?.tools?.guard === 'function') {
      ctx.tools.guard((exec) => guardExec(exec));
    }
  } catch { /* cordis: undeclared service access may throw */ }
  ctx.on('tools/guard', (exec) => guardExec(exec));

  ctx.on('session/event', (session, event) => {
    try {
      if (event?.type !== 'user/message' && event?.type !== 'turn/end') return;
      let agent = null;
      for (const [key] of state) {
        if (key?.session === session) { agent = key; break; }
      }
      if (agent === null) return;
      const st = stateOf(agent);
      const now = Date.now();
      if (now - st.lastEventAt > STALE_AFTER_MS) resetStretch(st, cfg, now);
      if (event.type === 'user/message') {
        st.promptResets += 1;
        resetStretch(st, cfg, now);
        // capture the task description for the reviewer
        try {
          const content = event?.data?.content;
          if (Array.isArray(content)) {
            const block = content.find((b) => b?.type === 'text' && typeof b.text === 'string');
            if (block) updateGoals(st, block.text);
          } else if (typeof event?.data?.text === 'string') {
            updateGoals(st, event.data.text);
          }
        } catch { /* goal capture is best-effort */ }
        // 回合里程碑:连续 N 个回合未做全局重规划 → 新回合开始时强制抬头。
        // 步数下限挡住纯寒暄轮;计数跨回合累计,只有真正触发才复位(replanReset)。
        if (cfg.enabled && cfg.roundReplan) {
          st.replanRounds += 1;
          const steps = st.toolCalls - st.callsAtReplan;
          if (st.replanRounds >= cfg.roundReplanRounds
              && steps >= cfg.roundReplanMinCalls) {
            const rounds = st.replanRounds;
            replanReset(st);
            deliver(agent, ctx, replanText(rounds, steps, cfg));
          }
        }
        // 会话中途注册的坑位也要有投递点:每 5 个回合强制重注一次
        if (cfg.lessons) {
          const known = lessonsMessage(st, st.promptResets % 5 === 0, cfg);
          if (known) deliver(agent, ctx, known);
        }
        return;
      }
      // turn/end: wrap-up review for long, outputless sessions
      if (!cfg.stopCheck || !cfg.enabled) return;
      if (st.stopBlocks >= 2) return;
      if (st.edits === 0 && st.toolCalls >= cfg.stopMinCalls
          && minutesSince(st, now) >= cfg.longRunMinutes) {
        st.stopBlocks += 1;
        const elapsed = minutesSince(st, now);
        resetStretch(st, cfg, now);
        const text = [
          '🔔 【AI 抬头 · 结束前审查】这轮会话累计 ' + st.toolCalls + ' 次工具调用、约 '
            + elapsed.toFixed(0) + ' 分钟,但没有任何文件被修改。在结束回复之前,请先:',
          '1. 明确向用户汇报:你做了什么尝试、卡在哪里、下一步建议是什么;',
          '2. 如果还有未完成的检查,先完成再结束;',
          '3. 不要默默结束一段没有产出的长时间运行。',
          reportLine(cfg),
        ].filter(Boolean).join('\n');
        deliverOrReview(agent, st, 'stop-check', '会话结束时累计 ' + st.toolCalls + ' 次调用零修改', text);
      }
    } catch (error) {
      ctx?.logger?.warn?.('[ai-look-up] session event handling failed: %o', error);
    }
  });

  ctx.on('tools/result', (exec, result) => {
    try {
      const agent = exec?.agent;
      if (agent === undefined || agent === null) {
        // 宿主没往 exec 上挂 agent:整套检测静默失明。一次性留证,不刷屏。
        if (!agentMissingWarned) {
          agentMissingWarned = true;
          ctx?.logger?.warn?.('[ai-look-up] tools/result 未携带 agent 引用,行为检测不会触发;请检查宿主版本的事件载荷。');
        }
        return;
      }
      if (exec?.signal?.aborted) return;
      const st = stateOf(agent);
      const now = Date.now();
      if (now - st.lastEventAt > STALE_AFTER_MS) resetStretch(st, cfg, now);
      st.lastEventAt = now;

      // 第三通道取回:把独立 watcher 的结论捎带注入。
      // 只取回文本、不 return——否则本次调用不被计数,toolCalls 会与实际脱节。
      if (cfg.deliverReview) {
        const verdicts = takeReviewResults(st, cfg, now);
        if (verdicts.length) deliver(agent, ctx, verdicts.join('\n'));
      }

      const tool = toolNameOf(exec);
      st.toolCalls += 1;
      st.callsSinceReminder += 1;
      st.byTool[tool] = (st.byTool[tool] ?? 0) + 1;

      if (isProductiveTool(tool)) {
        st.edits += 1;
        st.editsSinceReminder += 1;
        if (cfg.editFoldback) {
          try {
            const res = recordEditHash(st, cfg, exec);
            // ★ 注意:res 是 {hit, path} 对象或 null——必须取 .hit 且判 null,
            //   否则每次改动都会误判为折返(与 Python 侧 record_edit_hash 返回布尔的语义对齐)。
            if (res?.hit && cfg.enabled) {
              const nowFb = Date.now();
              const text = fire(st, cfg, nowFb, 'edit-foldback', nudgeText(st, cfg, nowFb,
                '文件 ' + res.path + ' 的内容回到了先前见过的状态——改了又改回是典型的原地打转,'
                + '请确认这条修改路径是否还有意义。'), kindCooldown(cfg, 'edit-foldback', cfg.cooldownSec * 1000));
              if (text) deliverOrReview(agent, st, 'edit-foldback',
                '文件内容折返: ' + res.path, text);
            }
          } catch { /* fold-back detection is best-effort */ }
        }
      } else if (isShellTool(tool)) {
        const cmd = extractCmd(exec);
        const n = normCmd(cmd);
        const r = rawCmd(cmd);
        if (n || r) {
          st.recentCmds.push({ n, r });
          st.recentCmds = st.recentCmds.slice(-cfg.maxRecentCmds);
        }
      } else {
        const [sigN, sigR] = nonShellRepeatSig(tool, exec);
        if (sigN || sigR) {
          st.recentCmds.push({ n: sigN, r: sigR });
          st.recentCmds = st.recentCmds.slice(-cfg.maxRecentCmds);
        }
      }

      const isError = result?.isError === true;
      const failBefore = st.failStreak;   // 成功路径会在下面清零,先存快照供 fire() 用
      if (isError) {
        st.failStreak += 1;
        st.totalFailures += 1;
        // 夹心式失败状态机:同归一化命令在无修改、无成功期间的连败 + 穿插只读段
        const curNorm = isShellTool(tool) ? normCmd(extractCmd(exec)) : '';
        if (curNorm && curNorm === st.cmdFailNorm) {
          st.cmdFailStreak += 1;
        } else {
          st.cmdFailNorm = curNorm;
          st.cmdFailStreak = curNorm ? 1 : 0;
          st.cmdFailInterleaves = 0;
        }
        st.lastWasCmdFail = Boolean(curNorm);
        const failEntry = { tool, brief: briefOf(exec, extractCmd(exec), tool), ok: false };
        const err = extractErr(result);
        if (err) failEntry.err = err;
        st.recentLog.push(failEntry);
        st.recentLog = st.recentLog.slice(-Math.max(cfg.reviewLogFloor, cfg.reviewLogSize));
        const interleavedHit = st.cmdFailInterleaves >= 2
          && st.cmdFailStreak >= cfg.failStreakThreshold;
        // ★ v0.8:fail-loop 收编进 fire()。门槛(连败阈值)留在这里,
        //   其余状态维护全部交给 fire()——此前这里是手工复刻(F1 即漏抄)。
        //   冷却基准 lastFailReminderAt 独立于其它触发器,故 cooldownKey 与 stampKey 都指向它。
        if (st.failStreak >= cfg.failStreakThreshold || interleavedHit) {
          // 夹心触发时 failStreak 可能被只读成功清零过,对外报数取两者较大值
          const streak = Math.max(st.failStreak, st.cmdFailStreak);
          let text = fire(st, cfg, now, 'fail-loop', [
            '🔔 【AI 抬头 · 失败循环】你已连续失败 ' + streak + ' 次。请勿再用同样的方式重试:',
            '1. 完整读取最近一次的错误信息,定位根因(而不是只看表面症状);',
            '2. 判断:这是可以修复的问题,还是方法本身不可行?',
            '3. 换方法、修复后再试;若连续两次换方法仍失败,停下来向用户汇报卡点。',
          ].join('\n'), kindCooldown(cfg, 'fail-loop', cfg.failCooldownSec * 1000),
          streak, 'lastFailReminderAt', 'lastFailReminderAt');
          if (text) {
            if (st.reminders >= cfg.escalateAfterReminders) {
              text += '\n⚠️ 这已是本会话第 ' + st.reminders + ' 次提醒。请考虑完全放弃当前路径,直接向用户汇报。';
            }
            const rl = reportLine(cfg);
            if (rl) text += '\n' + rl;
            deliverOrReview(agent, st, 'fail-loop',
              '连续失败 ' + streak + ' 次', text);
            if (cfg.lessons) {
              const lastFail = [...st.recentLog].reverse().find((e) => !e.ok);
              if (lastFail) {
                registerLesson(normCmd(lastFail.brief) || '未知模式',
                  '连续失败 ' + streak + ' 次后被打断——换方法前请先读错误定位根因,勿重复此方式',
                  lastFail.brief, st.edits);
              }
            }
            st.failStreak = 0;
            // 触发即闭环:夹心计数一并归零,下一轮从干净状态重新累计
            st.cmdFailNorm = '';
            st.cmdFailStreak = 0;
            st.cmdFailInterleaves = 0;
            st.lastWasCmdFail = false;
            return;
          }
        }
      } else {
        st.failStreak = 0;
        // 成功事件:按工具类型终结夹心连败状态
        if (isProductiveTool(tool)) {
          // 有代码修改 → 不是原地盲试,夹心连败计数整体失效
          st.cmdFailNorm = '';
          st.cmdFailStreak = 0;
          st.cmdFailInterleaves = 0;
          st.lastWasCmdFail = false;
        } else if (isShellTool(tool)) {
          // 命令跑通了 → 连败终结,同时从账本里消掉一次对应坑位
          st.cmdFailNorm = '';
          st.cmdFailStreak = 0;
          st.cmdFailInterleaves = 0;
          st.lastWasCmdFail = false;
          if (cfg.lessons) redeemLesson(extractCmd(exec), cfg);
        } else if (st.cmdFailStreak > 0 && st.lastWasCmdFail) {
          // 只读工具成功:若刚发生同命令失败,记一次"穿插只读段"
          st.cmdFailInterleaves += 1;
          st.lastWasCmdFail = false;
        }
        st.recentLog.push({ tool, brief: briefOf(exec, extractCmd(exec), tool), ok: true });
        // raw window is generous; run-compression happens at material build time
        st.recentLog = st.recentLog.slice(-Math.max(cfg.reviewLogFloor, cfg.reviewLogSize));
      }

      if (!cfg.enabled) return;

      evaluatePending(st, cfg);

      // triggers, strongest evidence first
      if (st.callsSinceReminder >= cfg.repeatMinCalls) {
        const exact = exactRepeatHit(st, cfg);
        if (exact) {
          const text = fire(st, cfg, now, 'exact-repeat', nudgeText(st, cfg, now,
            '完全相同的命令已原样执行 ' + exact.count + ' 次:`' + exact.raw.slice(0, 120)
            + '`。同样的输入必然得到同样的结果——请换参数、换方法,或停下来重新评估。'), kindCooldown(cfg, 'exact-repeat', cfg.cooldownSec * 1000), failBefore);
          if (text) { deliverOrReview(agent, st, 'exact-repeat',
            '原样命令重复 ' + exact.count + ' 次: ' + exact.raw.slice(0, 100), text); return; }
        }
        const repeat = repeatHit(st, cfg);
        if (repeat) {
          const text = fire(st, cfg, now, 'repeat-cmds', nudgeText(st, cfg, now,
            '检测到重复执行:归一化后为 "' + repeat.prefix + ' …" 的命令在本段已出现 '
            + repeat.count + ' 次。同样的命令大概率得到同样的结果——请换参数、换思路,或停下来重新评估。'), kindCooldown(cfg, 'repeat-cmds', cfg.cooldownSec * 1000), failBefore);
          if (text) { deliverOrReview(agent, st, 'repeat-cmds',
            '相似命令重复 ' + repeat.count + ' 次: ' + repeat.prefix, text); return; }
        }
      }
      if (st.edits === 0 && minutesSince(st, now) >= cfg.longRunMinutes
          && st.callsSinceReminder >= cfg.longRunMinCalls) {
        const text = fire(st, cfg, now, 'long-run', nudgeText(st, cfg, now,
          '本段会话已持续约 ' + minutesSince(st, now).toFixed(0)
          + ' 分钟,还没有任何文件被修改。如果当前路径走不通,请考虑向用户说明卡点,而不是继续消耗时间。'), kindCooldown(cfg, 'long-run', cfg.cooldownSec * 1000), failBefore);
        if (text) { deliverOrReview(agent, st, 'long-run',
          '运行 ' + minutesSince(st, now).toFixed(0) + ' 分钟零修改', text); return; }
      }
      if (st.callsSinceReminder >= cfg.callNudgeInterval && st.editsSinceReminder === 0) {
        const text = fire(st, cfg, now, 'no-output', nudgeText(st, cfg, now,
          '自上次审查以来 ' + st.callsSinceReminder
          + ' 次工具调用没有产生任何文件修改——要么是在合理地调研,要么是在空跑。请用证据判断是哪一种。'), kindCooldown(cfg, 'no-output', cfg.cooldownSec * 1000), failBefore);
        if (text) { deliverOrReview(agent, st, 'no-output',
          st.callsSinceReminder + ' 次调用零修改', text); return; }
      }
      // goal-drift patrol (v0.5): every N calls, the reviewer checks whether
      // current behavior still serves the original goal; silent when on-track
      if (cfg.driftCheck && reviewer.ready
          && st.toolCalls - st.callsAtLastDrift >= cfg.driftCheckCalls
          && now - st.lastDriftAt >= cfg.driftCooldownSec * 1000) {
        st.lastDriftAt = now;
        st.callsAtLastDrift = st.toolCalls;
        st.driftChecks += 1;
        const material = buildReviewMaterial(st, cfg, now, 'goal-drift', '巡检:目标对齐');
        const driftSignal = typeof AbortSignal !== 'undefined' && AbortSignal.timeout
          ? AbortSignal.timeout(cfg.llmTimeoutMs) : undefined;
        reviewer.review(agent, material, driftSignal, REVIEW_DRIFT_SYSTEM).then((raw) => {
          const verdict = parseVerdict(raw);
          if (verdict && verdict.verdict !== 'on-track') {
            let driftText = '🎯 【AI 抬头 · 目标漂移巡检】判定: ' + (DRIFT_LABEL[verdict.verdict] ?? verdict.verdict)
              + '\n漂移表现: ' + (verdict.reason || '(未给出)')
              + '\n收束建议: ' + (verdict.suggestion || '(未给出)')
              + '\n请对照最初任务,决定:把当前子任务收敛回主线 / 明确其为目标之一并告知用户 / 放弃。';
            const rl = reportLine(cfg);
            if (rl) driftText += '\n' + rl;
            deliver(agent, ctx, driftText);
          }
        }).catch((error) => {
          ctx?.logger?.warn?.('[ai-look-up] drift patrol failed: %o', error);
        });
      }
      // step milestone (v0.9): unconditionally force a global re-plan every N calls.
      // fire() 不复位 callsAtReplan,负向提醒在中间连发也不会推迟或吞掉本里程碑。
      if (cfg.enabled && cfg.stepReplan
          && st.toolCalls - st.callsAtReplan >= cfg.stepReplanCalls) {
        const rounds = st.replanRounds;
        const steps = st.toolCalls - st.callsAtReplan;
        replanReset(st);
        deliver(agent, ctx, replanText(rounds, steps, cfg));
      }
      // local clock anchor, lowest priority; quiet within 2 minutes of a nudge
      if (cfg.clockTick && now - st.lastReminderAt >= 120000) {
        const interval = st.clockInterval || cfg.clockTickMinutes;
        const tickMs = (cfg.clockTickBackoff ? interval : cfg.clockTickMinutes) * 60000;
        if (now - st.lastClockTickAt >= tickMs && minutesSince(st, now) >= cfg.clockTickMinutes) {
          st.lastClockTickAt = now;
          st.clockTicks += 1;
          st.clockInterval = Math.min(Math.max(interval * 2, 1), cfg.clockTickMaxMinutes);
          deliver(agent, ctx, clockText(st, now));
        }
      }
    } catch (error) {
      ctx?.logger?.warn?.('[ai-look-up] tool result handling failed: %o', error);
    }
  });
}

/** Test-only internals for golden conformance checks (no runtime consumers). */
export const __test = {
  normCmd, rawCmd, defaults: { ...DEFAULTS },
  // 行为级金标探针所需的只读句柄：freshState 造干净状态、kindCooldown 是
  // adaptive_* 键的唯一执行位、adaptiveStats 是模块级统计（跨会话存活，
  // 故探针必须能读取/清空，否则用例互相污染）。
  freshState, kindCooldown, adaptiveStats,
  checkLessonGuard, registerLesson, lessonsLedger,
  redeemLesson, buildReviewMaterial, nonShellRepeatSig,
  isSubstantiveGoal, updateGoals, extractErr,
};
