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
import threading
import time
import urllib.request
import uuid

PRODUCTIVE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit", "ApplyPatch"}

# 会话闲置超过该秒数后,下一次事件视为新的工作时段
STALE_AFTER_SEC = 2 * 3600

def _load_spec():
    """读取双端共享行为规格 spec.json(与脚本同仓库根);缺失/损坏时静默回退内建值。"""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "spec.json")
    try:
        with open(path, encoding="utf-8") as f:
            spec = json.load(f)
        if isinstance(spec, dict) and isinstance(spec.get("shared"), dict):
            return spec["shared"]
    except Exception:
        pass
    return {}


_SPEC = _load_spec()

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
    "review_log_floor": 40,
    "max_recent_cmds": 8,
    "settle_after_calls": 10,
    "adaptive_warmup": 5,
    "adaptive_low_rate": 0.3,
    "adaptive_high_rate": 0.7,
    "adaptive_backoff_mult": 2,
    "adaptive_backoff_cap": 4,
    "adaptive_tighten_mult": 0.75,
    "adaptive_tighten_floor_sec": 60,
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
    # ── 第三通道:投递请求 + 独立 watcher（v0.7）──
    # 钩子只写请求单就返回（不阻塞），常驻 watcher 用自己的 key 去审，结论回投后
    # 由下一次钩子事件捎带注入。主 agent 全程不在链路上 → 独立性最强。
    # 默认关闭：它花用户自己的 API 额度，只在升级阶梯末档触发。
    # 契约见 hooks/REVIEW_CHANNEL.md。
    "deliver_review": False,
    "escalate_after_reminders": 3,
    "request_poll_sec": 2,
    "request_ttl_sec": 900,
    "result_ttl_sec": 900,
    "result_settle_calls": 60,
}

for _k, _v in _SPEC.items():
    if _k in DEFAULTS:
        DEFAULTS[_k] = _v

BOOL_KEYS = {"enabled", "stop_check", "clock_tick", "llm_review", "edit_foldback", "adaptive", "drift_check", "lessons", "clock_tick_backoff", "deliver_review"}

REVIEW_SYSTEM = (
    "你是 AI 编码代理的独立行为审查员。主代理看不到你的存在,你只依据给它的任务描述"
    "与最近工具调用记录做判断。判断标准:最近的调用是否持续带来新信息、是否朝着任务"
    "目标推进;同样的命令反复执行、长时间没有任何文件修改、连续失败后仍在重试,都是"
    "空跑迹象。只输出一个 JSON 对象,不要输出任何其他文字:\n"
    '{"verdict":"on-track|drifting|stuck","reason":"一句话依据","suggestion":"一句话建议"}\n'
    "on-track=仍在正轨;drifting=有偏航/空转迹象;stuck=确认空跑或卡死。"
)

VERDICT_LABEL = {"on-track": "仍在正轨", "drifting": "有偏航迹象", "stuck": "空跑确认"}

# 模糊型触发:统计信号可能误报,on-track 短确认有意义。
# 机械证据型(重复/折返/失败循环/结束前审查)的触发条件本身就是"非正轨"的证据,
# 审查者回 on-track 时忽略判定、保留完整静态清单,避免自相矛盾。
FUZZY_KINDS = {"no-output", "long-run"}


def clip_goal(goal, head=120, tail=80):
    """目标截断保两端:约束/禁区通常写在任务描述的尾部。"""
    goal = goal or ""
    if len(goal) <= head + tail:
        return goal
    return goal[:head] + " … " + goal[-tail:]

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


def review_channel_dir():
    """第三通道投递区。与 watcher 共用同一目录（见 REVIEW_CHANNEL.md）。"""
    d = os.path.join(state_dir(), "review")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return d


def _id_safe(s):
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(s))[:160]


