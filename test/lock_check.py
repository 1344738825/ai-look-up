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
    if lost_locked == 0:
        print("PASS  lock_check.py: 跨进程锁保证并发事件零丢失")
        return 0
    print("FAIL  lock_check.py: 丢 %d 次，锁未起作用或被绕过" % lost_locked)
    return 1


if __name__ == "__main__":
    sys.exit(main())
