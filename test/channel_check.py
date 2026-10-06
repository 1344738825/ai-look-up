"""第三通道端到端检查（v0.7）：投递 → watcher 领取 → 结论 → 取回（过三道闸）。

不依赖任何真实 LLM：把 watcher 的 call_llm 打桩成固定结论，跑真实文件协议。
Run: python test/channel_check.py
"""
import importlib.util
import json
import os
import shutil
import sys

# Windows 宿主默认 ANSI 代码页(cp1252/gbk 之外会炸),测试输出含中文,统一走 UTF-8。
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "hooks"))

# 投递区必须有确定性落点：用临时目录覆盖 LOOKUP_STATE_DIR，
# 保证 hook 与 watcher 指向同一个 review 目录（真实运行时共用 %TEMP%）。
SANDBOX = tempfile.mkdtemp(prefix="lookup-channel-")
os.environ["LOOKUP_STATE_DIR"] = SANDBOX

import lookup_hook as H          # noqa: E402
import lookup_watcher as W       # noqa: E402

FAILS = []


def check(name, fn):
    try:
        fn()
        print("PASS ", name)
    except AssertionError as e:
        FAILS.append(name)
        print("FAIL ", name, "-", e)


def fresh_state(**over):
    now = 1_791_221_000.0
    st = H.new_state("sessA", now)
    st.update(over)
    return st, now


BASE_CFG = dict(H.DEFAULTS)
BASE_CFG.update({
    "deliver_review": True,
    "llm_api_key": "stub-key",
    "escalate_after_reminders": 3,
    "result_ttl_sec": 900,
    "result_settle_calls": 60,
    "request_ttl_sec": 900,
})


def req_path(rid):
    return os.path.join(H.review_channel_dir(), "req_%s.json" % H._id_safe(rid))


def res_path(rid):
    return os.path.join(H.review_channel_dir(), "res_%s.json" % H._id_safe(rid))


def clean_channel():
    d = H.review_channel_dir()
    for n in os.listdir(d):
        try:
            os.remove(os.path.join(d, n))
        except Exception:
            pass


# ── 1) deliver_review 关 → 不投递 ────────────────────────────────────────────
def t_disabled_no_deliver():
    clean_channel()
    st, now = fresh_state(reminders=5)
    cfg = dict(BASE_CFG, deliver_review=False)
    rid = H.deliver_review_request(cfg, st, now, "long-run", "运行 26 分钟零修改")
    assert rid is None, "deliver_review=False 时必须不投递，实际 %r" % rid
    assert not os.listdir(H.review_channel_dir()), "投递区应为空"
check("deliver_review=False 不投递", t_disabled_no_deliver)


# ── 2) 缺 key → 不投递 ──────────────────────────────────────────────────────
def t_no_key_no_deliver():
    clean_channel()
    st, now = fresh_state(reminders=5)
    cfg = dict(BASE_CFG, llm_api_key="")
    rid = H.deliver_review_request(cfg, st, now, "long-run", "x")
    assert rid is None, "缺 llm_api_key 时必须不投递"
check("缺 llm_api_key 不投递", t_no_key_no_deliver)


