#!/usr/bin/env python3
"""双端一致性金标测试(Python 侧)。

与 test/golden.mjs 消费同一份 test/golden_cases.json,校验:
  1) norm_cmd 输出与金标完全一致;
  2) spec.json 的 shared 键在 DEFAULTS 中全部存在且值一致(规格接入完整性);
  3) 会话回放的触发序列与金标一致(触发行为跨端一致)。

用法: python test/golden_check.py   (退出码 0 = 全过)
"""
import importlib.util
import json
import os
import sys

# Windows 宿主默认 ANSI 代码页(cp1252/gbk 之外会炸),测试输出含中文,统一走 UTF-8。
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

os.environ["LOOKUP_STATE_DIR"] = os.path.join(HERE, "_golden_state_py")

spec_path = os.path.join(ROOT, "spec.json")
with open(spec_path, encoding="utf-8") as f:
    SPEC = json.load(f)["shared"]

sys.argv = ["golden"]
mod_spec = importlib.util.spec_from_file_location("lk", os.path.join(ROOT, "hooks", "lookup_hook.py"))
lk = importlib.util.module_from_spec(mod_spec)
mod_spec.loader.exec_module(lk)

fails = 0


def check(name, ok, detail=""):
    global fails
    if not ok:
        fails += 1
        print("FAIL  %s %s" % (name, detail))
    else:
        print("PASS  %s" % name)


# 1) norm_cmd 金标
for case in json.load(open(os.path.join(HERE, "golden_cases.json"), encoding="utf-8"))["norm"]:
    got = lk.norm_cmd(case["cmd"])
    check("norm %r" % case["cmd"], got == case["want"], "got %r want %r" % (got, case["want"]))

# 2) spec 接入完整性:shared 键必须全部落到 DEFAULTS 且值一致
for k, v in SPEC.items():
    if k in ("interpreters", "script_extensions"):
        continue
    ok = k in lk.DEFAULTS and lk.DEFAULTS[k] == v
    check("spec key %s" % k, ok,
          "" if ok else "got %r want %r" % (lk.DEFAULTS.get(k), v))
check("spec interpreters", set(SPEC["interpreters"]) == set(lk.INTERPRETERS))
import re as _re
rx_ok = all(_re.search(lk.SCRIPT_RX, "x." + e) for e in SPEC["script_extensions"])
check("spec script_extensions rx", rx_ok)

# 3) 会话回放
CASES = json.load(open(os.path.join(HERE, "golden_cases.json"), encoding="utf-8"))["sessions"]
STEP_SEC = 60.0
for session in CASES:
    cfg = dict(lk.DEFAULTS)
    cfg.update({
        "clock_tick": False, "drift_check": False, "llm_review": False,
        "adaptive": False, "cooldown_sec": 0, "fail_cooldown_sec": 0,
    })
    state = lk.new_state("golden-" + session["name"][:20], 1000.0)
    kinds = []
    now = 1000.0
    for ev in session["events"]:
        now += STEP_SEC
        data = {"tool_name": ev["tool"], "tool_input": {"command": ev.get("cmd", "")}}
        if ev.get("fail"):
            out = lk.handle_post_fail(cfg, data, state, now)
        else:
            out = lk.handle_post_use(cfg, data, state, now)
        text = str(out)
        for marker in ("重复执行", "原样执行", "失败循环", "本地时钟", "还没有任何文件被修改",
                       "次工具调用没有产生任何文件修改", "折返"):
            if marker in text and marker not in kinds:
                kinds.append(marker)
    ok = kinds == session["want_kinds"]
    check("session %s" % session["name"], ok, "got %s want %s" % (kinds, session["want_kinds"]))

# 4) 行为级变异(补金标盲区):spec 键不能只落进 DEFAULTS,必须在真实执行路径上生效。
#    断言方式 = 走真实 handle_post_use / handle_post_fail 观察可观测行为,
#    不手工复刻任何被测逻辑(手工复刻 = 假阳性,教训见 WorkBuddy 2026-10-06 复核信 §3.2)。
def probe_log_window(path, floor):
    cfg2 = dict(lk.DEFAULTS)
    cfg2["review_log_floor"] = floor
    cfg2["review_log_size"] = 12
    st = lk.new_state("mutation-%s-%d" % (path, floor), 1000.0)
    for i in range(60):
        data = {"tool_name": "Bash", "tool_input": {"command": "cmd%d" % i}}
        if path == "fail":
            lk.handle_post_fail(cfg2, data, st, 1000.0 + i)
        else:
            lk.handle_post_use(cfg2, data, st, 1000.0 + i)
    return len(st["recent_log"])


