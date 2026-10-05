"""P2-1 跨进程并发探针：并发钩子事件不得互相覆盖状态。

回退验证：把 lookup_hook.main 里的 `with state_lock(sid):` 改成透传（不持锁），
本探针必须变红（记录数明显少于并发数）。
Run: python test/lock_check.py
"""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
HOOK = os.path.join(ROOT, "hooks", "lookup_hook.py")
N = 20


def run_concurrent():
    d = tempfile.mkdtemp(prefix="lookup-lock-")
    # 测试用较长等待（生产默认 2s，可用端到端强一致性验证锁的正确性）
    env = dict(os.environ, LOOKUP_STATE_DIR=d, LOOKUP_LOCK_WAIT="30")
    # 预置一份大状态，拉长每次 load/save 的 JSON I/O 时间，放大竞态窗口
    sp = os.path.join(d, "locktest.json")
    seed = {"v": 1, "session_id": "locktest", "tool_calls": 0,
            "recent_log": [{"tool": "Read", "brief": "x" * 200, "ok": True}
                           for _ in range(4000)]}
    with open(sp, "w", encoding="utf-8") as f:
        json.dump(seed, f)
    # 用一个共同的起跑闸：所有子进程先起好，再一起喂输入
    procs = []
    for i in range(N):
        p = subprocess.Popen(
            [sys.executable, HOOK, "post-use"],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env=env, text=True,
        )
        procs.append((p, i))
    payloads = [json.dumps({"session_id": "locktest", "tool_name": "Read",
                            "tool_input": {"file_path": "f%d.txt" % i}}) for _, i in procs]
    # stdin 一起写、不等待读端，尽可能让各进程的读改写窗口重叠
    for (p, _), payload in zip(procs, payloads):
        p.stdin.write(payload)
        p.stdin.flush()
    for p, _ in procs:
        try:
            p.stdin.close()
        except Exception:
            pass
    for p, _ in procs:
        p.wait()
    with open(sp, encoding="utf-8") as f:
        st = json.load(f)
    return st.get("tool_calls", 0), d


def probe_heartbeat():
    """v0.8 心跳：持锁者临界区超过 STALE_AFTER 时，活锁不得被等待者误接管。

    回退验证：把 state_lock.__enter__ 里的 self._start_heartbeat() 删掉，
    本探针必须变红（B 会抢到锁）。
    """
    import threading
    import time
    import shutil as _sh
    if os.path.join(ROOT, "hooks") not in sys.path:
        sys.path.insert(0, os.path.join(ROOT, "hooks"))
    # 探针用自己的临时目录，避免污染主状态。
    # 注意：state_path() 在调用时才读 LOOKUP_STATE_DIR，故隔离须覆盖整个探针期。
    _d = tempfile.mkdtemp(prefix="lookup-hb-")
    _old_state = os.environ.get("LOOKUP_STATE_DIR")
    os.environ["LOOKUP_STATE_DIR"] = _d
    import lookup_hook as H
    old_stale, old_hb = H.state_lock.STALE_AFTER, H.state_lock.HEARTBEAT_SEC
    H.state_lock.STALE_AFTER = 2.0      # 收紧便于测试
    H.state_lock.HEARTBEAT_SEC = 0.3
    try:
        sid = "hbprobe"
        held = {}
        mtime_ok = {}

        def holder():
            with H.state_lock(sid, wait=0):
                p = H.state_path(sid) + ".lock"
                m1 = os.path.getmtime(p)
                time.sleep(1.0)
                m2 = os.path.getmtime(p)
                mtime_ok["advanced"] = m2 > m1        # 心跳应推进 mtime
                time.sleep(2.6)                       # 总持锁 > STALE_AFTER
                held["done"] = True

        th = threading.Thread(target=holder)
        th.start()
        time.sleep(0.5)                               # 等持锁者拿到锁并起心跳
        with H.state_lock(sid, wait=1.0) as lk:
            got_b = lk.acquired
        th.join()
        return mtime_ok.get("advanced", False), got_b
    finally:
        H.state_lock.STALE_AFTER, H.state_lock.HEARTBEAT_SEC = old_stale, old_hb
        if _old_state is None:
            os.environ.pop("LOOKUP_STATE_DIR", None)
        else:
            os.environ["LOOKUP_STATE_DIR"] = _old_state
        _sh.rmtree(_d, ignore_errors=True)


def main():
    if os.environ.get("LOOKUP_STATE_DIR"):
        print("SKIP（已在外层指定 LOOKUP_STATE_DIR）")
        return 0
    import shutil
    # 用足够长的锁等待（LOOKUP_LOCK_WAIT=30）验证「锁本身正确」：
    # 此时有锁应做到零丢失；把 with state_lock 换成透传后，必须出现丢失。
    # （生产默认等待 2s——有界等待是刻意的可用性取舍，不卡会话。）
    calls_locked, d1 = run_concurrent()
    shutil.rmtree(d1, ignore_errors=True)
    lost_locked = N - calls_locked
    print("并发 %d，最终 tool_calls=%d，丢 %d" % (N, calls_locked, lost_locked))
    ok = True
    if lost_locked == 0:
        print("PASS  lock_check.py: 跨进程锁保证并发事件零丢失")
    else:
        print("FAIL  lock_check.py: 丢 %d 次，锁未起作用或被绕过" % lost_locked)
        ok = False

    # 心跳探针（v0.8）
    advanced, got_b = probe_heartbeat()
    if advanced and not got_b:
        print("PASS  lock_check.py: 心跳刷新 mtime，活锁未被误接管")
    else:
        print("FAIL  lock_check.py: 心跳失效（mtime 推进=%s，被误接管=%s）"
              % (advanced, got_b))
        ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