# ── 3) 完整链路：投递 → watcher 处理 → 取回注入 ───────────────────────────────
def t_full_roundtrip():
    clean_channel()
    st, now = fresh_state(reminders=5, tool_calls=42, prompt_resets=1)
    rid = H.deliver_review_request(BASE_CFG, st, now, "long-run", "运行 26 分钟零修改")
    assert rid, "应投递成功"
    p = req_path(rid)
    assert os.path.exists(p), "请求单应落盘: %s" % p

    # 用打桩 LLM 跑 watcher 的单次处理（真实文件协议）
    orig_load, orig_llm = W.load_cfg, W.call_llm
    stub_cfg = {"llm_api_key": "stub-key", "llm_model": "stub",
                "llm_api_base": "http://stub", "llm_timeout_sec": 5,
                "request_ttl_sec": 900}
    W.load_cfg = lambda: stub_cfg
    W.call_llm = lambda cfg, system, material: (
        '{"verdict":"drifting","reason":"子任务扩张","suggestion":"收敛回主线"}', "")
    try:
        reqs = W.list_requests()
        assert len(reqs) == 1, "应有 1 份请求单待处理，实际 %d" % len(reqs)
        W.process_one(reqs[0], stub_cfg, {"request_ttl_sec": 900})
    finally:
        W.load_cfg, W.call_llm = orig_load, orig_llm

    rp = res_path(rid)
    assert os.path.exists(rp), "watcher 应写入结论: %s" % rp
    assert not os.path.exists(p), "watcher 处理后应删除请求单"

    # 取回：结论对应 42 步，state 现在 45 步（3 步前）→ 过闸
    st["tool_calls"] = 45
    texts = H.take_review_results(st, BASE_CFG, now + 5)
    assert len(texts) == 1, "应取回 1 条，实际 %d" % len(texts)
    assert "独立审查者" in texts[0] and "有偏航迹象" in texts[0], texts[0]
    assert "3 步前" in texts[0], "必须标注场景步数: " + texts[0]
    assert not os.path.exists(rp), "取回后结论文件应删除"
check("完整链路：投递→watcher→取回注入", t_full_roundtrip)


# ── 4) 闸1：prompt_resets 变化 → 作废不注入 ─────────────────────────────────
def t_gate_fingerprint():
    clean_channel()
    st, now = fresh_state(reminders=5, tool_calls=42, prompt_resets=1)
    rid = H.deliver_review_request(BASE_CFG, st, now, "long-run", "x")
    os.makedirs(H.review_channel_dir(), exist_ok=True)
    with open(res_path(rid), "w", encoding="utf-8") as f:
        import json
        json.dump({"session_id": "sessA", "prompt_resets": 2, "finished_at": now,
                   "tool_calls": 42, "verdict": "drifting", "reason": "r",
                   "suggestion": "s", "detail": "x"}, f)
    st["tool_calls"] = 45
    texts = H.take_review_results(st, BASE_CFG, now + 5)
    assert texts == [], "prompt_resets 变了必须作废，实际 %r" % texts
    assert not os.path.exists(res_path(rid)), "作废的结论文件应删除"
check("闸1 场景指纹：换指令后作废", t_gate_fingerprint)


# ── 5) 闸2：过期 → 作废 ─────────────────────────────────────────────────────
def t_gate_ttl():
    clean_channel()
    st, now = fresh_state(reminders=5, tool_calls=42, prompt_resets=1)
    rid = H.deliver_review_request(BASE_CFG, st, now, "long-run", "x")
    import json
    with open(res_path(rid), "w", encoding="utf-8") as f:
        json.dump({"session_id": "sessA", "prompt_resets": 1, "finished_at": now,
                   "tool_calls": 42, "verdict": "drifting", "reason": "r",
                   "suggestion": "s", "detail": "x"}, f)
    st["tool_calls"] = 45
    texts = H.take_review_results(st, BASE_CFG, now + 901)   # 超 result_ttl_sec
    assert texts == [], "过期结论必须作废，实际 %r" % texts
check("闸2 过期：超 TTL 作废", t_gate_ttl)


# ── 6) 闸3：步数走远 → 作废 ─────────────────────────────────────────────────
def t_gate_steps():
    clean_channel()
    st, now = fresh_state(reminders=5, tool_calls=42, prompt_resets=1)
    rid = H.deliver_review_request(BASE_CFG, st, now, "long-run", "x")
    import json
    with open(res_path(rid), "w", encoding="utf-8") as f:
        json.dump({"session_id": "sessA", "prompt_resets": 1, "finished_at": now,
                   "tool_calls": 42, "verdict": "drifting", "reason": "r",
                   "suggestion": "s", "detail": "x"}, f)
    st["tool_calls"] = 42 + 61                                # 超 result_settle_calls
    texts = H.take_review_results(st, BASE_CFG, now + 5)
    assert texts == [], "走远后结论必须作废，实际 %r" % texts