check("mutation review_log_floor=137 fail path keeps all", probe_log_window("fail", 137) == 60)
check("mutation review_log_floor=137 success path keeps all", probe_log_window("use", 137) == 60)
check("mutation review_log_floor=5 falls back to review_log_size",
      probe_log_window("fail", 5) == 12 and probe_log_window("use", 5) == 12)
check("default review_log_floor=40 caps the window",
      probe_log_window("fail", lk.DEFAULTS["review_log_floor"]) == 40
      and probe_log_window("use", lk.DEFAULTS["review_log_floor"]) == 40)

# 5) F1 回归：fail-loop 必须补齐 fire() 的三项状态维护。
#    断言方式 = 走真实 handle_post_fail / handle_post_use，观察可观测后果，
#    不手工复刻被测逻辑（回退验证：删掉补齐项后本断言必须变红）。
def probe_fail_loop_state():
    cfg = dict(lk.DEFAULTS)
    cfg.update({"clock_tick": False, "drift_check": False, "llm_review": False,
                "adaptive": False, "cooldown_sec": 0, "fail_cooldown_sec": 0})
    st = lk.new_state("f1-failloop", 1000.0)
    # 先累一点静默计数（但不触发 no-output：阈值之上再插入 fail-loop）
    for i in range(3):
        lk.handle_post_use(cfg, {"tool_name": "Read", "tool_input": {"file_path": "a%d" % i}},
                           st, 1000.0 + i)
    before = dict(st)
    # 3 连败 → fail-loop
    for i in range(3):
        lk.handle_post_fail(cfg, {"tool_name": "Bash", "tool_input": {"command": "boom"}},
                            st, 1100.0 + i)
    return before, dict(st)


_b, _a = probe_fail_loop_state()
check("F1 pending_reminders 登记（自适应统计不被架空）",
      len(_a.get("pending_reminders", [])) >= 1
      and _a["pending_reminders"][-1].get("kind") == "fail-loop",
      "pending=%r" % _a.get("pending_reminders"))
check("F1 fail-loop 清零 calls_since_reminder",
      _a["calls_since_reminder"] == 0, "got %r" % _a["calls_since_reminder"])
check("F1 fail-loop 清零 edits_since_reminder",
      _a["edits_since_reminder"] == 0, "got %r" % _a["edits_since_reminder"])


# 6) F1 后果级断言：fail-loop 未清零计数时，no-output 会被提前误触发。
#    用 call_nudge_interval 收窄窗口，使差异可观测。
def probe_f1_no_output_early():
    cfg = dict(lk.DEFAULTS)
    cfg.update({"clock_tick": False, "drift_check": False, "llm_review": False,
                "adaptive": False, "cooldown_sec": 0, "fail_cooldown_sec": 0,
                "call_nudge_interval": 8})
    st = lk.new_state("f1-nofire", 1000.0)
    now = 1000.0
    # 6 次无修改，未达阈值
    for i in range(6):
        now += 1
        lk.handle_post_use(cfg, {"tool_name": "Read", "tool_input": {"file_path": "r%d" % i}}, st, now)
    # 3 连败打断（fail-loop）
    for i in range(3):
        now += 1
        lk.handle_post_fail(cfg, {"tool_name": "Bash", "tool_input": {"command": "boom"}}, st, now)
    # 再 3 次调用：清零正确则累计 3 < 8，no-output 不该出现
    fired = False
    for i in range(3):
        now += 1
        out = lk.handle_post_use(cfg, {"tool_name": "Read", "tool_input": {"file_path": "z%d" % i}}, st, now)
        if "没有产生任何文件修改" in str(out):
            fired = True
    return fired


check("F1 后果：fail-loop 后 no-output 不被提前误触发",
      probe_f1_no_output_early() is False)


