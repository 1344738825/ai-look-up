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

import hashlib
import json
import os
import re
import sys
import tempfile
import time
import urllib.request

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
    # 本地时钟锚点:每 N 分钟注入一次真实本地时间,校准 AI 的时间感。
    # backoff 开启时间隔倍增(10→20→40→封顶),省上下文;准确性不受影响——
    # 每次注入的都是插件从真实时钟现算的值,不存在虚拟漂移。
    "clock_tick": True,
    "clock_tick_minutes": 5,
    "clock_tick_backoff": True,
    "clock_tick_max_minutes": 60,
    # 独立审查者:触发提醒时直调一次 OpenAI 兼容接口,用独立上下文判定是否空跑。
    # 默认关闭;在配置文件里给出 llm_api_key 后启用(DeepSeek: api_base 保持默认
    # https://api.deepseek.com,model deepseek-chat)。失败/超时回退静态清单。
    "llm_review": False,
    "llm_api_base": "https://api.deepseek.com",
    "llm_api_key": "",
    "llm_model": "deepseek-chat",
    "llm_timeout_sec": 12,
    "review_log_size": 12,
    # A) 折返编辑检测:同一文件的内容回到先前见过的状态(改了又改回)= 原地打转
    "edit_foldback": True,
    "foldback_window": 5,
    # C) 提醒有效性自适应:按历史有效率调整各类提醒的冷却(有效→更勤,无效→降噪)
    "adaptive": True,
    # 目标漂移巡检:每 N 次调用用独立审查者对照最初目标,抓"子任务喧宾夺主"
    "drift_check": True,
    "drift_check_calls": 30,
    "drift_cooldown_sec": 900,
    # 教训登记簿:失败循环自动登记坑位,回合开始时注入 Top 教训,防"认坑后再踩"
    "lessons": True,
}

BOOL_KEYS = {"enabled", "stop_check", "clock_tick", "llm_review", "edit_foldback", "adaptive", "drift_check", "lessons", "clock_tick_backoff"}

REVIEW_SYSTEM = (
    "你是 AI 编码代理的独立行为审查员。主代理看不到你的存在,你只依据给它的任务描述"
    "与最近工具调用记录做判断。判断标准:最近的调用是否持续带来新信息、是否朝着任务"
    "目标推进;同样的命令反复执行、长时间没有任何文件修改、连续失败后仍在重试,都是"
    "空跑迹象。只输出一个 JSON 对象,不要输出任何其他文字:\n"
    '{"verdict":"on-track|drifting|stuck","reason":"一句话依据","suggestion":"一句话建议"}\n'
    "on-track=仍在正轨;drifting=有偏航/空转迹象;stuck=确认空跑或卡死。"
)

VERDICT_LABEL = {"on-track": "仍在正轨", "drifting": "有偏航迹象", "stuck": "空跑确认"}

DRIFT_LABEL = {"on-track": "仍在服务主线", "drifting": "子任务喧宾夺主", "stuck": "已明显偏离主线"}

REVIEW_DRIFT_SYSTEM = (
    "你是 AI 编码代理的目标漂移审查员。主代理看不到你的存在。长上下文任务最常见的失败是:"
    "最初为核验某个决定而展开的子任务,做着做着变成了独立目标,喧宾夺主。"
    "你只依据最初任务描述与最近工具调用记录判断:当前行为是否仍在服务最初目标,"
    "还是某个子任务已经扩张成了事实上的新目标。只输出一个 JSON 对象,不要输出任何其他文字:\n"
    '{"verdict":"on-track|drifting|stuck","reason":"一句话:当前行为与最初目标的关系","suggestion":"一句话:如何收束"}\n'
    "on-track=仍在服务主线;drifting=子任务喧宾夺主;stuck=已明显偏离主线。"
)


def adaptive_path():
    return os.path.join(state_dir(), "adaptive.json")