def deliver_review_request(cfg, state, now, kind, detail):
    """把一次末档审查投递出去（第三通道）。返回 request_id 或 None。

    只在升级阶梯到 escalate_after_reminders 时才可能被调用（由调用点把关）。
    幂等：同一 request_id 的请求单若已存在则跳过。
    """
    if not cfg.get("deliver_review"):
        return None
    if not (cfg.get("llm_api_key") or "").strip():
        return None
    sid = state.get("session_id", "default")
    rid = "%s-%s-%s" % (_id_safe(sid), state.get("tool_calls", 0), kind)
    path = os.path.join(review_channel_dir(), "req_%s.json" % _id_safe(rid))
    if os.path.exists(path):
        return rid  # 已投递，幂等
    req = {
        "v": 1,
        "request_id": rid,
        "session_id": sid,
        "kind": kind,
        "detail": detail,
        "created_at": now,
        "tool_calls": state.get("tool_calls", 0),
        "prompt_resets": state.get("prompt_resets", 0),
        "goal_snapshot": clip_goal(state.get("last_goal")),
        "system": REVIEW_SYSTEM,
        "material": build_review_material(state, cfg, now, kind, detail),
    }
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(req, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except Exception:
        return None
    return rid


def take_review_results(state, cfg, now):
    """扫投递区取回结论。返回待注入文本列表（可能为空）。

    三道闸（见 REVIEW_CHANNEL.md）：场景指纹 / 过期 / 步数。
    不过闸的**只删文件不注入**。
    """
    d = review_channel_dir()
    texts = []
    try:
        names = os.listdir(d)
    except Exception:
        return texts
    sid = state.get("session_id", "default")
    for name in names:
        if not (name.startswith("res_") and name.endswith(".json")):
            continue
        p = os.path.join(d, name)
        try:
            with open(p, encoding="utf-8") as f:
                res = json.load(f)
        except Exception:
            _safe_rm(p)
            continue
        if not isinstance(res, dict):
            _safe_rm(p)
            continue
        # 不属于本会话的结论不要碰（可能是另一个会话的）
        if res.get("session_id") and str(res["session_id"]) != str(sid):
            continue
        # 闸1: 场景指纹——用户换过指令则结论作废
        if res.get("prompt_resets", 0) != state.get("prompt_resets", 0):
            _safe_rm(p)
            continue
        # 闸2: 过期
        if now - float(res.get("finished_at", 0)) > cfg.get("result_ttl_sec", 900):
            _safe_rm(p)
            continue
        # 闸3: 步数——结论对应几十步前的现场，AI 走远了
        steps_back = state.get("tool_calls", 0) - int(res.get("tool_calls", 0))
        if steps_back > cfg.get("result_settle_calls", 60):
            _safe_rm(p)
            continue
        if res.get("verdict") == "error":
            _safe_rm(p)  # 失败不注入，静默丢弃（可观测性靠 watcher.log）
            continue
        texts.append(format_review_result(res, max(0, steps_back)))
        _safe_rm(p)
    return texts


def _safe_rm(path):
    try:
        os.remove(path)
    except Exception:
        pass


def format_review_result(res, steps_back):
    """把结论格式化成注入文本。必须标注场景，不能只说'N 步前'。"""
    label = VERDICT_LABEL.get(res.get("verdict", ""), res.get("verdict", ""))
    lines = [
        "🎯 【AI 抬头 · 独立审查者】判定: " + label,
        "该结论基于 %d 步前「%s」的现场。" % (steps_back, (res.get("detail") or "未知触发")[:60]),
        "漂移表现: " + (res.get("reason") or "(未给出)"),
        "收束建议: " + (res.get("suggestion") or "(未给出)"),
        "请对照最初任务,决定:把当前子任务收敛回主线 / 明确其为目标之一并告知用户 / 放弃。",
    ]
    return "\n".join(lines)


def kind_cooldown(cfg, kind, base):
    """按该类提醒的历史有效率调整冷却:无效(<30%)翻倍降噪,有效(>70%)缩短到 3/4。"""
    if not cfg.get("adaptive"):
        return base
    stats = load_adaptive().get(kind)
    if not stats or stats.get("fired", 0) < cfg["adaptive_warmup"]:
        return base
    rate = stats["effective"] / max(1, stats["fired"])
    if rate < cfg["adaptive_low_rate"]:
        return min(base * cfg["adaptive_backoff_mult"], base * cfg["adaptive_backoff_cap"])
    if rate > cfg["adaptive_high_rate"]:
        return max(base * cfg["adaptive_tighten_mult"], cfg["adaptive_tighten_floor_sec"])
    return base


def evaluate_pending(state, cfg):
    """提醒发出 10 次调用后结算有效性:期间出现修改/失败停止/命令模式变化都算有效。"""
    pending = state.get("pending_reminders", [])
    if not pending:
        return
    remaining = []
    changed = False
    to_settle = []
    for p in pending:
        if state["tool_calls"] - p["calls_at"] < cfg["settle_after_calls"]:
            remaining.append(p)
            continue
        to_settle.append(p)
    if to_settle:
        # 一次读、一次写:避免循环内重复 load/save(并发下会互相覆盖丢计数)。
        stats = load_adaptive()
        for p in to_settle:
            effective = _is_effective(state, p)
            s = stats.setdefault(p["kind"], {"fired": 0, "effective": 0})
            s["fired"] = s.get("fired", 0) + 1
            if effective:
                s["effective"] = s.get("effective", 0) + 1
        save_adaptive(stats)
        changed = True
    if changed or remaining != pending:
        state["pending_reminders"] = remaining


def _is_effective(state, p):
    """按触发类型判断"提醒是否真的改变了行为"，而不是笼统看有没有编辑。"""
    kind = p["kind"]
    if kind in ("repeat-cmds", "exact-repeat"):
        cur = state.get("recent_cmds", [])
        return bool(cur) and cur[-1]["n"] != p.get("norm", "")
    if kind == "fail-loop":
        # fail_streak 会被任意一次成功清零,结算时几乎恒 0,不能作判据;
        # 用累计失败数(total_failures 只增不清)对比快照,判"提醒后是否还有新失败"。
        return state.get("total_failures", 0) == p.get("fails_at", -1)
    if kind in ("no-output", "long-run", "edit-foldback"):
        return state["edits"] - p["edits_at"] > 0
    return (state["edits"] - p["edits_at"] > 0) or state["fail_streak"] == 0


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
        "calls_at_stretch_start": 0,
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
        data = json.dumps(state, ensure_ascii=False, indent=1)
        for attempt in range(3):
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write(data)
                os.replace(tmp, state_path(sid))
                return
            except OSError:
                # Windows 上 replace 可能撞上并发读句柄的共享冲突；
                # 静默放弃 = 直接丢一次状态更新，重试几次再放弃。
                if attempt == 2:
                    return
                time.sleep(0.05 * (attempt + 1))
    except Exception:
        pass


def _pid_alive(pid):
    """探测 PID 是否存活。Windows 用 OpenProcess；POSIX 用 os.kill(pid, 0)。"""
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            SYNCHRONIZE = 0x00100000
            k32 = ctypes.windll.kernel32
            h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, False, pid)
            if h:
                k32.CloseHandle(h)
                return True
            return False
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