# 7) P2-3 回归：evaluate_pending 必须"一次读、一次写"。
#    断言方式 = 计数落盘次数（不靠并发竞态——竞态下"精确终值"本就不可达，
#    没有跨进程锁就必然丢，那是 P2-1 的范畴，不能拿来当本条的判据）。
#    等价的确定性断言：同一轮结算 K 条 pending，save 只应发生 1 次；
#    循环内 save 的写法会 save K 次。用 monkeypatch 数 open() 写次数。
def probe_p23_save_count():
    import builtins
    cfg = dict(lk.DEFAULTS)
    st = lk.new_state("p23-count", 1000.0)
    st["tool_calls"] = 1000
    st["total_failures"] = 0
    st["edits"] = 999
    st["pending_reminders"] = [
        {"kind": "long-run", "calls_at": 10, "edits_at": 999, "fails_at": 0, "norm": ""},
        {"kind": "no-output", "calls_at": 10, "edits_at": 999, "fails_at": 0, "norm": ""},
        {"kind": "edit-foldback", "calls_at": 10, "edits_at": 999, "fails_at": 0, "norm": ""},
    ]
    lk.save_adaptive({})
    saves = {"n": 0}
    real_open = builtins.open
    apath = lk.adaptive_path()

    def counting_open(file, mode="r", *a, **k):
        if os.path.abspath(str(file)) == os.path.abspath(apath) and "w" in mode:
            saves["n"] += 1
        return real_open(file, mode, *a, **k)

    builtins.open = counting_open
    try:
        lk.evaluate_pending(st, cfg)
    finally:
        builtins.open = real_open
    return saves["n"], dict(lk.load_adaptive()), list(st["pending_reminders"])


_saves, _stats, _left = probe_p23_save_count()
check("P2-3 同轮 K 条 pending 只落盘 1 次", _saves == 1, "save 次数 = %r（循环内 save 会 = K）" % _saves)
check("P2-3 三类 pending 都记入 fired",
      all(_stats.get(k, {}).get("fired") == 1 for k in ("long-run", "no-output", "edit-foldback")),
      "stats=%r" % _stats)
check("P2-3 已结算的 pending 全部出队", _left == [], "got %r" % _left)


# 8) P2-2 回归：新回合（user prompt）必须重置 reminders 配额。
#    观察方式：把配额打满后开新回合，再制造一次可触发场景，必须还能提醒。
def probe_p22_quota_reset():
    cfg = dict(lk.DEFAULTS)
    cfg.update({"clock_tick": False, "drift_check": False, "llm_review": False,
                "adaptive": False, "cooldown_sec": 0, "fail_cooldown_sec": 0,
                "max_reminders": 2, "repeat_min_calls": 0})
    st = lk.new_state("p22", 1000.0)
    now = 1000.0
    # 打满配额：用 fail-loop 触发两次（阈值 3 连败）
    for _ in range(2):
        for i in range(3):
            now += 1
            lk.handle_post_fail(cfg, {"tool_name": "Bash", "tool_input": {"command": "boom"}}, st, now)
        for i in range(3):
            now += 1
            lk.handle_post_use(cfg, {"tool_name": "Read", "tool_input": {"file_path": "a%d" % i}}, st, now)
    assert st["reminders"] >= cfg["max_reminders"], "前置:配额应已打满，实际 %d" % st["reminders"]
    # 新回合
    now += 1
    lk.handle_prompt(cfg, {"prompt": "新任务:换一件事做"}, st, now)
    quota_after_prompt = st["reminders"]
    # 再制造一次可触发场景
    fired = False
    for i in range(3):
        now += 1
        out = lk.handle_post_fail(cfg, {"tool_name": "Bash", "tool_input": {"command": "boom"}}, st, now)
        if "失败循环" in str(out):
            fired = True
    return quota_after_prompt, fired


_quota, _fired = probe_p22_quota_reset()
check("P2-2 新回合重置 reminders 配额", _quota == 0, "重置后 reminders=%r（应为 0）" % _quota)
check("P2-2 新回合后仍能触发提醒（旧实现配额耗尽即静默）", _fired is True)


# 7) D3 回归：新回合不得装填冷却——handle_prompt 后立刻 peek 空跑必须照常触发。
#    reset_stretch 若把 last_reminder_at 拉到 now,fire() 的 cooldown_sec 会把
#    整个回合开头的空跑静默掉(e2e 场景 A 实测漏报)。
def probe_d3_turn_start_grinding():
    cfg = dict(lk.DEFAULTS)
    st = lk.new_state("golden-d3", 1000.0)
    lk.handle_prompt(cfg, {"prompt": "新任务"}, st, 1000.0)
    for i in range(5):
        lk.handle_post_use(cfg, {"tool_name": "Read", "tool_input": {"file_path": "a.txt"}},
                           st, 1002.0 + i)
    for i in range(3):
        out = lk.handle_post_use(
            cfg, {"tool_name": "Bash", "tool_input": {"command": "python _peek%d.py" % (i + 2)}},
            st, 1015.0 + i)
        if out and "重复执行" in str(out):
            return True
    return False


check("D3 新回合开头 60 秒内的 peek 空跑仍触发(回合冷却回归)",
      probe_d3_turn_start_grinding() is True)


