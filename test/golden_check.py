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

print()
print("golden_check.py: %s" % ("ALL PASSED" if fails == 0 else "%d FAILURES" % fails))
sys.exit(0 if fails == 0 else 1)
