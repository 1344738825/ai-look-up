#!/usr/bin/env python3
"""ai-look-up 独立审查 watcher（第三通道）。

常驻进程，轮询投递区，把钩子写下的"请求单"送去 LLM 审查，结论写回。
它活在宿主之外、主 agent 之外——这是它独立性的来源。

设计契约见同目录 REVIEW_CHANNEL.md。改协议先改那份文档。

用法:
    python lookup_watcher.py            # 常驻（前台）
    python lookup_watcher.py --once     # 处理一轮后退出（测试/定时任务用）
    python lookup_watcher.py --status   # 打印投递区状态后退出
    python lookup_watcher.py --stop     # 请求停止正在跑的 watcher

无第三方依赖：只用标准库 + urllib。
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

# ── 路径与常量 ──────────────────────────────────────────────────────────────
# 本脚本与 lookup_hook.py 同目录。刻意不 import 它——那会带进钩子的模块级副作用。
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)

DEFAULT_SPEC = {
    "request_poll_sec": 2,
    "request_ttl_sec": 900,
    "result_ttl_sec": 900,
    "result_settle_calls": 60,
    "escalate_after_reminders": 3,
}

# 默认配置里 watcher 关心的键。其它键与它无关。
DEFAULT_CFG = {
    "llm_api_key": "",
    "llm_api_base": "https://api.deepseek.com",
    "llm_model": "deepseek-chat",
    "llm_timeout_sec": 12,
    "llm_max_tokens": 200,
    "enabled": True,
    "deliver_review": False,
}


def _read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if data is not None else default
    except Exception:
        return default


def load_spec():
    spec = _read_json(os.path.join(_REPO_ROOT, "spec.json"), {}) or {}
    shared = spec.get("shared", {}) if isinstance(spec, dict) else {}
    out = dict(DEFAULT_SPEC)
    for k in DEFAULT_SPEC:
        if k in shared:
            out[k] = shared[k]
    return out


def load_cfg():
    """与 lookup_hook.load_config 同源顺序：默认 ← 插件 config.json ← ~/.zcode ← 项目 ← 环境变量。"""
    cfg = dict(DEFAULT_CFG)
    candidates = [
        os.path.join(_REPO_ROOT, "config.json"),
        os.path.expanduser(os.path.join("~", ".zcode", "ai-look-up.json")),
        os.path.join(os.getcwd(), ".zcode", "ai-look-up.json"),
    ]
    for path in candidates:
        data = _read_json(path)
        if isinstance(data, dict):
            for k in DEFAULT_CFG:
                if k in data:
                    cfg[k] = data[k]
    for k in DEFAULT_CFG:
        raw = os.environ.get("LOOKUP_" + k.upper())
        if raw is None or raw == "":
            continue
        if isinstance(DEFAULT_CFG[k], bool):
            cfg[k] = raw.strip().lower() in ("1", "true", "yes", "on")
        elif isinstance(DEFAULT_CFG[k], int):
            try:
                cfg[k] = int(float(raw))
            except Exception:
                pass
        else:
            cfg[k] = raw
    return cfg


def state_dir():
    base = os.environ.get("LOOKUP_STATE_DIR") or os.path.join(
        os.path.expanduser("~"), ".zcode-ai-look-up"
    )
    # 与 lookup_hook.state_dir 保持一致：它用 tempfile.gettempdir()
    try:
        import tempfile
        if not os.environ.get("LOOKUP_STATE_DIR"):
            base = os.path.join(tempfile.gettempdir(), "zcode-ai-look-up")
    except Exception:
        pass
    return base


def review_dir():
    d = os.path.join(state_dir(), "review")
    os.makedirs(d, exist_ok=True)
    return d


def log(msg):
    """追加一行日志。失败不影响主流程。"""
    try:
        line = "%s [pid %d] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), os.getpid(), msg)
        with open(os.path.join(review_dir(), "watcher.log"), "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


# ── 单例锁 ─────────────────────────────────────────────────────────────────
def _pid_alive(pid):
    if pid <= 0:
        return False
    if os.name == "nt":
        # Windows: 用 OpenProcess 探测；拿不到句柄即进程不存在
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        SYNCHRONIZE = 0x00100000
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, False, pid)
        if handle:
            kernel32.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def acquire_lock():
    """返回 (ok, lock_path)。抢到锁返回 True；已有活着的 watcher 返回 False。"""
    lock = os.path.join(review_dir(), "watcher.lock")
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode("ascii"))
        os.close(fd)
        return True, lock
    except FileExistsError:
        pass
    # 锁已存在：看持有者是否还活着（锁里是纯 PID 文本）
    try:
        with open(lock, encoding="utf-8") as f:
            holder = int((f.read() or "0").strip() or "0")
    except Exception:
        holder = 0
    if _pid_alive(holder):
        return False, lock
    log("接管失效锁（原持有者 PID %s 已死）" % holder)
    # 接管方式 = remove + O_EXCL 重抢（与 state_lock 的 rename 接管不同但安全性等价:
    # 两个竞争者都删时只有一个成功,重抢时 O_EXCL 保证单赢家,不会出现双持有者）。
    try:
        os.remove(lock)
    except OSError:
        return False, lock
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode("ascii"))
        os.close(fd)
        return True, lock
    except FileExistsError:
        return False, lock


def release_lock(lock):
    """释放锁。★Windows 注意:必须先关闭文件句柄再 os.remove——
    句柄未关时删除会抛 PermissionError（POSIX 允许 unlink 打开的文件,Windows 不允许）。
    这正是此前 release 静默失效的根因。
    """
    holder = 0
    try:
        with open(lock, encoding="utf-8") as f:
            holder = int((f.read() or "0").strip() or "0")
    except Exception:
        return
    if holder != os.getpid():
        return
    try:
        os.remove(lock)
    except OSError:
        pass


# ── LLM 调用 ───────────────────────────────────────────────────────────────
def call_llm(cfg, system, material):
    """直调 OpenAI 兼容接口。返回 (text, error_msg)。任何失败返回 ("", 原因)。"""
    api_key = (cfg.get("llm_api_key") or "").strip()
    if not api_key:
        return "", "no api key"
    base = (cfg.get("llm_api_base") or "https://api.deepseek.com").rstrip("/")
    payload = json.dumps({
        "model": cfg.get("llm_model") or "deepseek-chat",
        "temperature": 0,
        "max_tokens": int(cfg.get("llm_max_tokens") or 200),
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": material},
        ],
    }).encode("utf-8")
    req = urllib.request.Request(
        base + "/chat/completions", data=payload, method="POST",
        headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=cfg.get("llm_timeout_sec", 12)) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        return data["choices"][0]["message"]["content"], ""
    except urllib.error.HTTPError as e:
        return "", "HTTP %s" % e.code
    except Exception as e:
        return "", "%s: %s" % (type(e).__name__, e)


def parse_verdict(text):
    """从任意文本里抠出 JSON 判定。失败返回 None。"""
    if not text:
        return None
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        obj = json.loads(text[start:end + 1])
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None
    v = str(obj.get("verdict") or "").strip()
    if v not in ("on-track", "drifting", "stuck"):
        return None
    return {
        "verdict": v,
        "reason": str(obj.get("reason") or "")[:300],
        "suggestion": str(obj.get("suggestion") or "")[:300],
    }


# ── 投递区操作 ─────────────────────────────────────────────────────────────
def _id_safe(s):
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(s))[:160]


def claim(request_id):
    """原子领取。抢到返回 True。"""
    path = os.path.join(review_dir(), "claimed_%s.json" % _id_safe(request_id))
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(time.time()).encode("ascii"))
        os.close(fd)
        return True
    except FileExistsError:
        return False
    except OSError:
        return False


def write_result(req, verdict, reason, suggestion, elapsed_ms, model, err=""):
    rid = req.get("request_id") or "unknown"
    out = {
        "v": 1,
        "request_id": rid,
        "session_id": req.get("session_id", ""),
        "kind": req.get("kind", ""),
        "detail": req.get("detail", ""),
        "verdict": verdict,
        "reason": reason,
        "suggestion": suggestion,
        "finished_at": time.time(),
        "elapsed_ms": int(elapsed_ms),
        "model": model,
        # 从请求单回填，供钩子端取回时做场景指纹校验
        "tool_calls": req.get("tool_calls", 0),
        "prompt_resets": req.get("prompt_resets", 0),
        "goal_snapshot": req.get("goal_snapshot", ""),
    }
    if err:
        out["error"] = err
    path = os.path.join(review_dir(), "res_%s.json" % _id_safe(rid))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def drop_request(request_id):
    try:
        os.remove(os.path.join(review_dir(), "req_%s.json" % _id_safe(request_id)))
    except OSError:
        pass


def list_requests():
    d = review_dir()
    items = []
    for name in os.listdir(d):
        if not (name.startswith("req_") and name.endswith(".json")):
            continue
        req = _read_json(os.path.join(d, name))
        if isinstance(req, dict):
            items.append(req)
    items.sort(key=lambda r: r.get("created_at", 0))
    return items


def process_one(req, cfg, spec):
    rid = req.get("request_id") or "unknown"
    if not claim(rid):
        return "skipped-claimed"
    started = time.time()
    model = cfg.get("llm_model") or "deepseek-chat"
    text, err = call_llm(cfg, req.get("system", ""), req.get("material", ""))
    elapsed = (time.time() - started) * 1000
    if err:
        write_result(req, "error", "", "", elapsed, model, err=err)
        drop_request(rid)
        log("请求 %s 失败: %s" % (rid, err))
        return "error"
    verdict = parse_verdict(text)
    if not verdict:
        write_result(req, "error", "", "", elapsed, model, err="unparseable verdict")
        drop_request(rid)
        log("请求 %s 返回不可解析" % rid)
        return "error"
    write_result(req, verdict["verdict"], verdict["reason"], verdict["suggestion"],
                 elapsed, model)
    drop_request(rid)
    log("请求 %s 完成: %s (%.0fms)" % (rid, verdict["verdict"], elapsed))
    return verdict["verdict"]


def sweep_expired(spec):
    """请求单超时未处理 → 写 error 结论再删，避免钩子无限等待。"""
    now = time.time()
    ttl = float(spec.get("request_ttl_sec", 900))
    n = 0
    for req in list_requests():
        created = req.get("created_at", 0)
        if now - created <= ttl:
            continue
        if not claim(req.get("request_id", "")):
            continue
        write_result(req, "error", "", "", 0, "",
                     err="expired before processing")
        drop_request(req.get("request_id", ""))
        log("请求 %s 过期作废" % req.get("request_id"))
        n += 1
    return n


def cleanup_results(spec):
    """钩子端长期没取回的结论 → 清理。"""
    d = review_dir()
    now = time.time()
    ttl = float(spec.get("result_ttl_sec", 900)) * 2  # 留一倍冗余给钩子取
    n = 0
    for name in os.listdir(d):
        if not (name.startswith("res_") and name.endswith(".json")):
            continue
        p = os.path.join(d, name)
        res = _read_json(p)
        if not isinstance(res, dict):
            continue
        if now - res.get("finished_at", now) > ttl:
            try:
                os.remove(p)
                n += 1
            except OSError:
                pass
    # claimed_ 领取标记没有别的清理路径,滞留会一直涨(每次审查留一个)
    for name in os.listdir(d):
        if not (name.startswith("claimed_") and name.endswith(".json")):
            continue
        p = os.path.join(d, name)
        try:
            if now - os.path.getmtime(p) > ttl:
                os.remove(p)
                n += 1
        except OSError:
            pass
    return n


def status_report():
    d = review_dir()
    reqs = list_requests()
    res = [n for n in os.listdir(d) if n.startswith("res_") and n.endswith(".json")]
    claimed = [n for n in os.listdir(d) if n.startswith("claimed_") and n.endswith(".json")]
    lock = os.path.join(d, "watcher.lock")
    holder = ""
    if os.path.exists(lock):
        try:
            with open(lock, encoding="utf-8") as f:
                holder = (f.read() or "").strip()
        except Exception:
            holder = "?"
    cfg = load_cfg()
    print("投递目录: %s" % d)
    print("  watcher 锁: %s" % ("PID " + holder + (" (活)" if _pid_alive(int(holder or 0)) else " (死)")
                                 if holder else "无"))
    print("  待处理请求: %d" % len(reqs))
    for r in reqs:
        print("    - %s [%s] %s" % (r.get("request_id"), r.get("kind"), r.get("detail", "")[:50]))
    print("  待取回结论: %d" % len(res))
    print("  已领取标记: %d" % len(claimed))
    print("  已配置 key: %s" % ("是" if (cfg.get("llm_api_key") or "").strip() else "否"))
    print("  审查开关 deliver_review: %s" % cfg.get("deliver_review"))
    return 0


# ── 主循环 ─────────────────────────────────────────────────────────────────
def run_loop(once=False):
    spec = load_spec()
    cfg = load_cfg()
    if not (cfg.get("llm_api_key") or "").strip():
        log("未配置 llm_api_key，watcher 空转（只做 TTL 清理）。在 ~/.zcode/ai-look-up.json 里给 key 后重启。")
    poll = max(1, int(spec.get("request_poll_sec", 2)))
    log("watcher 启动，轮询间隔 %ss" % poll)
    try:
        while True:
            cfg = load_cfg()  # 每轮重读，支持热改配置
            if not cfg.get("enabled", True):
                log("enabled=false，watcher 退出")
                break
            if (cfg.get("llm_api_key") or "").strip():
                for req in list_requests():
                    process_one(req, cfg, spec)
            sweep_expired(spec)
            cleanup_results(spec)
            if once:
                break
            time.sleep(poll)
    except KeyboardInterrupt:
        log("收到中断，watcher 退出")
    return 0


def stop_running():
    lock = os.path.join(review_dir(), "watcher.lock")
    if not os.path.exists(lock):
        print("没有正在跑的 watcher")
        return 0
    try:
        with open(lock, encoding="utf-8") as f:
            pid = int((f.read() or "0").strip() or "0")
    except Exception:
        pid = 0
    if not _pid_alive(pid):
        print("锁里的 PID %s 已死，清理锁文件" % pid)
        try:
            os.remove(lock)
        except OSError:
            pass
        return 0
    try:
        if os.name == "nt":
            import ctypes
            PROCESS_TERMINATE = 0x0001
            h = ctypes.windll.kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
            if h:
                ctypes.windll.kernel32.TerminateProcess(h, 0)
                ctypes.windll.kernel32.CloseHandle(h)
        else:
            import signal
            os.kill(pid, signal.SIGTERM)
        print("已请求停止 watcher (PID %s)" % pid)
    except Exception as e:
        print("停止失败: %s" % e)
    return 0


def main(argv):
    if "--status" in argv:
        return status_report()
    if "--stop" in argv:
        return stop_running()
    once = "--once" in argv
    ok, lock = acquire_lock()
    if not ok:
        print("已有 watcher 在运行（锁: %s）。用 --status 查看，或 --stop 停止。" % lock)
        return 1
    try:
        return run_loop(once=once)
    finally:
        release_lock(lock)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
