"""反向验证（mutation / revert-verify）：把每条修复改回缺陷形态，确认对应测试变红。

方法论：一条断言如果"把 bug 放回去它也不红"，就是装饰性的。
本脚本对 v0.7 新增的每条防线逐一回退代码，跑对应测试，要求出现 FAIL。

Run: python test/revert_verify.py
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PY = sys.executable
NODE = os.environ.get("NODE_BIN") or shutil.which("node")

HOOK = os.path.join(ROOT, "hooks", "lookup_hook.py")
WATCHER = os.path.join(ROOT, "hooks", "lookup_watcher.py")
INDEX = os.path.join(ROOT, "index.js")

results = []


def read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def write(p, s):
    with open(p, "w", encoding="utf-8") as f:
        f.write(s)


def run(cmd, env=None):
    e = dict(os.environ)
    if env:
        e.update(env)
    p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, env=e)
    return p.returncode, p.stdout + p.stderr


def verify(label, file, old, new, test_cmd, expect_pass_cmd=None):
    """把 file 里的 old 换成 new（注入缺陷），跑 test_cmd，要求返回非 0（红）。"""
    orig = read(file)
    if old not in orig:
        results.append((label, False, "找不到待回退片段"))
        print("FAIL  找不到待回退片段（脚本与实现已漂移，需同步）: %s" % label)
        return
    write(file, orig.replace(old, new, 1))
    try:
        rc, out = run(test_cmd)
    finally:
        write(file, orig)                       # 务必还原
    if rc != 0:
        results.append((label, True, ""))
        print("PASS  回退后测试变红: %s" % label)
    else:
        results.append((label, False, out[-600:]))
        print("FAIL  回退后测试仍绿（防线是装饰性的）: %s" % label)
        print("---- 输出尾部 ----\n%s\n----------------" % out[-600:])


# ═══════════════════════════════════════════════════════════════════════════
# 防线 1：F1 —— fail-loop 补齐 3 项状态维护（Python）
#   回退：删掉 calls/edits 清零与 pending_reminders 登记 → golden F1 断言应红
# ═══════════════════════════════════════════════════════════════════════════
verify(
    "F1/Python: fail-loop 漏 pending_reminders 登记",
    HOOK,
    '    state["calls_since_reminder"] = 0\n'
    '    state["edits_since_reminder"] = 0\n'
    '    _fcur = state.get("recent_cmds", [])\n'
    '    state.setdefault("pending_reminders", []).append({',
    '    _fcur = state.get("recent_cmds", [])\n'
    '    state.setdefault("pending_reminders", []).append({',
    [PY, os.path.join(HERE, "golden_check.py")],
)

verify(
    "F1/Python: fail-loop 不清零 calls_since_reminder（no-output 被提前误触发）",
    HOOK,
    '    state["calls_since_reminder"] = 0\n'
    '    state["edits_since_reminder"] = 0\n'
    '    _fcur = state.get("recent_cmds", [])',
    '    _fcur = state.get("recent_cmds", [])',
    [PY, os.path.join(HERE, "golden_check.py")],
)

# ═══════════════════════════════════════════════════════════════════════════
# 防线 2：P2-3 —— evaluate_pending 单次 load/save（Python）
#   回退：改回循环内 load+save（模拟重复覆盖）→ 需一条能感知覆盖的断言。
#   这里用并发覆盖探针。
# ═══════════════════════════════════════════════════════════════════════════
verify(
    "P2-3/Python: evaluate_pending 循环内 save_adaptive（多余读改写窗口）",
    HOOK,
    "    if to_settle:\n"
    "        # 一次读、一次写:避免循环内重复 load/save(并发下会互相覆盖丢计数)。\n"
    "        stats = load_adaptive()\n"
    "        for p in to_settle:\n"
    "            effective = _is_effective(state, p)\n"
    "            s = stats.setdefault(p[\"kind\"], {\"fired\": 0, \"effective\": 0})\n"
    "            s[\"fired\"] = s.get(\"fired\", 0) + 1\n"
    "            if effective:\n"
    "                s[\"effective\"] = s.get(\"effective\", 0) + 1\n"
    "        save_adaptive(stats)",
    "    if to_settle:\n"
    "        for p in to_settle:\n"
    "            effective = _is_effective(state, p)\n"
    "            stats = load_adaptive()\n"
    "            s = stats.setdefault(p[\"kind\"], {\"fired\": 0, \"effective\": 0})\n"
    "            s[\"fired\"] = s.get(\"fired\", 0) + 1\n"
    "            if effective:\n"
    "                s[\"effective\"] = s.get(\"effective\", 0) + 1\n"
    "            save_adaptive(stats)",
    [PY, os.path.join(HERE, "golden_check.py")],
)

# ═══════════════════════════════════════════════════════════════════════════
# 防线 3：第三通道三闸 —— 任删一闸，对应闸测试应红
# ═══════════════════════════════════════════════════════════════════════════
verify(
    "通道闸1（场景指纹）删除",
    HOOK,
    '        if res.get("prompt_resets", 0) != state.get("prompt_resets", 0):\n'
    '            _safe_rm(p)\n'
    '            continue',
    '        pass  # gate1 removed',
    [PY, os.path.join(HERE, "channel_check.py")],
)

verify(
    "通道闸2（过期）删除",
    HOOK,
    '        if now - float(res.get("finished_at", 0)) > cfg.get("result_ttl_sec", 900):\n'
    '            _safe_rm(p)\n'
    '            continue',
    '        pass  # gate2 removed',
    [PY, os.path.join(HERE, "channel_check.py")],
)

verify(
    "通道闸3（步数）删除",
    HOOK,
    '        if steps_back > cfg.get("result_settle_calls", 60):\n'
    '            _safe_rm(p)\n'
    '            continue',
    '        pass  # gate3 removed',
    [PY, os.path.join(HERE, "channel_check.py")],
)

verify(
    "会话隔离删除（会碰别人的结论）",
    HOOK,
    '        if res.get("session_id") and str(res["session_id"]) != str(sid):\n'
    '            continue',
    '        pass  # isolation removed',
    [PY, os.path.join(HERE, "channel_check.py")],
)

verify(
    "投递前置：deliver_review 开关被忽略",
    HOOK,
    '    if not cfg.get("deliver_review"):\n        return None',
    '    if False:\n        return None',
    [PY, os.path.join(HERE, "channel_check.py")],
)

# ═══════════════════════════════════════════════════════════════════════════
# 防线 4：watcher 单例锁 release（Windows 句柄未关就删 → 静默失效）
# ═══════════════════════════════════════════════════════════════════════════
verify(
    "watcher release_lock: 句柄未关就 os.remove（Windows 静默失效）",
    WATCHER,
    "    holder = 0\n"
    "    try:\n"
    "        with open(lock, encoding=\"utf-8\") as f:\n"
    "            holder = int((f.read() or \"0\").strip() or \"0\")\n"
    "    except Exception:\n"
    "        return\n"
    "    if holder != os.getpid():\n"
    "        return\n"
    "    try:\n"
    "        os.remove(lock)\n"
    "    except OSError:\n"
    "        pass",
    "    try:\n"
    "        with open(lock, encoding=\"utf-8\") as f:\n"
    "            if int((f.read() or \"0\").strip() or \"0\") == os.getpid():\n"
    "                os.remove(lock)\n"
    "    except Exception:\n"
    "        pass",
    [PY, os.path.join(HERE, "channel_check.py")],
)

# ═══════════════════════════════════════════════════════════════════════════
# 防线 5：JS F1 —— index.js fail-loop 补齐状态维护
#   回退：删掉 callsSinceReminder/editsSinceReminder 清零 与 pending 登记，
#   JS 侧用 golden.mjs 的 fail-loop 回放 + 新增断言感知。
# ═══════════════════════════════════════════════════════════════════════════
verify(
    "F1/JS: fail-loop 不清零 callsSinceReminder（no-output 被提前误触发）",
    INDEX,
    "          st.callsSinceReminder = 0;\n"
    "          st.editsSinceReminder = 0;\n"
    "          {\n"
    "            const cur = st.recentCmds;",
    "          {\n"
    "            const cur = st.recentCmds;",
    [NODE, os.path.join(HERE, "dsh-smoke.mjs")],
)

verify(
    "F1/JS: fail-loop 漏 pendingReminders 登记（自适应统计被架空）",
    INDEX,
    "            st.pendingReminders.push({\n"
    "              kind: 'fail-loop',",
    "            if (false) st.pendingReminders.push({\n"
    "              kind: 'fail-loop',",
    [NODE, os.path.join(HERE, "dsh-smoke.mjs")],
)

# ═══════════════════════════════════════════════════════════════════════════
# 防线 6：JS 第三通道投递开关（deliverReview 关 → 不投）
# ═══════════════════════════════════════════════════════════════════════════
verify(
    "JS 通道：投递不看 deliverReview 开关",
    INDEX,
    "  if (!cfg.deliverReview) return null;",
    "  if (false) return null;",
    [NODE, os.path.join(HERE, "dsh-smoke.mjs")],
)

verify(
    "JS 通道：投递不看 llmApiKey",
    INDEX,
    "  if (!String(cfg.llmApiKey ?? '').trim()) return null;",
    "  if (false) return null;",
    [NODE, os.path.join(HERE, "dsh-smoke.mjs")],
)

# ═══════════════════════════════════════════════════════════════════════════
# 防线 7：P2-1 —— 跨进程状态锁
# ═══════════════════════════════════════════════════════════════════════════
verify(
    "P2-1/Python: 未持锁执行 load→save（并发覆盖丢状态）",
    HOOK,
    "    with state_lock(sid):",
    "    if True:  # lock disabled",
    [PY, os.path.join(HERE, "lock_check.py")],
)

# ═══════════════════════════════════════════════════════════════════════════
# 防线 8：P2-2 —— 新回合重置 reminders 配额
# ═══════════════════════════════════════════════════════════════════════════
verify(
    "P2-2/Python: reset_stretch 不重置 reminders",
    HOOK,
    '    state["clock_interval"] = 0\n'
    '    state["reminders"] = 0',
    '    state["clock_interval"] = 0\n'
    '    state["_reminders_noreset"] = 0',
    [PY, os.path.join(HERE, "golden_check.py")],
)

verify(
    "P2-2/JS: resetStretch 不重置 reminders",
    INDEX,
    "  // P2-2：reminders 必须在这里清零，否则同一会话累计 maxReminders 次后\n"
    "  // 配额永久耗尽，用户开新任务也再无提醒（新任务静默失守）。\n"
    "  state.reminders = 0;",
    "  // P2-2 disabled for revert-verify",
    [NODE, os.path.join(HERE, "dsh-smoke.mjs")],
)

# ═══════════════════════════════════════════════════════════════════════════
print()
ok = sum(1 for _, good, _ in results if good)
print("revert_verify.py: %d/%d 防线在回退后确实变红" % (ok, len(results)))
if ok != len(results):
    print("存在装饰性防线，需修正测试或实现。")
    sys.exit(1)
print("revert_verify.py: ALL DEFENSES PROVEN")
