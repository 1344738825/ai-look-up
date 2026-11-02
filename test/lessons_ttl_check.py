"""教训登记簿 TTL：超期未被再次踩到的坑位必须被淘汰。

回退验证：把 lookup_hook._lessons_alive 改成直接返回原列表（不做 TTL 过滤），
本探针必须变红——过期坑位会随注入出现在新回合开头。
Run: python test/lessons_ttl_check.py
"""
import json
import os
import subprocess
import sys
import tempfile
import time

# Windows 宿主默认 ANSI 代码页,测试输出含中文,统一走 UTF-8。
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
HOOK = os.path.join(ROOT, "hooks", "lookup_hook.py")

FRESH = "python fresh_probe.py"      # 昨天刚被踩过 → 必须保留
STALE = "python stale_probe.py"      # 8 天前踩过(TTL 默认 7 天) → 必须淘汰
NO_TS = "python ghost_probe.py"      # 无 last_seen 的旧格式条目 → 视为过期淘汰


def seed_lessons(d):
    now = time.time()
    with open(os.path.join(d, "lessons.json"), "w", encoding="utf-8") as f:
        json.dump([
            {"pattern": STALE, "correction": "旧坑位", "hits": 99,
             "last_seen": now - 8 * 86400},
            {"pattern": FRESH, "correction": "新坑位", "hits": 2,
             "last_seen": now - 86400},
            {"pattern": NO_TS, "correction": "旧格式", "hits": 50},
        ], f)


def run_hook(action, d, payload):
    env = dict(os.environ, LOOKUP_STATE_DIR=d)
    p = subprocess.run([sys.executable, HOOK, action], input=json.dumps(payload),
                       capture_output=True, env=env, text=True, encoding="utf-8")
    if p.returncode != 0:
        raise AssertionError("hook exit %d: %s" % (p.returncode, p.stderr[-400:]))
    return p.stdout


def read_lessons(d):
    with open(os.path.join(d, "lessons.json"), encoding="utf-8") as f:
        return {e["pattern"] for e in json.load(f)}


def main():
    d = tempfile.mkdtemp(prefix="lookup-ttl-")
    seed_lessons(d)

    # 1) 回合开始注入:过期坑位不得进入 Top 3
    out = run_hook("prompt", d, {"session_id": "ttl-test"})
    assert FRESH in out, "fresh lesson must still be injected"
    assert STALE not in out, "expired lesson must not be injected"
    assert NO_TS not in out, "lesson without last_seen must not be injected"
    print("PASS  prompt injection skips expired lessons")

    # 2) 失败循环登记(3 连败):写入时顺带物理淘汰过期条目。
    #    失败命令用 other_probe:若复用 stale_probe,TTL 淘汰后同一坑位会被当作
    #    新坑位重新登记(hits=1),那是对的行为,但不能用它验证"淘汰"本身。
    payload = {"session_id": "ttl-test", "tool_name": "Bash",
               "tool_input": {"command": "python other_probe.py --retry"}}
    for _ in range(3):
        out = run_hook("post-fail", d, payload)
    assert "失败循环" in out, "fail-loop must fire on 3rd consecutive failure"
    remaining = read_lessons(d)
    assert any("other_probe" in p for p in remaining), \
        "the new failure must be registered as a fresh lesson"
    assert STALE not in remaining, "expired lesson must be pruned from lessons.json on write"
    assert NO_TS not in remaining, "legacy lesson without last_seen must be pruned"
    assert FRESH in remaining, "fresh lesson must survive pruning"
    print("PASS  register_lesson prunes expired entries from lessons.json")

    # 3) TTL=0(LOOKUP_lessons_ttl_days=0)→ 永不淘汰,旧行为可回退
    d2 = tempfile.mkdtemp(prefix="lookup-ttl-off-")
    seed_lessons(d2)
    env = dict(os.environ, LOOKUP_STATE_DIR=d2, LOOKUP_LESSONS_TTL_DAYS="0")
    p = subprocess.run([sys.executable, HOOK, "prompt"],
                       input=json.dumps({"session_id": "ttl-off-test"}),
                       capture_output=True, env=env, text=True, encoding="utf-8")
    assert STALE in p.stdout, "TTL=0 must keep old entries (never expire)"
    print("PASS  lessons_ttl_days=0 disables expiry (legacy behaviour)")

    print("ALL LESSONS TTL CHECKS PASSED")


if __name__ == "__main__":
    main()