check("闸3 步数：走远后作废", t_gate_steps)


# ── 7) verdict=error → 静默丢弃，不注入 ─────────────────────────────────────
def t_error_dropped():
    clean_channel()
    st, now = fresh_state(reminders=5, tool_calls=42, prompt_resets=1)
    rid = H.deliver_review_request(BASE_CFG, st, now, "long-run", "x")
    import json
    with open(res_path(rid), "w", encoding="utf-8") as f:
        json.dump({"session_id": "sessA", "prompt_resets": 1, "finished_at": now,
                   "tool_calls": 42, "verdict": "error"}, f)
    st["tool_calls"] = 45
    texts = H.take_review_results(st, BASE_CFG, now + 5)
    assert texts == [], "error 结论不得注入，实际 %r" % texts
    assert not os.path.exists(res_path(rid)), "error 结论文件应删除"
check("verdict=error 静默丢弃", t_error_dropped)


# ── 8) 幂等：同 request_id 重复投递只留一份 ─────────────────────────────────
def t_idempotent():
    clean_channel()
    st, now = fresh_state(reminders=5, tool_calls=42)
    r1 = H.deliver_review_request(BASE_CFG, st, now, "long-run", "x")
    r2 = H.deliver_review_request(BASE_CFG, st, now, "long-run", "x")
    assert r1 == r2, "同场景 request_id 必须相同：%r vs %r" % (r1, r2)
    reqs = [n for n in os.listdir(H.review_channel_dir()) if n.startswith("req_")]
    assert len(reqs) == 1, "幂等：应只有 1 份请求单，实际 %r" % reqs
check("同场景重复投递幂等", t_idempotent)


# ── 9) 会话隔离：别的会话的结论不取回、不删除 ───────────────────────────────
def t_session_isolation():
    clean_channel()
    import json
    other = os.path.join(H.review_channel_dir(), "res_other.json")
    with open(other, "w", encoding="utf-8") as f:
        json.dump({"session_id": "sessOTHER", "prompt_resets": 1, "finished_at": 1,
                   "tool_calls": 1, "verdict": "drifting", "reason": "r",
                   "suggestion": "s", "detail": "x"}, f)
    st, now = fresh_state(reminders=5, tool_calls=42)
    texts = H.take_review_results(st, BASE_CFG, now)
    assert texts == [], "别的会话的结论不该被注入"
    assert os.path.exists(other), "别的会话的结论文件不该被删"
check("会话隔离：不碰别人的结论", t_session_isolation)


# ── 10) watcher 单例锁：第二个实例不得抢主 ──────────────────────────────────
def t_singleton_lock():
    ok1, lock = W.acquire_lock()
    assert ok1 is True, "第一个实例应拿到锁"
    try:
        ok2, _ = W.acquire_lock()
        assert ok2 is False, "同进程第二个实例应拿不到锁（PID 活着）"
    finally:
        W.release_lock(lock)
    ok3, lock3 = W.acquire_lock()
    assert ok3 is True, "释放后应能重新拿到锁"
    W.release_lock(lock3)
check("watcher 单例锁互斥", t_singleton_lock)


# ── 11) watcher 过期清扫：超 TTL 的请求单写成 error 结论并删除 ───────────────
def t_sweep_expired():
    clean_channel()
    st, now = fresh_state(reminders=5, tool_calls=42)
    rid = H.deliver_review_request(BASE_CFG, st, now, "long-run", "x")
    # 手动把 created_at 改老，模拟滞留（真实 created_at 是 time.time() 量级）
    import json
    import time as _time
    p = req_path(rid)
    with open(p, encoding="utf-8") as f:
        req = json.load(f)
    req["created_at"] = _time.time() - 10_000
    with open(p, "w", encoding="utf-8") as f:
        json.dump(req, f)
    n = W.sweep_expired({"request_ttl_sec": 900, "llm_api_key": "stub-key"})
    assert n == 1, "应清扫 1 份过期请求单，实际 %d" % n
    rp = res_path(rid)
    assert os.path.exists(rp), "过期请求单应写成 error 结论"
    with open(rp, encoding="utf-8") as f:
        res = json.load(f)
    assert res.get("verdict") == "error", "过期结论 verdict 应为 error"
    assert not os.path.exists(p), "过期请求单应删除"