# 9) 金标全键行为断言（v0.8）：spec 的每一个键都必须有一条「改它的值 → 真实执行路径
#    的可观测行为跟着变」的断言。仅校验「键落在 DEFAULTS 里、值一致」（§2）是不够的，
#    那正是漏掉 review_log_floor「4 处执行位只接了 1 处」的盲区。
#    断言纪律（WorkBuddy 2026-06-06 复核信 §3.2）：驱动真实入口函数
#    （handle_post_use / handle_post_fail / handle_prompt / handle_stop / run_loop），
#    不手工复刻被测逻辑；每条断言必须在一个方向上产生可观测差异，
#    回退验证时把该键的执行位改坏必须让本断言变红。
import tempfile as _tempfile
import shutil as _sh
import json as _json

# 断言表：key -> (probe，探针调用两次返回 (a, b)，断言 a != b)
# 每个 probe 只驱动真实入口，不复制任何被测判定逻辑。


def _cfg(**over):
    c = dict(lk.DEFAULTS)
    c.update({"clock_tick": False, "drift_check": False, "llm_review": False,
              "adaptive": False, "edit_foldback": False, "deliver_review": False,
              "cooldown_sec": 0, "fail_cooldown_sec": 0})
    c.update(over)
    return c


def _drive(cfg, events, sid, step=60.0):
    """events: [(tool, cmd|None, fail)] -> (state, outputs, last_now)"""
    st = lk.new_state(sid, 1000.0)
    now = 1000.0
    outs = []
    for tool, cmd, fail in events:
        now += step
        data = {"tool_name": tool,
                "tool_input": ({"command": cmd} if cmd is not None else {"file_path": "f.txt"})}
        if fail:
            o = lk.handle_post_fail(cfg, data, st, now)
        else:
            o = lk.handle_post_use(cfg, data, st, now)
        outs.append(str(o) if o else "")
    return st, outs, now


def _count(outs, marker):
    return sum(1 for o in outs if marker in o)


# ── 单键探针（每个返回一个可比较的观测量）────────────────────────────────
def _p_call_nudge(iv):
    """阈值 iv 下，恰好 iv 次调用触发、iv-1 次不触发。
    返回 (iv-1 次是否静默, iv 次是否触发) —— 双向锚定，写死常量必露馅。"""
    _, outs_lo, _ = _drive(_cfg(call_nudge_interval=iv),
                           [("Read", None, False)] * (iv - 1), "bnl-%d" % iv)
    _, outs_hi, _ = _drive(_cfg(call_nudge_interval=iv),
                           [("Read", None, False)] * iv, "bnh-%d" % iv)
    return (_count(outs_lo, "没有产生任何文件修改") == 0,
            _count(outs_hi, "没有产生任何文件修改") > 0)


def _p_repeat_min_calls(mc):
    _, outs, _ = _drive(_cfg(repeat_min_calls=mc, repeat_cmd_count=3, repeat_window=5),
                        [("Bash", "echo same", False)] * 6, "brm-%d" % mc)
    return _count(outs, "原样执行")


def _p_repeat_cmd_count(cc):
    _, outs, _ = _drive(_cfg(repeat_min_calls=0, repeat_cmd_count=cc, repeat_window=6),
                        [("Bash", "echo same", False)] * 6, "brc-%d" % cc)
    return _count(outs, "原样执行")


def _p_repeat_window(win):
    """窗口 win 只覆盖最近 win 条命令。
    序列 x c c c：win=2 只见 c c（2<3，不判重复）；win=4 见 x c c c（3≥3，判重复）。
    因此 win=2 必须静默 —— 窗口被写大（含更多历史）会翻成触发，即露馅。"""
    _, outs, _ = _drive(_cfg(repeat_min_calls=0, repeat_cmd_count=3, repeat_window=win),
                        [("Bash", "x", False), ("Bash", "c", False),
                         ("Bash", "c", False), ("Bash", "c", False)], "brw-%d" % win)
    return _count(outs, "原样执行")


def _p_max_recent_cmds(mx):
    _, outs, _ = _drive(_cfg(repeat_min_calls=0, repeat_cmd_count=3, repeat_window=5,
                             max_recent_cmds=mx),
                        [("Bash", "a", False), ("Bash", "b", False), ("Bash", "c", False),
                         ("Bash", "c", False), ("Bash", "c", False)], "bmr-%d" % mx)
    return _count(outs, "原样执行")