class state_lock(object):
    """跨进程文件锁：保护「load → 处理 → save」整段，避免并发钩子互相覆盖状态。

    P2-1：钩子是独立短命进程，多个事件可能并发落到同一 <sid>.json；
    没有锁时后写的会整体覆盖先写的（实测 20 并发只保住 7~9 次记录）。

    设计要点（避开 Windows 上踩过的坑）：
      * 锁内容 = `pid:token`，token 为本次获取的随机串。释放时校验 token——
        只删自己的锁，绝不误删别人重新抢到的锁。
      * 接管失效锁用 **rename**（原子），不用 os.remove——
        直接 remove 可能删掉「刚被别人抢到的新锁」，造成双持有者。
      * 接管判据 = PID 已死 **或** 锁文件心跳超过 stale_after 秒（见下）。
      * 有界等待：拿不到锁最多等 wait 秒就放行，绝不卡住会话（可用性优先）。

    ★ v0.8 心跳：以前接管只看「文件 mtime 是否过期」，可 mtime 只有创建时被写一次，
    于是它实际等于"锁创建了多久"而非"持锁者还活着多久"。两个后果：
      - **POSIX 上 `os.kill(pid, 0)` 对 zombie 进程返回成功** → `_pid_alive` 判活，
        僵尸锁无人接管（要等 STALE_AFTER 兜底）；
      - 反之若临界区真比 STALE_AFTER 长（大状态文件 + 慢盘），活锁会被误接管 → 双持有者。
    现在持锁者起一个心跳线程定期 touch mtime，`STALE_AFTER` 才真正表示
    "持锁者最近一次动静在 N 秒前"，两个方向都准。
    """

    STALE_AFTER = 15.0     # 心跳超过这么久没更新，即可安全接管
    HEARTBEAT_SEC = 5.0    # 心跳间隔（须显著小于 STALE_AFTER，留冗余）

    def __init__(self, sid, wait=None):
        self.path = state_path(sid) + ".lock"
        if wait is None:
            try:
                wait = float(os.environ.get("LOOKUP_LOCK_WAIT", "2.0"))
            except Exception:
                wait = 2.0
        self.wait = wait
        self.token = "%d:%s" % (os.getpid(), uuid.uuid4().hex)
        self.acquired = False
        self._hb_stop = threading.Event()
        self._hb_thread = None

    def _holder_token(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                return (f.read() or "").strip()
        except Exception:
            return ""

    def heartbeat(self):
        """刷新锁文件 mtime，告诉等待者"我还活着"。只在自己仍持有锁时刷。"""
        if not self.acquired:
            return
        # 只在 token 仍属自己时 touch，避免给"已被接管的新锁"续命。
        if self._holder_token() != self.token:
            self._hb_stop.set()
            return
        try:
            os.utime(self.path, None)
        except OSError:
            pass

    def _start_heartbeat(self):
        def _loop():
            # wait(x) 在 stop 被 set 时立即返回，因此这里天然随 __exit__ 退出。
            while not self._hb_stop.wait(self.HEARTBEAT_SEC):
                self.heartbeat()
        t = threading.Thread(target=_loop, name="state-lock-hb", daemon=True)
        t.start()
        self._hb_thread = t

    def _stop_heartbeat(self):
        self._hb_stop.set()
        t = self._hb_thread
        if t is not None:
            try:
                t.join(timeout=1.0)
            except Exception:
                pass
            self._hb_thread = None

    def _is_stale(self, raw):
        """锁是否已失效：PID 已死，或心跳久未更新（兜住 PID 复用/zombie）。"""
        try:
            age = time.time() - os.path.getmtime(self.path)
        except OSError:
            return True
        if age > self.STALE_AFTER:
            return True
        pid = 0
        try:
            pid = int(raw.split(":", 1)[0] or "0")
        except Exception:
            pid = 0
        return not _pid_alive(pid)

    def __enter__(self):
        deadline = time.time() + max(0.0, self.wait)
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, self.token.encode("ascii"))
                os.close(fd)
                self.acquired = True
                self._start_heartbeat()      # ★ v0.8：持锁期间持续刷 mtime
                return self
            except FileExistsError:
                raw = self._holder_token()
                if raw and self._is_stale(raw):
                    # 原子接管：rename 到本进程专属名，只有一方能成功，
                    # 且只对「刚才那个失效锁」生效，不会误伤新持有者。
                    grab = "%s.stale.%d.%s" % (self.path, os.getpid(), uuid.uuid4().hex[:8])
                    try:
                        os.rename(self.path, grab)
                        try:
                            os.remove(grab)
                        except OSError:
                            pass
                    except OSError:
                        pass          # 已被别人接管或已释放，下一轮重试
                    continue
                if time.time() >= deadline:
                    return self      # 超时：不阻塞会话，降级为无锁执行
                time.sleep(0.02)
            except OSError:
                # 非 EEXIST 的瞬时错误（Windows 共享冲突/杀软扫描等）：
                # 等在 deadline 内重试而不是立刻降级为无锁——无锁执行
                # 会造成并发写互相覆盖（P2-1 的丢更新）。超时才放行保可用性。
                if time.time() >= deadline:
                    return self
                time.sleep(0.02)

    def __exit__(self, *exc):
        if not self.acquired:
            return False
        self._stop_heartbeat()               # ★ v0.8：先停心跳，再删锁
        # 只删自己的锁：token 不匹配说明锁已被接管/重抢，绝不能动别人的。
        if self._holder_token() == self.token:
            try:
                os.remove(self.path)
            except OSError:
                pass
        return False