check("watcher 清扫滞留请求单", t_sweep_expired)


# ── 12) 未投递时取回为空（不误伤状态）──────────────────────────────────────
def t_empty_takeback():
    clean_channel()
    st, now = fresh_state(reminders=5, tool_calls=42)
    assert H.take_review_results(st, BASE_CFG, now) == []
check("空投递区取回为空", t_empty_takeback)


# ── 13) 真实投递路径:无本地触发的普通事件也必须注入捎带结论 ─────────────────
# (教训:不能只直呼 take_review_results 断言返回值——那绕开了真实投递路径,
#  emit/carried 链路上的问题它一个都看不见,须走真实 handle_post_use。)
def _stub_result(rid):
    W.load_cfg_orig = W.load_cfg
    W.call_llm_orig = W.call_llm
    stub_cfg = {"llm_api_key": "stub-key", "llm_model": "stub",
                "llm_api_base": "http://stub", "llm_timeout_sec": 5,
                "request_ttl_sec": 900}
    W.load_cfg = lambda: stub_cfg
    W.call_llm = lambda cfg, system, material: (
        '{"verdict":"drifting","reason":"子任务扩张","suggestion":"收敛回主线"}', "")
    try:
        for req in W.list_requests():
            W.process_one(req, stub_cfg, {"request_ttl_sec": 900})
    finally:
        W.load_cfg, W.call_llm = W.load_cfg_orig, W.call_llm_orig


def _out_text(out):
    obj = out if isinstance(out, dict) else json.loads(out)
    return obj["hookSpecificOutput"]["additionalContext"]


def t_realpath_no_trigger():
    clean_channel()
    st, now = fresh_state(reminders=5, tool_calls=42, prompt_resets=1,
                          calls_since_reminder=1)
    st["last_clock_tick_at"] = now
    rid = H.deliver_review_request(BASE_CFG, st, now, "long-run", "运行 26 分钟零修改")
    assert rid
    _stub_result(rid)
    data = {"tool_name": "Bash", "tool_input": {"command": "echo unique-nontrigger-xyz"}}
    out = H.handle_post_use(BASE_CFG, data, st, now + 5)
    assert out is not None, "普通事件也必须把捎带结论注入出去(结论文件已被取回删除)"
    t = _out_text(out)
    assert "独立审查者" in t and "有偏航迹象" in t, t
check("真实路径:无触发事件注入捎带结论", t_realpath_no_trigger)


def t_realpath_with_trigger():
    clean_channel()
    st, now = fresh_state(reminders=5, tool_calls=45, prompt_resets=1,
                          calls_since_reminder=30, edits_since_reminder=0)
    st["last_reminder_at"] = now - 400
    st["last_clock_tick_at"] = now
    rid = H.deliver_review_request(BASE_CFG, st, now, "long-run", "运行 26 分钟零修改")
    assert rid
    _stub_result(rid)
    data = {"tool_name": "Bash", "tool_input": {"command": "echo trigger-carry-xyz"}}
    out = H.handle_post_use(BASE_CFG, data, st, now + 5)
    assert out is not None
    t = _out_text(out)
    assert "独立审查者" in t, "捎带结论应在: " + t
    assert "次工具调用没有产生任何文件修改" in t, "本地提醒应在: " + t
    assert t.count("独立审查者") == 1, "结论只能出现一次(回归验证 emit 递归缺陷): " + t
check("真实路径:结论+提醒同事件各自恰好一次(emit 递归回归)", t_realpath_with_trigger)


shutil.rmtree(SANDBOX, ignore_errors=True)
print()
if FAILS:
    print("channel_check.py: %d FAILURES" % len(FAILS))
    sys.exit(1)
print("channel_check.py: ALL PASSED")
