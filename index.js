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
import { readFile } from 'node:fs/promises';

const PRODUCTIVE_TOOLS = new Set(['Edit', 'Write', 'MultiEdit', 'NotebookEdit', 'ApplyPatch']);

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
  // lesson ledger (v0.5): failures register pitfalls, new agents get the top ones
  lessons: true,
};

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

function registerLesson(pattern, correction) {
  if (!pattern) return;
  const prev = lessonsLedger.get(pattern);
  if (prev) {
    prev.hits += 1;
    prev.correction = correction;
  } else {
    lessonsLedger.set(pattern, { pattern, correction, hits: 1 });
  }
}

function lessonsMessage(state) {
  if (!lessonsLedger.size) return null;
  const top = [...lessonsLedger.values()].sort((a, b) => b.hits - a.hits).slice(0, 3);
  const sig = top.map((e) => e.pattern + 'x' + e.hits).join('|');
  if (state.lessonsSig === sig) return null;
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

function normCmd(cmd) {
  let s = String(cmd ?? '').trim().toLowerCase();
  if (!s) return '';
  s = s.replace(/\s+/g, ' ');
  const parts = s.split(/&&|\|\||;|\|/);
  s = (parts[parts.length - 1] || s).trim();
  const toks = s.split(' ');
  let head = (toks[0] ?? '').replace(/\\/g, '/').split('/').pop().replace(/^["']|["']$/g, '');
  head = head.replace(/\.exe$/, '');
  let second = toks.length > 1 ? toks[1] : '';
  second = second.replace(/\\/g, '/').split('/').pop().replace(/^["']|["']$/g, '');
  second = second.replace(/\.(py|js|ts|mjs|cjs|sh|json|md|txt|log|csv|yaml|yml)$/, '');
  second = second.replace(/[\d_-]+$/, '');
  return (head + ' ' + second).trim();
}

const rawCmd = (cmd) => String(cmd ?? '').trim().replace(/\s+/g, ' ');

function extractCmd(exec) {
  const input = exec?.input ?? exec?.arguments ?? exec?.args ?? exec?.toolInput;
  if (typeof input === 'string') return input;
  if (input && typeof input === 'object') {
    for (const key of ['command', 'cmd', 'script']) {
      if (typeof input[key] === 'string') return input[key];
    }
  }
  return '';
}

function toolNameOf(exec) {
  for (const key of ['name', 'toolName', 'tool']) {
    if (typeof exec?.[key] === 'string') return exec[key];
  }
  return 'Unknown';
}

function briefOf(exec, cmd) {
  if (cmd) return cmd.slice(0, 100);
  const input = exec?.input ?? exec?.arguments ?? exec?.args ?? exec?.toolInput;
  if (input === undefined || input === null) return '';
  try {
    return JSON.stringify(input).slice(0, 100);
  } catch {
    return '';
  }
}

function freshState() {
  const now = Date.now();
  return {
    startedAt: now, lastEventAt: now,
    toolCalls: 0, byTool: {},
    edits: 0, editsSinceReminder: 0, callsSinceReminder: 0,
    failStreak: 0, totalFailures: 0,
    recentCmds: [], recentLog: [],
    goal: '',
    reminders: 0, lastReminderAt: 0, lastFailReminderAt: 0, lastTrigger: '',
    stopBlocks: 0, promptResets: 0, lastClockTickAt: 0, clockTicks: 0,
    reviewInFlight: false, lessonsSig: '',
    fileHashes: {}, pendingReminders: [],
    driftChecks: 0, lastDriftAt: 0, callsAtLastDrift: 0,
    clockInterval: 0,
  };
}

function resetStretch(state, cfg, now) {
  state.startedAt = now;
  state.callsSinceReminder = 0;
  state.editsSinceReminder = 0;
  state.failStreak = 0;
  state.recentCmds = [];
  state.clockInterval = 0;
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

function fire(state, cfg, now, kind, text, cooldownMs) {
  if (!cfg.enabled) return null;
  if (state.reminders >= cfg.maxReminders) return null;
  if (now - state.lastReminderAt < cooldownMs) return null;
  state.reminders += 1;
  state.lastReminderAt = now;
  state.lastTrigger = kind;
  state.callsSinceReminder = 0;
  state.editsSinceReminder = 0;
  const cur = state.recentCmds;
  state.pendingReminders.push({
    kind,
    callsAt: state.toolCalls,
    editsAt: state.edits,
    fail: state.failStreak,
    norm: cur.length ? cur[cur.length - 1].n : '',
  });
  state.pendingReminders = state.pendingReminders.slice(-10);
  return text;
}

/** Adaptive cooldown per trigger kind: ineffective reminders back off, effective ones tighten. */
function kindCooldown(kind, base) {
  const stats = adaptiveStats.get(kind);
  if (!stats || stats.fired < 5) return base;
  const rate = stats.effective / Math.max(1, stats.fired);
  if (rate < 0.3) return Math.min(base * 2, base * 4);
  if (rate > 0.7) return Math.max(base * 0.75, 60000);
  return base;
}

/** Settle reminders fired ≥10 calls ago: an edit, a stopped failure streak, or a changed command pattern counts as effective. */
function evaluatePending(state) {
  if (!state.pendingReminders.length) return;
  const remaining = [];
  let changed = false;
  for (const p of state.pendingReminders) {
    if (state.toolCalls - p.callsAt < 10) { remaining.push(p); continue; }
    let effective = (state.edits - p.editsAt > 0) || (state.failStreak === 0 && p.fail > 0);
    if (!effective) {
      const cur = state.recentCmds;
      const last = cur.length ? cur[cur.length - 1].n : '';
      effective = Boolean(last) && last !== p.norm;
    }
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
 */
async function recordEditHash(state, cfg, exec) {
  const input = exec?.input ?? exec?.arguments ?? exec?.args ?? exec?.toolInput;
  if (!input || typeof input !== 'object') return null;
  const rawPath = input.file_path ?? input.path ?? input.notebook_path;
  if (!rawPath) return null;
  const path = String(rawPath);
  let hash = '';
  try {
    const content = await readFile(path);
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
      + state.callsSinceReminder + ' 次调用,修改 ' + state.editsSinceReminder + ' 次。自查:',
    '① 对照最初目标是否偏移?② 最近 5 次调用有无新信息?若无 → 正在空跑。',
  ];
  if (specific) lines.push('③ ' + specific);
  lines.push('→ 一句话结论:继续 / 换方法 / 先向用户汇报,然后再继续。');
  if (state.reminders >= 3) {
    lines.push('⚠️ 第 ' + (state.reminders + 1) + ' 次提醒仍无改善——停止当前方法,直接向用户汇报卡点。');
  }
  return lines.join('\n');
}

const clockText = (state, now) => '🕐 【AI 抬头 · 本地时钟】' + localHm(now)
  + ',已进行 ' + minutesSince(state, now).toFixed(0) + ' 分钟(开始于 ' + localHm(state.startedAt)
  + ')。耗时预估/汇报以此为准,勿自估。';

function buildReviewMaterial(state, cfg, now, kind, detail) {
  const logLines = state.recentLog.slice(-Math.min(8, cfg.reviewLogSize)).map((entry, i) =>
    (i + 1) + '. [' + entry.tool + (entry.ok ? '' : ' ✗') + '] ' + (entry.brief || '无').slice(0, 60));
  return [
    '【审查材料】触发: ' + kind,
    '【任务】' + (state.goal || '(未捕获)').slice(0, 200),
    '【统计】' + minutesSince(state, now).toFixed(0) + ' 分钟/' + state.toolCalls
      + ' 次调用/修改 ' + state.edits + ',连败 ' + state.failStreak + ',累计败 '
      + state.totalFailures + '。',
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

/**
 * Independent reviewer on the optional `llm` service. The stream chunk shape
 * (text blocks + terminal finish) follows the shipped auto-review plugin;
 * BlockAssembler is intentionally not imported so this package stays
 * dependency-free and degrades to the static checklist without the service.
 */
function makeReviewer(ctx) {
  let llm = (ctx && typeof ctx.llm === 'object') ? ctx.llm : null;
  try {
    if (llm === null && typeof ctx?.inject === 'function') {
      ctx.inject(['llm'], (svc) => { llm = svc; });
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
      if (llm === null) throw new Error('llm service unavailable');
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
    if (cfg.llmReview && reviewer.ready && !st.reviewInFlight) {
      st.reviewInFlight = true;
      const material = buildReviewMaterial(st, cfg, now, kind, detail);
      const signal = typeof AbortSignal !== 'undefined' && AbortSignal.timeout
        ? AbortSignal.timeout(cfg.llmTimeoutMs) : undefined;
      reviewer.review(agent, material, signal, system).then((raw) => {
        st.reviewInFlight = false;
        const verdict = parseVerdict(raw);
        deliver(agent, ctx, verdict ? verdictText(verdict) : fallbackText);
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
      const known = lessonsMessage(stateOf(agent));
      if (known) deliver(agent, ctx, known);
    }
    try {
      agent.ctx?.effect?.(() => () => state.delete(agent));
    } catch (error) {
      ctx?.logger?.warn?.('[ai-look-up] agent cleanup registration failed: %o', error);
    }
  });

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
            if (block) st.goal = block.text.slice(0, 400);
          } else if (typeof event?.data?.text === 'string') {
            st.goal = event.data.text.slice(0, 400);
          }
        } catch { /* goal capture is best-effort */ }
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
        ].join('\n');
        deliverOrReview(agent, st, 'stop-check', '会话结束时累计 ' + st.toolCalls + ' 次调用零修改', text);
      }
    } catch (error) {
      ctx?.logger?.warn?.('[ai-look-up] session event handling failed: %o', error);
    }
  });

  ctx.on('tools/result', (exec, result) => {
    try {
      const agent = exec?.agent;
      if (agent === undefined || agent === null) return;
      if (exec?.signal?.aborted) return;
      const st = stateOf(agent);
      const now = Date.now();
      if (now - st.lastEventAt > STALE_AFTER_MS) resetStretch(st, cfg, now);
      st.lastEventAt = now;

      const tool = toolNameOf(exec);
      st.toolCalls += 1;
      st.callsSinceReminder += 1;
      st.byTool[tool] = (st.byTool[tool] ?? 0) + 1;

      if (PRODUCTIVE_TOOLS.has(tool)) {
        st.edits += 1;
        st.editsSinceReminder += 1;
        if (cfg.editFoldback) {
          recordEditHash(st, cfg, exec).then((hit) => {
            if (!hit || !cfg.enabled) return;
            const nowFb = Date.now();
            const text = fire(st, cfg, nowFb, 'edit-foldback', nudgeText(st, cfg, nowFb,
              '文件 ' + hit.path + ' 的内容回到了先前见过的状态——改了又改回是典型的原地打转,'
              + '请确认这条修改路径是否还有意义。'), kindCooldown('edit-foldback', cfg.cooldownSec * 1000));
            if (text) deliverOrReview(agent, st, 'edit-foldback',
              '文件内容折返: ' + hit.path, text);
          }).catch(() => { /* fold-back detection is best-effort */ });
        }
      } else if (tool === 'Bash') {
        const cmd = extractCmd(exec);
        const n = normCmd(cmd);
        const r = rawCmd(cmd);
        if (n || r) {
          st.recentCmds.push({ n, r });
          st.recentCmds = st.recentCmds.slice(-cfg.maxRecentCmds);
        }
      }

      const isError = result?.isError === true;
      if (isError) {
        st.failStreak += 1;
        st.totalFailures += 1;
        st.recentLog.push({ tool, brief: briefOf(exec, extractCmd(exec)), ok: false });
        st.recentLog = st.recentLog.slice(-cfg.reviewLogSize);
        if (st.failStreak >= cfg.failStreakThreshold
            && now - st.lastFailReminderAt >= kindCooldown('fail-loop', cfg.failCooldownSec * 1000)
            && st.reminders < cfg.maxReminders && cfg.enabled) {
          st.reminders += 1;
          st.lastReminderAt = now;
          st.lastFailReminderAt = now;
          st.lastTrigger = 'fail-loop';
          let text = [
            '🔔 【AI 抬头 · 失败循环】你已连续失败 ' + st.failStreak + ' 次。请勿再用同样的方式重试:',
            '1. 完整读取最近一次的错误信息,定位根因(而不是只看表面症状);',
            '2. 判断:这是可以修复的问题,还是方法本身不可行?',
            '3. 换方法、修复后再试;若连续两次换方法仍失败,停下来向用户汇报卡点。',
          ].join('\n');
          if (st.reminders >= 3) {
            text += '\n⚠️ 这已是本会话第 ' + (st.reminders + 1) + ' 次提醒。请考虑完全放弃当前路径,直接向用户汇报。';
          }
          deliverOrReview(agent, st, 'fail-loop',
            '连续失败 ' + st.failStreak + ' 次', text);
          if (cfg.lessons) {
            const lastFail = [...st.recentLog].reverse().find((e) => !e.ok);
            if (lastFail) {
              registerLesson(normCmd(lastFail.brief) || '未知模式',
                '连续失败 ' + st.failStreak + ' 次后被打断——换方法前请先读错误定位根因,勿重复此方式');
            }
          }
          st.failStreak = 0;
          return;
        }
      } else {
        st.failStreak = 0;
        st.recentLog.push({ tool, brief: briefOf(exec, extractCmd(exec)), ok: true });
        st.recentLog = st.recentLog.slice(-cfg.reviewLogSize);
      }

      if (!cfg.enabled) return;

      evaluatePending(st);

      // triggers, strongest evidence first
      if (st.callsSinceReminder >= cfg.repeatMinCalls) {
        const exact = exactRepeatHit(st, cfg);
        if (exact) {
          const text = fire(st, cfg, now, 'exact-repeat', nudgeText(st, cfg, now,
            '完全相同的命令已原样执行 ' + exact.count + ' 次:`' + exact.raw.slice(0, 120)
            + '`。同样的输入必然得到同样的结果——请换参数、换方法,或停下来重新评估。'), kindCooldown('exact-repeat', cfg.cooldownSec * 1000));
          if (text) { deliverOrReview(agent, st, 'exact-repeat',
            '原样命令重复 ' + exact.count + ' 次: ' + exact.raw.slice(0, 100), text); return; }
        }
        const repeat = repeatHit(st, cfg);
        if (repeat) {
          const text = fire(st, cfg, now, 'repeat-cmds', nudgeText(st, cfg, now,
            '检测到重复执行:归一化后为 "' + repeat.prefix + ' …" 的命令在本段已出现 '
            + repeat.count + ' 次。同样的命令大概率得到同样的结果——请换参数、换思路,或停下来重新评估。'), kindCooldown('repeat-cmds', cfg.cooldownSec * 1000));
          if (text) { deliverOrReview(agent, st, 'repeat-cmds',
            '相似命令重复 ' + repeat.count + ' 次: ' + repeat.prefix, text); return; }
        }
      }
      if (st.edits === 0 && minutesSince(st, now) >= cfg.longRunMinutes
          && st.callsSinceReminder >= cfg.longRunMinCalls) {
        const text = fire(st, cfg, now, 'long-run', nudgeText(st, cfg, now,
          '本段会话已持续约 ' + minutesSince(st, now).toFixed(0)
          + ' 分钟,还没有任何文件被修改。如果当前路径走不通,请考虑向用户说明卡点,而不是继续消耗时间。'), kindCooldown('long-run', cfg.cooldownSec * 1000));
        if (text) { deliverOrReview(agent, st, 'long-run',
          '运行 ' + minutesSince(st, now).toFixed(0) + ' 分钟零修改', text); return; }
      }
      if (st.callsSinceReminder >= cfg.callNudgeInterval && st.editsSinceReminder === 0) {
        const text = fire(st, cfg, now, 'no-output', nudgeText(st, cfg, now,
          '自上次审查以来 ' + st.callsSinceReminder
          + ' 次工具调用没有产生任何文件修改——要么是在合理地调研,要么是在空跑。请用证据判断是哪一种。'), kindCooldown('no-output', cfg.cooldownSec * 1000));
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
            deliver(agent, ctx, '🎯 【AI 抬头 · 目标漂移巡检】判定: ' + (DRIFT_LABEL[verdict.verdict] ?? verdict.verdict)
              + '\n漂移表现: ' + (verdict.reason || '(未给出)')
              + '\n收束建议: ' + (verdict.suggestion || '(未给出)')
              + '\n请对照最初任务,决定:把当前子任务收敛回主线 / 明确其为目标之一并告知用户 / 放弃。');
          }
        }).catch((error) => {
          ctx?.logger?.warn?.('[ai-look-up] drift patrol failed: %o', error);
        });
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