def reset_stretch(state, cfg, now):
    """开启新的工作时段:重置回合内的节奏计数,保留会话累计。

    P2-2：`reminders` 也必须在这里清零。原实现只加不减，同一会话累计 12 次后
    配额永久耗尽——用户开新任务也不再有任何提醒（新任务静默失守）。
    配额的本意是「单个回合内别刷屏」,不是「整个会话只能用 12 次」。
    """
    state["started_at"] = now
    state["calls_at_stretch_start"] = state["tool_calls"]
    state["calls_since_reminder"] = 0
    state["edits_since_reminder"] = 0
    state["fail_streak"] = 0
    state["recent_cmds"] = []
    state["clock_interval"] = 0
    state["reminders"] = 0
    # 注意:这里绝不能动 last_reminder_at / last_fail_reminder_at。
    # 证据窗口(上面清零的那些)已经保证新回合不可能"立刻触发",而把冷却基准
    # 拉到 now 会给每个新回合装填一个完整的 cooldown_sec 静默窗——回合开头
    # 5 分钟内的空跑全部漏报(e2e 场景 A 实测)。


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


_SCRIPT_EXTS = _SPEC.get("script_extensions", ["py", "js", "ts", "mjs", "cjs", "sh", "ps1", "bat"])
SCRIPT_RX = re.compile(r"\.(?:" + "|".join(map(re.escape, _SCRIPT_EXTS)) + r")$", re.I)
INTERPRETERS = set(_SPEC.get("interpreters",
                             ["python", "python3", "py", "node", "bash", "sh", "pwsh", "powershell", "perl", "ruby"]))