def _p_fail_streak_threshold(th):
    """返回首次触发失败循环时的累计失败数（应 == 阈值）。"""
    cfg = _cfg(fail_streak_threshold=th, max_reminders=99)
    st = lk.new_state("bfs-%d" % th, 1000.0)
    now = 1000.0
    first = None
    for _ in range(6):
        now += 1
        o = lk.handle_post_fail(cfg, {"tool_name": "Bash", "tool_input": {"command": "boom"}}, st, now)
        if o and "失败循环" in str(o) and first is None:
            first = st["total_failures"]
            st["fail_streak"] = 0
    return first


def _p_long_run_minutes(mins):
    _, outs, _ = _drive(_cfg(long_run_minutes=mins, long_run_min_calls=15),
                        [("Read", None, False)] * 20, "blrm-%d" % mins)
    return _count(outs, "还没有任何文件被修改")


def _p_long_run_min_calls(mc):
    _, outs, _ = _drive(_cfg(long_run_minutes=0, long_run_min_calls=mc),
                        [("Read", None, False)] * 20, "blrc-%d" % mc)
    return _count(outs, "还没有任何文件被修改")


def _p_cooldown_sec(cd):
    cfg = _cfg(cooldown_sec=cd, repeat_min_calls=0, repeat_cmd_count=2, repeat_window=3)
    st = lk.new_state("bcd-%d" % cd, 1000.0)
    now = 1000.0
    fires = 0
    for _ in range(2):
        for _i in range(3):
            now += 1
            o = lk.handle_post_use(cfg, {"tool_name": "Bash", "tool_input": {"command": "echo x"}}, st, now)
            if o and ("原样执行" in str(o) or "重复执行" in str(o)):
                fires += 1
                break
        now += 200
    return fires


def _p_fail_cooldown_sec(fc):
    cfg = _cfg(fail_cooldown_sec=fc, fail_streak_threshold=1, max_reminders=99)
    st = lk.new_state("bfc-%d" % fc, 1000.0)
    now = 1000.0
    fires = 0
    for _ in range(2):
        now += 1
        o = lk.handle_post_fail(cfg, {"tool_name": "Bash", "tool_input": {"command": "b"}}, st, now)
        if o and "失败循环" in str(o):
            fires += 1
        now += 100
    return fires


def _p_max_reminders(mx):
    cfg = _cfg(max_reminders=mx, fail_streak_threshold=1, repeat_min_calls=0,
               repeat_cmd_count=2, repeat_window=3)
    st = lk.new_state("bmx-%d" % mx, 1000.0)
    now = 1000.0
    fires = 0
    for _ in range(8):
        now += 1
        o = lk.handle_post_fail(cfg, {"tool_name": "Bash", "tool_input": {"command": "b"}}, st, now)
        if o and "失败循环" in str(o):
            fires += 1
    return fires


def _p_stop_min_calls(mc):
    cfg = _cfg(stop_min_calls=mc, long_run_minutes=1)
    st = lk.new_state("bsm-%d" % mc, 1000.0)
    st["tool_calls"] = 10
    st["edits"] = 0
    o = lk.handle_stop(cfg, {}, st, 1000.0 + 400)
    return 1 if (o and "结束前审查" in _json.dumps(o, ensure_ascii=False)) else 0


def _p_clock_tick_minutes(mins, maxmin, backoff=True):
    cfg = _cfg(clock_tick=True, clock_tick_backoff=backoff, clock_tick_minutes=mins,
               clock_tick_max_minutes=maxmin)
    st = lk.new_state("bck-%d-%d" % (mins, maxmin), 1000.0)
    now = 1000.0
    fires = 0
    for _ in range(100):
        now += 60
        o = lk.handle_post_use(cfg, {"tool_name": "Read", "tool_input": {"file_path": "f"}}, st, now)
        if o and "本地时钟" in str(o):
            fires += 1
    return fires


def _p_foldback_window(win):
    cfg = _cfg(edit_foldback=True, foldback_window=win)
    d = _tempfile.mkdtemp(prefix="bfb-")
    p = os.path.join(d, "f.txt")
    st = lk.new_state("bfb-%d" % win, 1000.0)
    now = 1000.0
    o = None
    for content in ("A", "B", "A"):
        open(p, "w").write(content)
        now += 1
        o = lk.handle_post_use(cfg, {"tool_name": "Edit", "tool_input": {"file_path": p}}, st, now)
    _sh.rmtree(d, ignore_errors=True)
    return 1 if o and "折返" in str(o) else 0