def load_adaptive():
    try:
        with open(adaptive_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_adaptive(stats):
    try:
        with open(adaptive_path(), "w", encoding="utf-8") as f:
            json.dump(stats, f, ensure_ascii=False)
    except Exception:
        pass


def kind_cooldown(cfg, kind, base):
    """按该类提醒的历史有效率调整冷却:无效(<30%)翻倍降噪,有效(>70%)缩短到 3/4。"""
    if not cfg.get("adaptive"):
        return base
    stats = load_adaptive().get(kind)
    if not stats or stats.get("fired", 0) < 5:
        return base
    rate = stats["effective"] / max(1, stats["fired"])
    if rate < 0.3:
        return min(base * 2, base * 4)
    if rate > 0.7:
        return max(base * 0.75, 60)
    return base


def evaluate_pending(state, cfg):
    """提醒发出 10 次调用后结算有效性:期间出现修改/失败停止/命令模式变化都算有效。"""
    pending = state.get("pending_reminders", [])
    if not pending:
        return
    remaining = []
    changed = False
    for p in pending:
        if state["tool_calls"] - p["calls_at"] < 10:
            remaining.append(p)
            continue
        effective = (state["edits"] - p["edits_at"] > 0) or (
            state["fail_streak"] == 0 and p["fail"] > 0)
        if not effective:
            cur = state.get("recent_cmds", [])
            n_last = cur[-1]["n"] if cur else ""
            effective = bool(n_last) and n_last != p.get("norm", "")
        stats = load_adaptive()
        s = stats.setdefault(p["kind"], {"fired": 0, "effective": 0})
        s["fired"] = s.get("fired", 0) + 1
        if effective:
            s["effective"] = s.get("effective", 0) + 1
        save_adaptive(stats)
        changed = True
    if changed or remaining != pending:
        state["pending_reminders"] = remaining


def lessons_path():
    return os.path.join(state_dir(), "lessons.json")


def register_lesson(pattern, correction):
    """登记/累加一条坑位教训;同模式合并,hits 记录被踩次数。"""
    if not pattern:
        return
    try:
        with open(lessons_path(), encoding="utf-8") as f:
            lessons = json.load(f)
        lessons = lessons if isinstance(lessons, list) else []
    except Exception:
        lessons = []
    for entry in lessons:
        if entry.get("pattern") == pattern:
            entry["hits"] = entry.get("hits", 1) + 1
            entry["correction"] = correction
            entry["last_seen"] = time.time()
            break
    else:
        lessons.append({"pattern": pattern, "correction": correction,
                        "hits": 1, "last_seen": time.time()})
    lessons.sort(key=lambda e: -e.get("hits", 1))
    try:
        with open(lessons_path(), "w", encoding="utf-8") as f:
            json.dump(lessons[:50], f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def top_lessons(n=3):
    try:
        with open(lessons_path(), encoding="utf-8") as f:
            lessons = json.load(f)
        return [e for e in lessons if isinstance(e, dict)][:n]
    except Exception:
        return []


def lessons_text(state):
    lessons = top_lessons(3)
    if not lessons:
        return None
    # 内容没变就不重复注入(每 5 个回合强制重注一次,防上下文压缩后丢失)
    sig = "|".join("{}x{}".format(e["pattern"], e.get("hits", 1)) for e in lessons)
    count = state.get("prompt_resets", 0)
    if state.get("lessons_sig") == sig and count % 5 != 0:
        return None
    state["lessons_sig"] = sig
    lines = ["📚 【AI 抬头 · 已知坑位】请勿重复:"]
    for i, e in enumerate(lessons, 1):
        lines.append("{i}. [{hits} 次] {p} —— {c}".format(
            i=i, hits=e.get("hits", 1), p=e["pattern"], c=e.get("correction", "")))
    return "\n".join(lines)


def record_edit_hash(state, cfg, data):
    """记录 Edit/Write 后文件内容的短 hash;内容回到先前见过的状态 → 折返(返回 True)。"""
    ti = data.get("tool_input") or data.get("toolInput") or {}
    if not isinstance(ti, dict):
        return False
    path = ti.get("file_path") or ti.get("path") or ti.get("notebook_path")
    if not path:
        return False
    path = os.path.abspath(str(path))
    try:
        with open(path, "rb") as f:
            h = hashlib.md5(f.read()).hexdigest()[:12]
    except Exception:
        return False
    hashes = state.setdefault("file_hashes", {})
    seen = hashes.setdefault(path, [])
    hit = len(seen) >= 1 and h in seen[:-1]
    seen.append(h)
    hashes[path] = seen[-cfg["foldback_window"]:]
    if len(hashes) > 16:
        for k in list(hashes)[:-16]:
            hashes.pop(k, None)
    return hit


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
            elif isinstance(DEFAULTS[k], float):
                cfg[k] = float(raw)
            else:
                cfg[k] = raw
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
        "clock_interval": 0,
        "recent_log": [],
        "last_goal": "",
        "reviews": 0,
        "lessons_sig": "",
        "file_hashes": {},
        "pending_reminders": [],
        "drift_checks": 0,
        "last_drift_at": 0.0,
        "calls_at_last_drift": 0,
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


def reset_stretch(state, cfg, now):
    """开启新的工作时段:重置回合内的节奏计数,保留会话累计。"""
    state["started_at"] = now
    state["calls_since_reminder"] = 0
    state["edits_since_reminder"] = 0
    state["fail_streak"] = 0
    state["recent_cmds"] = []
    state["clock_interval"] = 0


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
    cur = state.get("recent_cmds", [])
    pending = state.setdefault("pending_reminders", [])
    pending.append({
        "kind": kind,
        "calls_at": state["tool_calls"],
        "edits_at": state["edits"],
        "fail": state["fail_streak"],
        "norm": cur[-1]["n"] if cur else "",
    })
    state["pending_reminders"] = pending[-10:]
    return text


def local_hm(ts):
    return time.strftime("%H:%M", time.localtime(ts))


def nudge_text(state, cfg, now, specific):
    mins = fmt_minutes(state, now)
    lines = [
        "🔔 【AI 抬头 · 中途自我审查】时钟 {h},本段 {m:.0f} 分钟/{c} 次调用,修改 {e} 次。自查:".format(
            h=local_hm(now), m=mins,
            c=state["calls_since_reminder"], e=state["edits_since_reminder"]),
        "① 对照最初目标是否偏移?② 最近 5 次调用有无新信息?若无 → 正在空跑。",
    ]
    if specific:
        lines.append("③ " + specific)
    lines.append("→ 一句话结论:继续 / 换方法 / 先向用户汇报,然后再继续。")
    if state.get("reminders", 0) >= 3:
        lines.append(
            "⚠️ 第 {n} 次提醒仍无改善——停止当前方法,直接向用户汇报卡点。".format(
                n=state["reminders"] + 1))
    return "\n".join(lines)


def clock_text(state, now):
    return (
        "🕐 【AI 抬头 · 本地时钟】{h},已进行 {m:.0f} 分钟(开始于 {hs})。"
        "耗时预估/汇报以此为准,勿自估。"
    ).format(h=local_hm(now), hs=local_hm(state.get("started_at", now)), m=fmt_minutes(state, now))


def build_review_material(state, cfg, now, kind, detail):
    log_lines = [
        "{i}. [{t}{ok}] {b}".format(
            i=i + 1, t=e.get("tool", "?"),
            ok="" if e.get("ok") else " ✗",
            b=(e.get("brief") or "无")[:60])
        for i, e in enumerate(state.get("recent_log", [])[-min(8, cfg["review_log_size"]):])
    ]
    parts = [
        "【审查材料】触发: " + kind,
        "【任务】" + (state.get("last_goal") or "(未捕获)")[:200],
        "【统计】{m:.0f} 分钟/{c} 次调用/修改 {e},连败 {f},累计败 {t}。".format(
            m=fmt_minutes(state, now), c=state.get("tool_calls", 0),
            e=state.get("edits", 0), f=state.get("fail_streak", 0),
            t=state.get("total_failures", 0)),
        "【最近调用(旧→新)】",
    ]
    parts.extend(log_lines)
    if detail:
        parts.append("【细节】" + detail)
    return "\n".join(parts)


def call_llm_review(state, cfg, now, kind, detail, system=REVIEW_SYSTEM):
    """直调一次 OpenAI 兼容接口,返回解析后的 verdict dict;任何失败返回 None。"""
    api_key = cfg.get("llm_api_key") or ""
    if not api_key:
        return None
    material = build_review_material(state, cfg, now, kind, detail)
    payload = json.dumps({
        "model": cfg.get("llm_model") or "deepseek-chat",
        "temperature": 0,
        "max_tokens": 200,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": material},
        ],
    }).encode("utf-8")
    base = (cfg.get("llm_api_base") or "https://api.deepseek.com").rstrip("/")
    req = urllib.request.Request(
        base + "/chat/completions", data=payload, method="POST",
        headers={
            "Authorization": "Bearer " + api_key,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=cfg.get("llm_timeout_sec", 12)) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        text = data["choices"][0]["message"]["content"]
    except Exception:
        return None
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        obj = json.loads(text[start:end + 1])
    except Exception:
        return None
    if obj.get("verdict") not in VERDICT_LABEL:
        return None
    return {
        "verdict": obj["verdict"],
        "reason": str(obj.get("reason", ""))[:300],
        "suggestion": str(obj.get("suggestion", ""))[:300],
    }


def finalize_text(state, cfg, now, kind, detail, static_text):
    """提醒发出前的最后一站:llm_review 开启时追加独立审查结论,失败回退静态文案。
    on-track 判定用短确认替代完整清单,省上下文。"""
    if not cfg.get("llm_review"):
        return static_text
    verdict = call_llm_review(state, cfg, now, kind, detail)
    if verdict is None:
        return static_text
    state["reviews"] = state.get("reviews", 0) + 1
    if verdict["verdict"] == "on-track":
        return (static_text.split("\n")[0]
                + "\n🔎 【AI 抬头 · 独立审查】仍在正轨(" + (verdict["reason"] or "无异常")
                + ")——保持节奏。")
    return (static_text + "\n🔎 【AI 抬头 · 独立审查】" + VERDICT_LABEL[verdict["verdict"]]
            + "(" + (verdict["reason"] or "未给出") + ")建议:"
            + (verdict["suggestion"] or "未给出"))


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
        reset_stretch(state, cfg, now)
    state["last_event_at"] = now
    tool = str(data.get("tool_name") or data.get("toolName") or "Unknown")
    state["tool_calls"] += 1
    state["calls_since_reminder"] += 1
    state["by_tool"][tool] = state["by_tool"].get(tool, 0) + 1
    state["fail_streak"] = 0

    if tool in PRODUCTIVE_TOOLS:
        state["edits"] += 1
        state["edits_since_reminder"] += 1
        if cfg["edit_foldback"] and record_edit_hash(state, cfg, data):
            state["foldback_hit"] = True
            ti = data.get("tool_input") or data.get("toolInput") or {}
            state["foldback_path"] = str((ti.get("file_path") or ti.get("path") or ti.get("notebook_path") or "?"))[:120]
    elif tool == "Bash":
        cmd = extract_cmd(data)
        n = norm_cmd(cmd)
        r = raw_cmd(cmd)
        if n or r:
            recent = state.get("recent_cmds", [])
            recent.append({"t": now, "n": n, "r": r})
            state["recent_cmds"] = recent[-8:]
    log = state.get("recent_log", [])
    log.append({"tool": tool, "brief": (extract_cmd(data) or "")[:100], "ok": True})
    state["recent_log"] = log[-cfg["review_log_size"]:]

    # ── 提醒有效性结算(自适应) ──
    evaluate_pending(state, cfg)

    # ── 触发器按证据强度排序,命中第一个即返回 ──
    # 0a) 折返编辑:文件内容改了又改回
    if cfg["edit_foldback"] and state.get("foldback_hit"):
        state["foldback_hit"] = False
        fb_path = state.get("foldback_path", "")
        text = fire(
            state, cfg, now, "edit-foldback",
            nudge_text(
                state, cfg, now,
                "文件 {p} 的内容回到了先前见过的状态——改了又改回是典型的原地打转,"
                "请确认这条修改路径是否还有意义,或者你已经在这个文件上折返了多次。".format(
                    p=fb_path),
            ),
            kind_cooldown(cfg, "edit-foldback", cfg["cooldown_sec"]),
        )
        if text:
            return post_use_output(finalize_text(
                state, cfg, now, "edit-foldback",
                "文件内容折返: {p}".format(p=fb_path),
                text))
    # 0b) 完全相同的命令原样重复(参数级,证据最强)
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
                kind_cooldown(cfg, "exact-repeat", cfg["cooldown_sec"]),
            )
            if text:
                return post_use_output(finalize_text(
                    state, cfg, now, "exact-repeat",
                    "原样命令重复 {n} 次: {cmd}".format(n=hit["count"], cmd=hit["raw"][:100]),
                    text))
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
                kind_cooldown(cfg, "repeat-cmds", cfg["cooldown_sec"]),
            )
            if text:
                return post_use_output(finalize_text(
                    state, cfg, now, "repeat-cmds",
                    "相似命令重复 {n} 次: {p}".format(n=hit["count"], p=hit["prefix"]),
                    text))
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
            kind_cooldown(cfg, "long-run", cfg["cooldown_sec"]),
        )
        if text:
            return post_use_output(finalize_text(
                state, cfg, now, "long-run",
                "运行 {m:.0f} 分钟零修改".format(m=fmt_minutes(state, now)),
                text))
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
            kind_cooldown(cfg, "no-output", cfg["cooldown_sec"]),
        )
        if text:
            return post_use_output(finalize_text(
                state, cfg, now, "no-output",
                "{c} 次调用零修改".format(c=state["calls_since_reminder"]),
                text))
    # 3.5) 目标漂移巡检:每 N 次调用用独立审查者对照最初目标(需要 llm_api_key)
    if (cfg["drift_check"] and cfg.get("llm_api_key")
            and state["tool_calls"] - state.get("calls_at_last_drift", 0) >= cfg["drift_check_calls"]
            and now - state.get("last_drift_at", 0.0) >= cfg["drift_cooldown_sec"]):
        state["last_drift_at"] = now
        state["calls_at_last_drift"] = state["tool_calls"]
        state["drift_checks"] = state.get("drift_checks", 0) + 1
        verdict = call_llm_review(state, cfg, now, "goal-drift", "巡检:目标对齐",
                                  system=REVIEW_DRIFT_SYSTEM)
        if verdict and verdict["verdict"] in ("drifting", "stuck"):
            state["reviews"] = state.get("reviews", 0) + 1
            return post_use_output(
                "🎯 【AI 抬头 · 目标漂移巡检】判定: " + DRIFT_LABEL[verdict["verdict"]]
                + "\n漂移表现: " + (verdict["reason"] or "(未给出)")
                + "\n收束建议: " + (verdict["suggestion"] or "(未给出)")
                + "\n请对照最初任务,决定:把当前子任务收敛回主线 / 明确其为目标之一并告知用户 / 放弃。")

    # 4) 本地时钟锚点(最低优先级:空跑提醒优先,且其刚发出 2 分钟内时钟静默)
    if cfg["clock_tick"] and now - state.get("last_reminder_at", 0.0) >= 120:
        interval = state.get("clock_interval") or cfg["clock_tick_minutes"]
        tick_sec = interval * 60
        if (now - state.get("last_clock_tick_at", 0.0) >= tick_sec
                and fmt_minutes(state, now) >= cfg["clock_tick_minutes"]):
            state["last_clock_tick_at"] = now
            state["clock_ticks"] = state.get("clock_ticks", 0) + 1
            if cfg["clock_tick_backoff"]:
                state["clock_interval"] = min(max(interval * 2, 1), cfg["clock_tick_max_minutes"])
            else:
                state["clock_interval"] = cfg["clock_tick_minutes"]
            return post_use_output(clock_text(state, now))
    return None