SEQ_RX = re.compile(r"[\d_-]+(?=\.\w+$)")


def norm_cmd(cmd):
    """把一条命令归一化成"程序 + 目标主体",用于识别换汤不换药的重复执行。

    python _peek2.py / python _peek3.py  →  "python _peek.py"   (判定重复)
    grep -rn "foo" a / grep -rn "bar" b  →  不同                (不判重复)
    """
    s = (cmd or "").strip().lower()
    if not s:
        return ""
    s = re.sub(r"\s+", " ", s)
    parts = re.split(r"&&|\|\||;", s)
    s = parts[-1].strip() if parts else s
    toks = [t for t in s.split(" ") if t]
    if not toks:
        return ""
    head = toks[0].replace("\\", "/").split("/")[-1].strip("\"'")
    head = re.sub(r"\.exe$", "", head)

    if head in INTERPRETERS and len(toks) > 1:
        second = toks[1].replace("\\", "/").split("/")[-1].strip("\"'")
        if SCRIPT_RX.search(second):
            return (head + " " + SEQ_RX.sub("", second)).strip()
        return s   # 非脚本形态不截断:任何 token 截断都会制造误报(-c 脚本/build2/git 分支)

    return s


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


def fire(state, cfg, now, kind, text, cooldown, fail_at=None, stamp_key=None,
         cooldown_key="last_reminder_at"):
    """统一发火口:所有触发器的状态维护都必须走这里,不许各写一份。

    参数:
      fail_at      —— pending 快照里的连败数。默认取当前 fail_streak;
                      但 no-output/long-run 等在 handle_post_use 里调用时,
                      fail_streak 已被提前清零,须由调用方传入清零前的值。
      stamp_key    —— 除 last_reminder_at 外,额外要戳的冷却键名(如 fail-loop
                      的 "last_fail_reminder_at")。为 None 则不额外戳。
      cooldown_key —— 冷却以哪个键为准。fail-loop 用独立冷却,故传
                      "last_fail_reminder_at";其余默认共用 last_reminder_at。

    ★ v0.8 收编 fail-loop:此前 handle_post_fail 绕过本函数、手工复刻了
    下面全部 7 项状态维护。那种写法让"新增一个触发器"必须再抄一份,
    且 F1 缺陷(漏抄 3 项)正是这么来的。现在 fail-loop 也走本函数,
    Python 侧不再有任何手工路径。
    """
    if not cfg["enabled"]:
        return None
    if state["reminders"] >= cfg["max_reminders"]:
        return None
    if now - state.get(cooldown_key, 0.0) < cooldown:
        return None
    state["reminders"] += 1
    state["last_reminder_at"] = now
    if stamp_key:
        state[stamp_key] = now
    state["last_trigger"] = kind
    state["calls_since_reminder"] = 0
    state["edits_since_reminder"] = 0
    cur = state.get("recent_cmds", [])
    pending = state.setdefault("pending_reminders", [])
    pending.append({
        "kind": kind,
        "calls_at": state["tool_calls"],
        "edits_at": state["edits"],
        "fail": state["fail_streak"] if fail_at is None else fail_at,
        "fails_at": state.get("total_failures", 0),
        "norm": cur[-1]["n"] if cur else "",
    })
    state["pending_reminders"] = pending[-10:]
    return text


