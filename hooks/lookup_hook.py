#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ai-look-up —— AI 抬头钩子(核心检测脚本)

在会话中监控 AI 的行为节奏,识别"长时间空跑"的典型特征:

  1. 大量工具调用却没有任何文件修改(只跑不产出);
  2. 高度相似的命令被反复执行(换汤不换药的重复试探,如 _peek2.py/_peek3.py/_peek4.py);
  3. 工具连续失败(盲目重试循环);
  4. 会话持续过长且始终没有产出。

命中特征时通过 hook 输出注入一段"抬头"提醒(additionalContext),
要求 AI 中途停下来自我审查:对照最初目标、检查近几步是否带来新信息、
决定继续 / 换方法 / 先向用户汇报。

用法(由 hooks.json 调用,stdin 传 hook 事件 JSON):
    lookup_hook.py prompt      # UserPromptSubmit:重置回合计数
    lookup_hook.py post-use    # PostToolUse:记录调用并评估是否注入提醒
    lookup_hook.py post-fail   # PostToolUseFailure:累计连续失败
    lookup_hook.py stop        # Stop:结束前审查(可请求一次继续)

人工调试/查看:
    lookup_hook.py status      # 打印当前会话监控状态
    lookup_hook.py reset       # 删除状态文件(重置监控)