def handle_post_fail(cfg, data, state, now):
    state["last_event_at"] = now
    state["fail_streak"] += 1
    state["total_failures"] += 1
    log = state.get("recent_log", [])
    log.append({"tool": str(data.get("tool_name") or data.get("toolName") or "Unknown"),
                "brief": (extract_cmd(data) or "")[:100], "ok": False})
    state["recent_log"] = log[-cfg["review_log_size"]:]
    if state["fail_streak"] < cfg["fail_streak_threshold"]:
        return None
    if now - state.get("last_fail_reminder_at", 0.0) < kind_cooldown(cfg, "fail-loop", cfg["fail_cooldown_sec"]):
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
    streak = state["fail_streak"]
    state["fail_streak"] = 0
    if cfg.get("lessons"):
        last_fail = next((e for e in reversed(state.get("recent_log", [])) if not e.get("ok")), None)
        if last_fail:
            register_lesson(
                norm_cmd(last_fail.get("brief", "")) or "未知模式",
                "连续失败 {n} 次后被打断——换方法前请先读错误定位根因,勿重复此方式".format(n=streak))
    return post_fail_output(finalize_text(
        state, cfg, now, "fail-loop",
        "连续失败 {n} 次".format(n=streak),
        text))


def handle_prompt(cfg, data, state, now):
    state["last_event_at"] = now
    state["prompt_resets"] += 1
    reset_stretch(state, cfg, now)
    # 捕获任务描述,供独立审查者使用(尽力而为)
    goal = data.get("prompt") or data.get("user_prompt") or ""
    if isinstance(goal, str) and goal.strip():
        state["last_goal"] = goal.strip()[:400]
    # 回合开始即注入历史教训,防"认坑后再踩"(内容未变则跳过,每 5 回合强制重注)
    if cfg.get("lessons"):
        lt = lessons_text(state)
        if lt:
            return {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": lt,
                }
            }
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
        reset_stretch(state, cfg, now)
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