def local_hm(ts):
    return time.strftime("%H:%M", time.localtime(ts))


def nudge_text(state, cfg, now, specific):
    mins = fmt_minutes(state, now)
    lines = [
        "🔔 【AI 抬头 · 中途自我审查】时钟 {h},本段 {m:.0f} 分钟/{c} 次调用,修改 {e} 次。停一下,自查:".format(
            h=local_hm(now), m=mins,
            c=max(0, state["tool_calls"]
                  - state.get("calls_at_stretch_start", state["tool_calls"] - state["calls_since_reminder"])),
            e=state["edits_since_reminder"]),
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


def brief_of(data, tool):
    """结构化参数摘要:Bash 取命令,文件类工具取路径,其余取参数 JSON 前段。"""
    if tool == "Bash":
        return extract_cmd(data)[:100]
    ti = data.get("tool_input") or data.get("toolInput") or {}
    if isinstance(ti, dict):
        for k in ("file_path", "path", "notebook_path", "url", "query"):
            if ti.get(k):
                return str(ti[k])[:100]
        try:
            return json.dumps(ti, ensure_ascii=False)[:80]
        except Exception:
            return ""
    return ""


def build_review_material(state, cfg, now, kind, detail):
    log = state.get("recent_log", [])
    # 先按"连续同工具段"折叠、再取最近 N 段:关键旧证据不因新同形刷屏被挤出窗口
    runs = []
    for e in log:
        if runs and runs[-1][0].get("tool") == e.get("tool"):
            runs[-1].append(e)
        else:
            runs.append([e])
    runs = runs[-cfg["review_log_size"]:]
    log_lines = []
    for run in runs:
        t = run[0].get("tool", "?")
        fails = sum(1 for e in run if not e.get("ok"))
        head = (run[0].get("brief") or "无")[:40]
        tail = (run[-1].get("brief") or "")[:40]
        if len(run) > 1:
            mark = "✗{}/{}, ".format(fails, len(run)) if fails else ""
            log_lines.append("{t}×{n}({mark}{a} … {b})".format(
                t=t, n=len(run), mark=mark, a=head, b=tail))
        else:
            log_lines.append("[{t}{ok}] {b}".format(
                t=t, ok=" ✗" if fails else "", b=(run[0].get("brief") or "无")[:60]))
    dist = ", ".join("{k}×{v}".format(k=k, v=v) for k, v in
                     sorted(state.get("by_tool", {}).items(), key=lambda kv: -kv[1])[:4])
    parts = [
        "【审查材料】触发: " + kind,
        "【任务】" + clip_goal(state.get("last_goal")),
        "【统计】{m:.0f} 分钟/{c} 次调用/修改 {e},连败 {f},累计败 {t}。".format(
            m=fmt_minutes(state, now), c=state.get("tool_calls", 0),
            e=state.get("edits", 0), f=state.get("fail_streak", 0),
            t=state.get("total_failures", 0)),
        "【分布】" + (dist or "无"),
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
    on-track 仅对模糊型触发(no-output/long-run)用短确认;机械证据型忽略判定,
    保留完整静态清单——连续失败/零产出是既成事实,不能被"仍在正轨"放行。

    另外:升级阶梯到末档且开启 deliver_review 时,投递一次第三通道审查
    （不在此处等待——watcher 异步审,结论由后续钩子事件捎带注入）。"""
    # 第三通道投递：末档才投，避免常态消耗用户 API 额度
    if (cfg.get("deliver_review")
            and state.get("reminders", 0) >= cfg.get("escalate_after_reminders", 3)):
        deliver_review_request(cfg, state, now, kind, detail)
    if not cfg.get("llm_review"):
        return static_text
    verdict = call_llm_review(state, cfg, now, kind, detail)
    if verdict is None:
        return static_text
    state["reviews"] = state.get("reviews", 0) + 1
    if verdict["verdict"] == "on-track":
        if kind not in FUZZY_KINDS:
            return static_text
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
    fail_before = state["fail_streak"]       # 保存清零前的连败数,供 pending 快照
    state["fail_streak"] = 0

    # 第三通道取回：watcher 异步审完的结论在此捎带注入（先于本地触发器）。
    # 取回要过三道闸（场景指纹/过期/步数），不过闸的静默丢弃——见 REVIEW_CHANNEL.md。
    # 注意：只注入、**不提前 return**——否则本次调用进不了 recent_log，
    # 材料会与实际调用序列脱节（tool_calls 已 +1，日志却没有这条）。
    pending_verdicts = take_review_results(state, cfg, now) if cfg.get("deliver_review") else []
    carried = "\n".join(pending_verdicts) if pending_verdicts else ""

    def emit(text):
        """带上前一次捎带的独立审查结论一起输出(有则拼在前)。

        调用点传进来的 text 一律非空;carried 为空时退化为原行为。
        只能拼一次——carried 在闭包里恒真值,递归实现会无限递归。
        """
        if carried:
            return post_use_output(carried + "\n" + text)
        return post_use_output(text)

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
            state["recent_cmds"] = recent[-cfg["max_recent_cmds"]:]
    log = state.get("recent_log", [])
    log.append({"tool": tool, "brief": brief_of(data, tool), "ok": True})
    # 原始条目保留窗口放宽(材料构造时才按段折叠压缩),否则关键旧证据进不了材料
    state["recent_log"] = log[-max(cfg["review_log_floor"], cfg["review_log_size"]):]

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
            fail_at=fail_before,
        )
        if text:
            return emit(finalize_text(
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
                fail_at=fail_before,
            )
            if text:
                return emit(finalize_text(
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
                fail_at=fail_before,
            )
            if text:
                return emit(finalize_text(
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
            fail_at=fail_before,
        )
        if text:
            return emit(finalize_text(
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
            fail_at=fail_before,
        )
        if text:
            return emit(finalize_text(
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
            return emit(
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
            return emit(clock_text(state, now))
    # 无本地触发的普通事件也要把捎带结论送出去:结论文件在 take_review_results
    # 里已被删除,不在此注入就永远丢失(JS 端 deliver() 无此问题,任何事件都注入)。
    if carried:
        return post_use_output(carried)
    return None


def handle_post_fail(cfg, data, state, now):
    state["last_event_at"] = now
    state["fail_streak"] += 1
    state["total_failures"] += 1
    log = state.get("recent_log", [])
    log.append({"tool": str(data.get("tool_name") or data.get("toolName") or "Unknown"),
                "brief": brief_of(data, str(data.get("tool_name") or data.get("toolName") or "Unknown")), "ok": False})
    state["recent_log"] = log[-max(cfg["review_log_floor"], cfg["review_log_size"]):]
    if state["fail_streak"] < cfg["fail_streak_threshold"]:
        return None
    # ★ v0.8:fail-loop 收编进 fire()。此前这里是手工复刻的整套状态维护,
    #   漏抄了 3 项(F1)。现在前置门槛(连败阈值)留在这里,其余全部交给 fire()。
    #   冷却基准用 last_fail_reminder_at(独立于其它触发器),故 cooldown_key
    #   与 stamp_key 都指向它。
    text = fire(
        state, cfg, now, "fail-loop",
        "\n".join([
            "🔔 【AI 抬头 · 失败循环】你已连续失败 {n} 次。请勿再用同样的方式重试:".format(
                n=state["fail_streak"]),
            "1. 完整读取最近一次的错误信息,定位根因(而不是只看表面症状);",
            "2. 判断:这是可以修复的问题,还是方法本身不可行?",
            "3. 换方法、修复后再试;若连续两次换方法仍失败,停下来向用户汇报卡点。",
        ]),
        kind_cooldown(cfg, "fail-loop", cfg["fail_cooldown_sec"]),
        stamp_key="last_fail_reminder_at",
        cooldown_key="last_fail_reminder_at",
    )
    if not text:
        return None
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

    # P2-1：整段 load→处理→save 持锁，避免并发钩子互相覆盖状态。
    with state_lock(sid):
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