def _p_review_log_size(size, floor):
    """60 次调用后窗口长度 = max(floor, size)。floor=1 时以 size 为准。"""
    cfg = _cfg(review_log_size=size, review_log_floor=floor)
    st = lk.new_state("blc-%d-%d" % (size, floor), 1000.0)
    now = 1000.0
    for i in range(60):
        now += 1
        lk.handle_post_use(cfg, {"tool_name": "Bash", "tool_input": {"command": "c%d" % i}}, st, now)
    return len(st["recent_log"])


def _p_review_log_floor(size, floor):
    return _p_review_log_size(size, floor)


def _p_settle_after_calls(sa):
    cfg = _cfg(adaptive=True, settle_after_calls=sa, adaptive_warmup=1,
               repeat_min_calls=0, repeat_cmd_count=2, repeat_window=3)
    lk.save_adaptive({})
    st = lk.new_state("bsa-%d" % sa, 1000.0)
    now = 1000.0
    for _ in range(4):
        now += 1
        lk.handle_post_use(cfg, {"tool_name": "Bash", "tool_input": {"command": "echo hi"}}, st, now)
    for i in range(sa + 2):
        now += 1
        lk.handle_post_use(cfg, {"tool_name": "Read", "tool_input": {"file_path": "x%d" % i}}, st, now)
    return sum(v.get("fired", 0) for v in lk.load_adaptive().values())


def _p_adaptive(adaptive=True, warmup=5, low=0.3, high=0.7, bm=2, bc=4, tm=0.75, tf=60,
                fired=10, eff=0, cds=100, gap=150):
    lk.save_adaptive({"repeat-cmds": {"fired": fired, "effective": eff},
                      "exact-repeat": {"fired": fired, "effective": eff}})
    cfg = _cfg(adaptive=adaptive, adaptive_warmup=warmup, adaptive_low_rate=low,
               adaptive_high_rate=high, adaptive_backoff_mult=bm, adaptive_backoff_cap=bc,
               adaptive_tighten_mult=tm, adaptive_tighten_floor_sec=tf,
               cooldown_sec=cds, repeat_min_calls=0, repeat_cmd_count=2, repeat_window=3,
               max_reminders=12, settle_after_calls=10 ** 9)
    st = lk.new_state("bad-%d" % (abs(hash((adaptive, warmup, low, high, bm, bc, tm, tf,
                                            fired, eff, cds, gap))) % 1000000), 1000.0)
    now = 1000.0
    fires = 0
    for _ in range(3):
        for _i in range(3):
            now += 1
            o = lk.handle_post_use(cfg, {"tool_name": "Bash", "tool_input": {"command": "echo same"}}, st, now)
            if o and ("原样执行" in str(o) or "重复执行" in str(o)):
                fires += 1
                break
        now += gap
    return fires


def _p_kind_cooldown(bc=4, tm=0.75, eff=0):
    """backoff_cap / tighten_mult 的唯一执行位就是 kind_cooldown 本身（无其它消费点）。"""
    lk.save_adaptive({"x": {"fired": 10, "effective": eff}})
    cfg = _cfg(adaptive=True, adaptive_warmup=5, adaptive_low_rate=0.3,
               adaptive_high_rate=0.7, adaptive_backoff_mult=2, adaptive_backoff_cap=bc,
               adaptive_tighten_mult=tm, adaptive_tighten_floor_sec=60)
    return lk.kind_cooldown(cfg, "x", 100)


def _p_drift_count(dc, cd):
    cfg = _cfg(drift_check=True, drift_check_calls=dc, drift_cooldown_sec=cd, llm_api_key="d")
    saved = lk.call_llm_review
    lk.call_llm_review = lambda *a, **k: None      # 打桩：只数巡检次数，不发网络
    try:
        st = lk.new_state("bdr-%d-%d" % (dc, cd), 1000.0)
        now = 1000.0
        for i in range(40):
            now += 1
            lk.handle_post_use(cfg, {"tool_name": "Read", "tool_input": {"file_path": "z%d" % i}}, st, now)
        return st.get("drift_checks", 0)
    finally:
        lk.call_llm_review = saved


def _p_escalate(esc):
    cfg = _cfg(escalate_after_reminders=esc, deliver_review=True, llm_api_key="d",
               fail_streak_threshold=1)
    rd = lk.review_channel_dir()
    _sh.rmtree(rd, ignore_errors=True)
    st = lk.new_state("bes-%d" % esc, 1000.0)
    st["reminders"] = 2
    lk.handle_post_fail(cfg, {"tool_name": "Bash", "tool_input": {"command": "b"}}, st, 1111.0)
    try:
        n = len([f for f in os.listdir(rd) if f.startswith("req_")])
    except OSError:
        n = 0
    _sh.rmtree(rd, ignore_errors=True)
    return n