"""

import json
import os
import re
import sys
import tempfile
import time

PRODUCTIVE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit", "ApplyPatch"}

# 会话闲置超过该秒数后,下一次事件视为新的工作时段
STALE_AFTER_SEC = 2 * 3600

DEFAULTS = {
    "enabled": True,
    # 连续 N 次工具调用且期间没有文件修改 → 注入通用提醒
    "call_nudge_interval": 30,
    # 最近 repeat_window 条命令中有 repeat_cmd_count 条归一化后相同 → 判定重复空转
    "repeat_window": 5,
    "repeat_cmd_count": 3,
    "repeat_min_calls": 8,
    # 工具连续失败 N 次 → 注入"停止盲目重试"提醒
    "fail_streak_threshold": 3,
    "fail_cooldown_sec": 180,
    # 会话持续超过 N 分钟且从未修改文件、调用频繁 → 提醒
    "long_run_minutes": 25,
    "long_run_min_calls": 15,
    # 普通提醒冷却(秒),避免刷屏
    "cooldown_sec": 300,
    # 单会话最多注入提醒次数
    "max_reminders": 12,
    # 结束时若长期零产出的会话,请求一次继续以强制复盘
    "stop_check": True,
    "stop_min_calls": 40,
    # 本地时钟锚点:每 N 分钟注入一次真实本地时间,校准 AI 的时间感
    "clock_tick": True,
    "clock_tick_minutes": 10,
}

BOOL_KEYS = {"enabled", "stop_check", "clock_tick"}


def load_config():
    """默认值 ← 插件根 config.json ← ~/.zcode/ai-look-up.json ← 项目 .zcode/ai-look-up.json ← LOOKUP_* 环境变量"""
    cfg = dict(DEFAULTS)
    candidates = []
    plugin_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    candidates.append(os.path.join(plugin_root, "config.json"))
    candidates.append(os.path.expanduser("~/.zcode/ai-look-up.json"))
    candidates.append(os.path.join(os.getcwd(), ".zcode", "ai-look-up.json"))
    for path in candidates:
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                for k in DEFAULTS:
                    if k in data:
                        cfg[k] = data[k]
        except Exception:
            pass
    for k in DEFAULTS:
        raw = os.environ.get("LOOKUP_" + k.upper())
        if raw is None or raw == "":
            continue
        try:
            if k in BOOL_KEYS:
                cfg[k] = raw.strip().lower() in ("1", "true", "yes", "on")
            elif isinstance(DEFAULTS[k], bool):
                cfg[k] = raw.strip().lower() in ("1", "true", "yes", "on")
            elif isinstance(DEFAULTS[k], int):
                cfg[k] = int(float(raw))
            else:
                cfg[k] = float(raw)
        except Exception:
            pass
    return cfg


def read_stdin_json():
    try:
        raw = sys.stdin.read()
        if raw and raw.strip():
            return json.loads(raw)
    except Exception:
        pass
    return {}


def state_dir():
    base = os.environ.get("LOOKUP_STATE_DIR") or os.path.join(
        tempfile.gettempdir(), "zcode-ai-look-up"
    )
    os.makedirs(base, exist_ok=True)
    return base


def session_id(data, argv):
    sid = ""
    if isinstance(data, dict):
        sid = data.get("session_id") or data.get("sessionId") or ""
    if not sid:
        sid = os.environ.get("CLAUDE_SESSION_ID") or os.environ.get("ZCODE_SESSION_ID") or ""
    if not sid and "--session" in argv:
        i = argv.index("--session")
        if i + 1 < len(argv):
            sid = argv[i + 1]
    sid = str(sid).strip() or "default"
    return re.sub(r"[^A-Za-z0-9_.-]", "_", sid)[:120]


def state_path(sid):
    return os.path.join(state_dir(), sid + ".json")


def new_state(sid, now):
    return {
        "v": 1,
        "session_id": sid,
        "started_at": now,
        "last_event_at": now,
        "tool_calls": 0,
        "by_tool": {},
        "edits": 0,
        "edits_since_reminder": 0,
        "calls_since_reminder": 0,
        "fail_streak": 0,
        "total_failures": 0,
        "recent_cmds": [],
        "reminders": 0,
        "last_reminder_at": 0.0,
        "last_fail_reminder_at": 0.0,
        "last_trigger": "",
        "stop_blocks": 0,
        "prompt_resets": 0,
        "last_clock_tick_at": 0.0,
        "clock_ticks": 0,
    }


def load_state(sid, now):
    try:
        with open(state_path(sid), encoding="utf-8") as f:
            state = json.load(f)
        if not isinstance(state, dict) or state.get("v") != 1:
            raise ValueError("bad state")
    except Exception:
        return new_state(sid, now)
    for k, v in new_state(sid, now).items():
        state.setdefault(k, v)
    return state


def save_state(sid, state):
    try:
        tmp = state_path(sid) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=1)
        os.replace(tmp, state_path(sid))
    except Exception:
        pass


def reset_stretch(state, now):
    """开启新的工作时段:重置回合内的节奏计数,保留会话累计。"""
    state["started_at"] = now
    state["calls_since_reminder"] = 0
    state["edits_since_reminder"] = 0
    state["fail_streak"] = 0
    state["recent_cmds"] = []


def extract_cmd(data):
    ti = data.get("tool_input")
    if ti is None:
        ti = data.get("toolInput")
    if isinstance(ti, str):
        return ti.strip()
    if isinstance(ti, dict):
        for k in ("command", "cmd", "script"):
            v = ti.get(k)
            if isinstance(v, str):
                return v
    return ""


def norm_cmd(cmd):
    """把一条命令归一化成"程序 + 目标主体",用于识别换汤不换药的重复执行。

    python _peek2.py  /  python _peek3.py  /  python "_peek4.py"  →  "python _peek"
    """
    s = (cmd or "").strip().lower()
    if not s:
        return ""
    s = re.sub(r"\s+", " ", s)
    # 取管道/链式命令的最后一段(前面的是准备动作)
    parts = re.split(r"&&|\|\||;|\|", s)
    s = parts[-1].strip() if parts else s
    toks = s.split(" ")
    head = toks[0].replace("\\", "/").split("/")[-1].strip("\"'")
    head = re.sub(r"\.exe$", "", head)
    second = toks[1] if len(toks) > 1 else ""
    second = second.replace("\\", "/").split("/")[-1].strip("\"'")
    second = re.sub(r"\.(py|js|ts|mjs|cjs|sh|json|md|txt|log|csv|yaml|yml)$", "", second)
    second = re.sub(r"[\d_-]+$", "", second)
    return (head + " " + second).strip()


def raw_cmd(cmd):
    """压缩空白后的原始命令,用于识别"同一命令带同样参数原样重跑"。"""
    return " ".join((cmd or "").split())


def repeat_hit(state, cfg):
    cmds = state.get("recent_cmds")[-cfg["repeat_window"]:]
    if len(cmds) < cfg["repeat_cmd_count"]:
        return None
    last = cmds[-1]["n"]
    if not last:
        return None
    count = sum(1 for c in cmds if c["n"] == last)
    if count >= cfg["repeat_cmd_count"]:
        return {"prefix": last, "count": count}
    return None


def exact_repeat_hit(state, cfg):
    """最近窗口内出现 N 条完全相同的原样命令(参数级)。"""
    cmds = state.get("recent_cmds")[-cfg["repeat_window"]:]
    if len(cmds) < cfg["repeat_cmd_count"]:
        return None
    last = cmds[-1].get("r", "")
    if not last:
        return None
    count = sum(1 for c in cmds if c.get("r", "") == last)
    if count >= cfg["repeat_cmd_count"]:
        return {"raw": last, "count": count}
    return None


def fmt_minutes(state, now):
    return max(0.0, (now - state.get("started_at", now)) / 60.0)


def fire(state, cfg, now, kind, text, cooldown):
    if not cfg["enabled"]:
        return None
    if state["reminders"] >= cfg["max_reminders"]:
        return None
    if now - state.get("last_reminder_at", 0.0) < cooldown:
        return None
    state["reminders"] += 1
    state["last_reminder_at"] = now
    state["last_trigger"] = kind
    state["calls_since_reminder"] = 0
    state["edits_since_reminder"] = 0
    return text


def local_hm(ts):
    return time.strftime("%H:%M", time.localtime(ts))


def nudge_text(state, cfg, now, specific):
    mins = fmt_minutes(state, now)
    lines = [
        "🔔 【AI 抬头 · 中途自我审查】当前本地时间 {h},本段任务已运行 {m:.0f} 分钟"
        "(开始于 {hs}),{c} 次工具调用,期间文件修改 {e} 次。"
        "请暂停手头的操作,抬头检查:".format(
            h=local_hm(now), m=mins, hs=local_hm(state.get("started_at", now)),
            c=state["calls_since_reminder"], e=state["edits_since_reminder"]),
        "1. 对照最初目标:当前进展到哪一步?是否已经偏离?",
        "2. 最近 5 次工具调用各带来了什么新信息?如果没有新信息,说明正在空跑。",
    ]
    if specific:
        lines.append("3. " + specific)
        lines.append("4. 用一句话得出结论:继续 / 换方法 / 先向用户汇报,然后再继续工作。")
    else:
        lines.append("3. 用一句话得出结论:继续 / 换方法 / 先向用户汇报,然后再继续工作。")
    if state.get("reminders", 0) >= 3:
        lines.append(
            "⚠️ 这已是本会话第 {n} 次提醒,之前的提醒后仍没有改善。"
            "请认真考虑:停止当前方法,直接向用户汇报现状与卡点,请求指示。".format(
                n=state["reminders"] + 1))
    return "\n".join(lines)


def clock_text(state, now):
    return (
        "🕐 【AI 抬头 · 本地时钟】当前本地时间 {h},本段任务开始于 {hs},已进行 {m:.0f} 分钟。"
        "AI 对时间流逝的感觉不可靠:凡是要向用户预估或汇报耗时、判断是否超时,"
        "请一律以这个真实时钟为准,不要自己估算。"
    ).format(h=local_hm(now), hs=local_hm(state.get("started_at", now)), m=fmt_minutes(state, now))


def post_use_output(text):
    return {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": text,
        }
    }


def post_fail_output(text):
    return {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUseFailure",
            "additionalContext": text,
        }
    }


def stop_output(reason):
    return {"decision": "block", "reason": reason}


def handle_post_use(cfg, data, state, now):
    if now - state.get("last_event_at", now) > STALE_AFTER_SEC:
        reset_stretch(state, now)
    state["last_event_at"] = now
    tool = str(data.get("tool_name") or data.get("toolName") or "Unknown")
    state["tool_calls"] += 1
    state["calls_since_reminder"] += 1
    state["by_tool"][tool] = state["by_tool"].get(tool, 0) + 1
    state["fail_streak"] = 0

    if tool in PRODUCTIVE_TOOLS:
        state["edits"] += 1
        state["edits_since_reminder"] += 1
    elif tool == "Bash":
        cmd = extract_cmd(data)
        n = norm_cmd(cmd)
        r = raw_cmd(cmd)
        if n or r:
            recent = state.get("recent_cmds", [])
            recent.append({"t": now, "n": n, "r": r})
            state["recent_cmds"] = recent[-8:]

    # ── 触发器按证据强度排序,命中第一个即返回 ──
    # 0) 完全相同的命令原样重复(参数级,证据最强)
    if state["calls_since_reminder"] >= cfg["repeat_min_calls"]:
        hit = exact_repeat_hit(state, cfg)
        if hit:
            text = fire(
                state, cfg, now, "exact-repeat",
                nudge_text(
                    state, cfg, now,
                    "完全相同的命令已原样执行 {n} 次:`{cmd}`。"
                    "同样的输入必然得到同样的结果——请换参数、换方法,或停下来重新评估。".format(
                        n=hit["count"], cmd=hit["raw"][:120]),
                ),
                cfg["cooldown_sec"],
            )
            if text:
                return post_use_output(text)
    # 1) 重复命令空转(归一化相似)
    if state["calls_since_reminder"] >= cfg["repeat_min_calls"]:
        hit = repeat_hit(state, cfg)
        if hit:
            text = fire(
                state, cfg, now, "repeat-cmds",
                nudge_text(
                    state, cfg, now,
                    "检测到重复执行:归一化后为 \"{p} …\" 的命令在本段已出现 {n} 次。"
                    "同样的命令大概率得到同样的结果——请换参数、换思路,或停下来重新评估。".format(
                        p=hit["prefix"], n=hit["count"]),
                ),
                cfg["cooldown_sec"],
            )
            if text:
                return post_use_output(text)
    # 2) 会话超长且从未产出修改
    if (state["edits"] == 0
            and fmt_minutes(state, now) >= cfg["long_run_minutes"]
            and state["calls_since_reminder"] >= cfg["long_run_min_calls"]):
        text = fire(
            state, cfg, now, "long-run",
            nudge_text(
                state, cfg, now,
                "本段会话已持续约 {m:.0f} 分钟,还没有任何文件被修改。"
                "如果当前路径走不通,请考虑向用户说明卡点,而不是继续消耗时间。".format(
                    m=fmt_minutes(state, now)),
            ),
            cfg["cooldown_sec"],
        )
        if text:
            return post_use_output(text)
    # 3) 调用量大但期间零修改
    if (state["calls_since_reminder"] >= cfg["call_nudge_interval"]
            and state["edits_since_reminder"] == 0):
        text = fire(
            state, cfg, now, "no-output",
            nudge_text(
                state, cfg, now,
                "自上次审查以来 {c} 次工具调用没有产生任何文件修改——"
                "要么是在合理地调研,要么是在空跑。请用证据判断是哪一种。".format(
                    c=state["calls_since_reminder"]),
            ),
            cfg["cooldown_sec"],
        )
        if text:
            return post_use_output(text)
    # 4) 本地时钟锚点(最低优先级:空跑提醒优先,且其刚发出 2 分钟内时钟静默)
    if cfg["clock_tick"] and now - state.get("last_reminder_at", 0.0) >= 120:
        tick_sec = cfg["clock_tick_minutes"] * 60
        if (now - state.get("last_clock_tick_at", 0.0) >= tick_sec
                and fmt_minutes(state, now) >= cfg["clock_tick_minutes"]):
            state["last_clock_tick_at"] = now
            state["clock_ticks"] = state.get("clock_ticks", 0) + 1
            return post_use_output(clock_text(state, now))
    return None


def handle_post_fail(cfg, data, state, now):
    state["last_event_at"] = now
    state["fail_streak"] += 1
    state["total_failures"] += 1
    if state["fail_streak"] < cfg["fail_streak_threshold"]:
        return None
    if now - state.get("last_fail_reminder_at", 0.0) < cfg["fail_cooldown_sec"]:
        return None
    if state["reminders"] >= cfg["max_reminders"] or not cfg["enabled"]:
        return None
    state["reminders"] += 1
    state["last_reminder_at"] = now
    state["last_fail_reminder_at"] = now
    state["last_trigger"] = "fail-loop"
    text = "\n".join([
        "🔔 【AI 抬头 · 失败循环】你已连续失败 {n} 次。请勿再用同样的方式重试:".format(
            n=state["fail_streak"]),
        "1. 完整读取最近一次的错误信息,定位根因(而不是只看表面症状);",
        "2. 判断:这是可以修复的问题,还是方法本身不可行?",
        "3. 换方法、修复后再试;若连续两次换方法仍失败,停下来向用户汇报卡点。",
    ])
    if state.get("reminders", 0) >= 3:
        text += "\n⚠️ 这已是本会话第 {n} 次提醒。请考虑完全放弃当前路径,直接向用户汇报。".format(
            n=state["reminders"] + 1)
    state["fail_streak"] = 0
    return post_fail_output(text)


def handle_prompt(cfg, data, state, now):
    state["last_event_at"] = now
    state["prompt_resets"] += 1
    reset_stretch(state, now)
    return None


def handle_stop(cfg, data, state, now):
    state["last_event_at"] = now
    if not cfg["stop_check"] or not cfg["enabled"]:
        return None
    if state.get("stop_blocks", 0) >= 2:
        return None
    if (state["edits"] == 0
            and state["tool_calls"] >= cfg["stop_min_calls"]
            and fmt_minutes(state, now) >= cfg["long_run_minutes"]):
        elapsed = fmt_minutes(state, now)
        state["stop_blocks"] = state.get("stop_blocks", 0) + 1
        reset_stretch(state, now)
        reason = "\n".join([
            "🔔 【AI 抬头 · 结束前审查】这轮会话累计 {c} 次工具调用、约 {m:.0f} 分钟,"
            "但没有任何文件被修改。在结束回复之前,请先:".format(
                c=state["tool_calls"], m=elapsed),
            "1. 明确向用户汇报:你做了什么尝试、卡在哪里、下一步建议是什么;",
            "2. 如果还有未完成的检查,先完成再结束;",
            "3. 不要默默结束一段没有产出的长时间运行。",
        ])
        return stop_output(reason)
    return None


def judgment(state, cfg):
    score = 0
    if (state["calls_since_reminder"] >= cfg["call_nudge_interval"]
            and state["edits_since_reminder"] == 0):
        score += 2
    if state.get("recent_cmds") and exact_repeat_hit(state, cfg):
        score += 2
    if state.get("recent_cmds") and repeat_hit(state, cfg):
        score += 2
    if state["fail_streak"] >= 2:
        score += 1
    if score == 0:
        return "健康"
    if score == 1:
        return "留意"
    return "空跑嫌疑高"


def latest_state_file():
    best = None
    for name in sorted(os.listdir(state_dir())):
        p = os.path.join(state_dir(), name)
        if name.endswith(".json") and (best is None or os.path.getmtime(p) > os.path.getmtime(best)):
            best = p
    return best


def handle_status(cfg, argv):
    sid = session_id({}, argv)
    path = state_path(sid)
    if not os.path.exists(path):
        # 指定会话没有记录时,回退到最近活跃的状态文件
        best = latest_state_file()
        if best is None:
            print("AI 抬头:当前没有会话监控状态(还没有任何工具调用记录)。")
            return 0
        path = best
    with open(path, encoding="utf-8") as f:
        state = json.load(f)
    now = time.time()
    by_tool = state.get("by_tool", {})
    tools = ", ".join("{k}×{v}".format(k=k, v=v) for k, v in
                      sorted(by_tool.items(), key=lambda kv: -kv[1]))
    print("AI 抬头 · 会话监控状态")
    print("  状态文件 : {p}".format(p=path))
    print("  本地时间 : {h}(本段开始于 {hs})".format(
        h=local_hm(now), hs=local_hm(state.get("started_at", now))))
    print("  运行时长 : {m:.1f} 分钟(会话累计 {c} 次工具调用)".format(
        m=fmt_minutes(state, now), c=state.get("tool_calls", 0)))
    print("  工具分布 : {t}".format(t=tools or "无"))
    print("  文件修改 : {e} 次   失败: 连续 {f} / 累计 {t}".format(
        e=state.get("edits", 0), f=state.get("fail_streak", 0),
        t=state.get("total_failures", 0)))
    print("  提醒     : 已注入 {r}/{m} 次,上次触发: {k}".format(
        r=state.get("reminders", 0), m=cfg["max_reminders"],
        k=state.get("last_trigger") or "—"))
    recent = [c["n"] for c in state.get("recent_cmds", [])][-5:]
    print("  最近命令 : {r}".format(r=" | ".join(recent) or "无"))
    print("  判定     : {j}".format(j=judgment(state, cfg)))
    return 0


def handle_reset(argv):
    sid = session_id({}, argv)
    p = state_path(sid)
    if not os.path.exists(p):
        # 手动运行 reset 没有 stdin 会话信息,回退到最近活跃的状态文件
        p = latest_state_file() or p
    try:
        os.remove(p)
        print("已重置会话监控状态: {p}".format(p=p))
    except FileNotFoundError:
        print("没有找到状态文件({p}),无需重置。".format(p=p))
    return 0


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    argv = sys.argv[1:]
    action = argv[0] if argv else "post-use"
    if action in ("status",):
        return handle_status(load_config(), argv)
    if action in ("reset",):
        return handle_reset(argv)

    cfg = load_config()
    data = read_stdin_json()
    sid = session_id(data, argv)
    now = time.time()
    state = load_state(sid, now)

    out = None
    if action == "post-use":
        out = handle_post_use(cfg, data, state, now)
    elif action == "post-fail":
        out = handle_post_fail(cfg, data, state, now)
    elif action == "prompt":
        out = handle_prompt(cfg, data, state, now)
    elif action == "stop":
        out = handle_stop(cfg, data, state, now)
    else:
        return 0

    save_state(sid, state)
    if out:
        sys.stdout.write(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
