/**
 * AI Look-Up — Host plugin for the DeepSeek Harness (cordis bundle).
 *
 * Watches the agent's behavioral rhythm and injects a self-review nudge when
 * aimless grinding is detected: the exact same command repeated, variant
 * commands that normalize to the same shape, long runs with zero file edits,
 * dense calls with zero output, and failure loops. Also anchors the agent's
 * unreliable sense of time by injecting the real local clock periodically.
 *
 * Extension points (see the cordis-plugin-development skill):
 * - `tools/result`      observe final tool outcomes
 * - `session/event`     reset per-turn state on `user/message`, wrap-up check on `turn/end`
 * - `agent.inject()`    mid-run context, enters the next admitted step
 */

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
  clockTickMinutes: 10,
  maxRecentCmds: 8,
};

const STALE_AFTER_MS = 2 * 60 * 60 * 1000;

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

function freshState() {
  const now = Date.now();
  return {
    startedAt: now, lastEventAt: now,
    toolCalls: 0, byTool: {},
    edits: 0, editsSinceReminder: 0, callsSinceReminder: 0,
    failStreak: 0, totalFailures: 0,
    recentCmds: [],
    reminders: 0, lastReminderAt: 0, lastFailReminderAt: 0, lastTrigger: '',
    stopBlocks: 0, promptResets: 0, lastClockTickAt: 0, clockTicks: 0,
  };
}

function resetStretch(state, now) {
  state.startedAt = now;
  state.callsSinceReminder = 0;
  state.editsSinceReminder = 0;
  state.failStreak = 0;
  state.recentCmds = [];
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
  return text;
}

function nudgeText(state, cfg, now, specific) {
  const lines = [
    '🔔 【AI 抬头 · 中途自我审查】当前本地时间 ' + localHm(now)
      + ',本段任务已运行 ' + minutesSince(state, now).toFixed(0)
      + ' 分钟(开始于 ' + localHm(state.startedAt)
      + '),' + state.callsSinceReminder + ' 次工具调用,期间文件修改 '
      + state.editsSinceReminder + ' 次。请暂停手头的操作,抬头检查:',
    '1. 对照最初目标:当前进展到哪一步?是否已经偏离?',
    '2. 最近 5 次工具调用各带来了什么新信息?如果没有新信息,说明正在空跑。',
  ];
  if (specific) {
    lines.push('3. ' + specific);
    lines.push('4. 用一句话得出结论:继续 / 换方法 / 先向用户汇报,然后再继续工作。');
  } else {
    lines.push('3. 用一句话得出结论:继续 / 换方法 / 先向用户汇报,然后再继续工作。');
  }
  if (state.reminders >= 3) {
    lines.push('⚠️ 这已是本会话第 ' + (state.reminders + 1)
      + ' 次提醒,之前的提醒后仍没有改善。请认真考虑:停止当前方法,直接向用户汇报现状与卡点,请求指示。');
  }
  return lines.join('\n');
}

const clockText = (state, now) => '🕐 【AI 抬头 · 本地时钟】当前本地时间 ' + localHm(now)
  + ',本段任务开始于 ' + localHm(state.startedAt)
  + ',已进行 ' + minutesSince(state, now).toFixed(0)
  + ' 分钟。AI 对时间流逝的感觉不可靠:凡是要向用户预估或汇报耗时、判断是否超时,请一律以这个真实时钟为准,不要自己估算。';

/**
 * Deliver a nudge. `agent.inject()` is the documented mid-run channel; the
 * inbox prepend is the fallback if the runtime rejects a plain string.
 */
function deliver(agent, ctx, text) {
  try {
    if (typeof agent.inject === 'function') {
      agent.inject(text);
      return true;
    }
  } catch (error) {
    ctx?.logger?.warn?.('[ai-look-up] agent.inject failed: %o', error);
  }
  try {
    if (agent.inbox && typeof agent.inbox.prepend === 'function') {
      agent.inbox.prepend('next-step', text);
      return true;
    }
  } catch (error) {
    ctx?.logger?.warn?.('[ai-look-up] inbox prepend failed: %o', error);
  }
  return false;
}