def _p_request_poll_sec(pv):
    import time as _t
    import importlib.util as _iu
    wspec = _iu.spec_from_file_location("lkw", os.path.join(ROOT, "hooks", "lookup_watcher.py"))
    W = _iu.module_from_spec(wspec)
    wspec.loader.exec_module(W)
    cap = {}

    def fake_sleep(s):
        cap["poll"] = s
        raise KeyboardInterrupt

    orig = (W.load_spec, _t.sleep, W.load_cfg)
    try:
        W.load_cfg = lambda: {"enabled": True, "llm_api_key": ""}
        _t.sleep = fake_sleep
        W.load_spec = lambda: {"request_poll_sec": pv}
        try:
            W.run_loop(once=False)
        except KeyboardInterrupt:
            pass
        return cap.get("poll")
    finally:
        W.load_spec, _t.sleep, W.load_cfg = orig


# 每键一条行为断言，形式为 (探针, 锚定期望, 说明)：
#   探针()      -> 观测值
#   期望(观测值) -> 该观测值是否符合"键按文档语义生效"的**正向**预期
# 用锚定期望而非简单 `lo != hi`：后者在"键被打断成固定常量 12"时仍可能因两档
# 都改变了而假绿（settle_after_calls 回退验证实测到过），锚定则要求语义方向正确。
def _nonzero(v):
    return v is not None and v > 0


def _zero(v):
    return v == 0


def _eq(n):
    return lambda v: v == n


BEHAVIOR_KEYS = {
    # 键: (探针, 期望, 说明)
    "call_nudge_interval": (lambda: _p_call_nudge(3),
                            _eq((True, True)), "阈值 3：前 2 次静默、第 3 次触发"),
    "repeat_min_calls": (lambda: _p_repeat_min_calls(0),
                         _nonzero, "静默下限为 0 时重复命令立即被判重复"),
    "repeat_cmd_count": (lambda: _p_repeat_cmd_count(2),
                         _nonzero, "阈值 2 时 6 次同命令必判重复"),
    "repeat_window": (lambda: _p_repeat_window(2),
                      _eq(0), "窗口 2 只见 c c（<3）→ 不判重复"),
    "max_recent_cmds": (lambda: _p_max_recent_cmds(8),
                        _nonzero, "缓冲 8 保留 a,b,c,c,c → 判重复"),
    "fail_streak_threshold": (lambda: _p_fail_streak_threshold(3),
                              _eq(3), "首次触发失败循环恰在累计失败=阈值(3)"),
    "long_run_minutes": (lambda: _p_long_run_minutes(0),
                         _nonzero, "时间下限 0 → 立即判长跑零修改"),
    "long_run_min_calls": (lambda: _p_long_run_min_calls(0),
                           _nonzero, "调用下限 0 → 立即判长跑零修改"),
    "cooldown_sec": (lambda: _p_cooldown_sec(0),
                     _eq(2), "冷却 0 → 两轮重复都触发"),
    "fail_cooldown_sec": (lambda: _p_fail_cooldown_sec(0),
                          _eq(2), "失败冷却 0 → 两轮失败都触发"),
    "max_reminders": (lambda: _p_max_reminders(2),
                      _eq(2), "配额 2 → 至多 2 次提醒"),
    "stop_min_calls": (lambda: _p_stop_min_calls(5),
                       _eq(1), "下限 5(calls=10) → 触发结束前审查"),
    "clock_tick_minutes": (lambda: _p_clock_tick_minutes(60, 60),
                           _nonzero, "间隔 60 分钟 → 长跑中触发时钟锚点"),
    "clock_tick_max_minutes": (lambda: _p_clock_tick_minutes(1, 6, True),
                               _eq(18), "基础 1 分钟/上限 6 → 100 分钟内退避到 6 后恒 6"),
    "foldback_window": (lambda: _p_foldback_window(5),
                        _eq(1), "窗口 5 → A→B→A 折返被识别"),
    "review_log_size": (lambda: _p_review_log_size(5, 1),
                        _eq(5), "floor=1 时窗口以 size(5) 为准"),
    "review_log_floor": (lambda: _p_review_log_floor(12, 40),
                         _eq(40), "地板 40 压过 size(12) → 保留 40 条"),
    "settle_after_calls": (lambda: _p_settle_after_calls(1),
                           _nonzero, "结算步数 1 → 提醒被立即结算计入统计"),
    "adaptive_warmup": (lambda: _p_adaptive(warmup=5),
                        _eq(2), "预热 5(样本10) → backoff 生效(续火次数减少)"),
    "adaptive_low_rate": (lambda: _p_adaptive(low=0.3, eff=2),
                          _eq(2), "低门槛 0.3 > 有效率 0.2 → 触发降噪"),
    "adaptive_high_rate": (lambda: (_p_adaptive(high=0.7, eff=8, gap=85),
                                    _p_adaptive(high=0.9, eff=8, gap=85)),
                           _eq((3, 2)), "有效率 0.8：门槛 0.7 → 收紧(续火3)；门槛 0.9 → 不收窄(续火2)"),
    "adaptive_backoff_mult": (lambda: _p_adaptive(bm=2),
                              _eq(2), "倍率 2 → 冷却 200 压住间隔 150"),
    "adaptive_backoff_cap": (lambda: _p_kind_cooldown(bc=4),
                             _eq(200), "倍率2×基础100=200，未触上限 400"),
    "adaptive_tighten_mult": (lambda: _p_kind_cooldown(tm=0.75, eff=10),
                              _eq(75.0), "收紧倍率 0.75×基础100=75"),
    "adaptive_tighten_floor_sec": (lambda: _p_adaptive(eff=10, tf=60),
                                   _eq(3), "地板 60 低于 100 → 收紧后仍能续火"),
    "drift_check_calls": (lambda: _p_drift_count(5, 0),
                          _eq(7), "间隔 5，40 次调用 → 巡检 7 次"),
    "drift_cooldown_sec": (lambda: _p_drift_count(5, 0),
                           _nonzero, "冷却 0 → 巡检多次发生"),
    "escalate_after_reminders": (lambda: _p_escalate(3),
                                 _eq(1), "门槛 3(reminders 到 3) → 投递 1 件请求"),
    "request_poll_sec": (lambda: _p_request_poll_sec(7),
                         _eq(7), "spec 轮询 7s → run_loop 睡 7s"),
}

