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

print()
print("golden_check.py: %s" % ("ALL PASSED" if fails == 0 else "%d FAILURES" % fails))
sys.exit(0 if fails == 0 else 1)