export function apply(ctx, config) {
  const cfg = mergeConfig(config);
  const state = new Map(); // agent object -> session state

  const stateOf = (agent) => {
    let st = state.get(agent);
    if (st === undefined) {
      st = freshState();
      state.set(agent, st);
    }
    return st;
  };

  ctx.on('agent/created', (agent) => {
    state.set(agent, freshState());
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
      if (now - st.lastEventAt > STALE_AFTER_MS) resetStretch(st, now);
      if (event.type === 'user/message') {
        st.promptResets += 1;
        resetStretch(st, now);
        return;
      }
      // turn/end: wrap-up review for long, outputless sessions
      if (!cfg.stopCheck || !cfg.enabled) return;
      if (st.stopBlocks >= 2) return;
      if (st.edits === 0 && st.toolCalls >= cfg.stopMinCalls
          && minutesSince(st, now) >= cfg.longRunMinutes) {
        st.stopBlocks += 1;
        const elapsed = minutesSince(st, now);
        resetStretch(st, now);
        deliver(agent, ctx, [
          '🔔 【AI 抬头 · 结束前审查】这轮会话累计 ' + st.toolCalls + ' 次工具调用、约 '
            + elapsed.toFixed(0) + ' 分钟,但没有任何文件被修改。在结束回复之前,请先:',
          '1. 明确向用户汇报:你做了什么尝试、卡在哪里、下一步建议是什么;',
          '2. 如果还有未完成的检查,先完成再结束;',
          '3. 不要默默结束一段没有产出的长时间运行。',
        ].join('\n'));
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
      if (now - st.lastEventAt > STALE_AFTER_MS) resetStretch(st, now);
      st.lastEventAt = now;

      const tool = toolNameOf(exec);
      st.toolCalls += 1;
      st.callsSinceReminder += 1;
      st.byTool[tool] = (st.byTool[tool] ?? 0) + 1;
      st.failStreak = 0;

      if (PRODUCTIVE_TOOLS.has(tool)) {
        st.edits += 1;
        st.editsSinceReminder += 1;
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
        if (st.failStreak >= cfg.failStreakThreshold
            && now - st.lastFailReminderAt >= cfg.failCooldownSec * 1000
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
          st.failStreak = 0;
          deliver(agent, ctx, text);
          return;
        }
      }

      if (!cfg.enabled) return;

      // triggers, strongest evidence first
      if (st.callsSinceReminder >= cfg.repeatMinCalls) {
        const exact = exactRepeatHit(st, cfg);
        if (exact) {
          const text = fire(st, cfg, now, 'exact-repeat', nudgeText(st, cfg, now,
            '完全相同的命令已原样执行 ' + exact.count + ' 次:`' + exact.raw.slice(0, 120)
            + '`。同样的输入必然得到同样的结果——请换参数、换方法,或停下来重新评估。'), cfg.cooldownSec * 1000);
          if (text) { deliver(agent, ctx, text); return; }
        }
        const repeat = repeatHit(st, cfg);
        if (repeat) {
          const text = fire(st, cfg, now, 'repeat-cmds', nudgeText(st, cfg, now,
            '检测到重复执行:归一化后为 "' + repeat.prefix + ' …" 的命令在本段已出现 '
            + repeat.count + ' 次。同样的命令大概率得到同样的结果——请换参数、换思路,或停下来重新评估。'), cfg.cooldownSec * 1000);
          if (text) { deliver(agent, ctx, text); return; }
        }
      }
      if (st.edits === 0 && minutesSince(st, now) >= cfg.longRunMinutes
          && st.callsSinceReminder >= cfg.longRunMinCalls) {
        const text = fire(st, cfg, now, 'long-run', nudgeText(st, cfg, now,
          '本段会话已持续约 ' + minutesSince(st, now).toFixed(0)
          + ' 分钟,还没有任何文件被修改。如果当前路径走不通,请考虑向用户说明卡点,而不是继续消耗时间。'), cfg.cooldownSec * 1000);
        if (text) { deliver(agent, ctx, text); return; }
      }
      if (st.callsSinceReminder >= cfg.callNudgeInterval && st.editsSinceReminder === 0) {
        const text = fire(st, cfg, now, 'no-output', nudgeText(st, cfg, now,
          '自上次审查以来 ' + st.callsSinceReminder
          + ' 次工具调用没有产生任何文件修改——要么是在合理地调研,要么是在空跑。请用证据判断是哪一种。'), cfg.cooldownSec * 1000);
        if (text) { deliver(agent, ctx, text); return; }
      }
      // local clock anchor, lowest priority; quiet within 2 minutes of a nudge
      if (cfg.clockTick && now - st.lastReminderAt >= 120000) {
        const tickMs = cfg.clockTickMinutes * 60000;
        if (now - st.lastClockTickAt >= tickMs && minutesSince(st, now) >= cfg.clockTickMinutes) {
          st.lastClockTickAt = now;
          st.clockTicks += 1;
          deliver(agent, ctx, clockText(st, now));
        }
      }
    } catch (error) {
      ctx?.logger?.warn?.('[ai-look-up] tool result handling failed: %o', error);
    }
  });
}