# 这些键的行为断言由 test/channel_check.py 覆盖（第三通道端到端），
# 此处只登记"已被覆盖"，避免重复又漏记。
CHANNEL_COVERED = {"request_ttl_sec", "result_ttl_sec", "result_settle_calls"}

NUMERIC_KEYS = [k for k in SPEC if k not in ("interpreters", "script_extensions")]
covered = set(BEHAVIOR_KEYS) | CHANNEL_COVERED
missing = [k for k in NUMERIC_KEYS if k not in covered]
check("全键行为断言覆盖 spec 每个标量键", not missing,
      "未覆盖: %r" % missing)

for _k in NUMERIC_KEYS:
    if _k in BEHAVIOR_KEYS:
        _probe, _expect, _desc = BEHAVIOR_KEYS[_k]
        try:
            _got = _probe()
            _ok = bool(_expect(_got))
            check("behavior %s（%s）" % (_k, _desc), _ok,
                  "观测值 %r 不符合文档语义（键未真正接入执行路径或被写死）" % (_got,))
        except Exception as _e:  # noqa: BLE001
            check("behavior %s" % _k, False, "探针异常: %r" % _e)
    else:
        # 第三通道键：此处确认它们确实被 channel_check 覆盖（文件存在即视为登记）
        check("behavior %s（由 channel_check.py 端到端覆盖）" % _k,
              os.path.exists(os.path.join(HERE, "channel_check.py")))

# 列表键（interpreters / script_extensions）的行为断言在 §2 的 norm_cmd 金标里，
# 用同一份 spec 推导：删掉一个解释器/扩展名，norm_cmd 输出必变。
# 注意：归一化要求「解释器集合命中」且「脚本扩展名命中」两者同时成立，
# 故探测用解释器 × 已知扩展名的交叉组合（powershell 只在 spec 里、.ps1 也是）。
check("behavior interpreters（norm_cmd 行为随 spec 解释器集合变）",
      lk.norm_cmd("python a1.py") == "python a.py"
      and lk.norm_cmd("pwsh a1.ps1") == "pwsh a.ps1"
      and lk.norm_cmd("unknownbin a1.py") == "unknownbin a1.py")
check("behavior script_extensions（norm_cmd 行为随 spec 扩展名集合变）",
      lk.norm_cmd("python a1.ps1") == "python a.ps1"
      and lk.norm_cmd("python a1.mjs") == "python a.mjs"
      and lk.norm_cmd("python a1.noext") == "python a1.noext")


print()
print("golden_check.py: %s" % ("ALL PASSED" if fails == 0 else "%d FAILURES" % fails))
sys.exit(0 if fails == 0 else 1)
