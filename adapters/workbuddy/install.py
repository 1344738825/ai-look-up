#!/usr/bin/env python3
"""把 ai-look-up 的 4 个钩子写进 WorkBuddy / CodeBuddy CLI 的用户级 settings.json。

用法:
    python install.py                    # 自动识别已装的客户端
    python install.py --client workbuddy # 只配 WorkBuddy 桌面端
    python install.py --client codebuddy # 只配 CodeBuddy CLI
    python install.py --all              # 两个都配
    python install.py --dry-run          # 只打印将要写入的内容

行为:
  1. 探测 python 绝对路径(正斜杠),供 hook 命令使用;
  2. 备份目标 settings.json;
  3. 按顶层键合并 hooks,其余键原样保留(不存在则创建 {"hooks": ...});
  4. 用假 payload 跑一次 shim 自检,打印结果。
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ADAPTER = os.path.join(HERE, "lookup_wb_adapter.py")

CLIENTS = {
    "workbuddy": os.path.join(os.path.expanduser("~"), ".workbuddy", "settings.json"),
    "codebuddy": os.path.join(os.path.expanduser("~"), ".codebuddy", "settings.json"),
}

TIMEOUTS = {"UserPromptSubmit": 15, "PostToolUse": 15, "PostToolUseFailure": 15, "Stop": 20}


def slash(p):
    return p.replace(os.sep, "/")


def build_hooks(python_exe, adapter):
    cmd = "%s %s" % (slash(python_exe), slash(adapter))
    hooks = {}
    for event, timeout in TIMEOUTS.items():
        hooks[event] = [{
            "hooks": [{
                "type": "command",
                "command": cmd,
                "timeout": timeout,
                "description": "ai-look-up: %s" % event,
            }]
        }]
    return hooks


def detect_clients():
    return [name for name, path in CLIENTS.items() if os.path.isdir(os.path.dirname(path))]


def load_json(path):
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception as exc:  # 坏 JSON 不硬改,交给用户
        sys.exit("❌ %s 不是合法 JSON(%s),已中止,未做任何修改。" % (path, exc))


def self_test(python_exe):
    """用临时状态目录跑一次 PostToolUse,确认 shim 能写出状态文件。"""
    tmp = tempfile.mkdtemp(prefix="lookup-selftest-")
    env = dict(os.environ, LOOKUP_STATE_DIR=tmp)
    payload = json.dumps({
        "hook_event_name": "PostToolUse",
        "session_id": "install-selftest",
        "tool_name": "Bash",
        "tool_input": {"command": "ls"},
        "tool_response": {"output": "ok"},
    })
    try:
        proc = subprocess.run(
            [python_exe, ADAPTER], input=payload, capture_output=True,
            text=True, timeout=30, env=env,
        )
    except Exception as exc:
        return False, "shim 无法执行: %s" % exc

    state = os.path.join(tmp, "install-selftest.json")
    if not os.path.exists(state):
        detail = (proc.stderr or proc.stdout or "").strip()[:300]
        return False, "shim 未写出状态文件 (exit=%s) %s" % (proc.returncode, detail)

    try:
        with open(state, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as exc:
        return False, "状态文件不可解析: %s" % exc
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if data.get("tool_calls", 0) < 1:
        return False, "状态文件 tool_calls 为 0,判定逻辑可能未跑通"
    return True, "shim 正常,写入 %d 次工具调用记录" % data["tool_calls"]


def install(client, python_exe, adapter, dry_run=False):
    path = CLIENTS[client]
    parent = os.path.dirname(path)
    print("\n== %s -> %s" % (client, path))

    cfg = load_json(path)
    had = cfg.get("hooks")
    cfg["hooks"] = build_hooks(python_exe, adapter)

    if dry_run:
        print(json.dumps(cfg["hooks"], ensure_ascii=False, indent=2))
        print("(dry-run,未写入)")
        return True

    os.makedirs(parent, exist_ok=True)
    if os.path.exists(path):
        backup = "%s.bak-%s" % (path, time.strftime("%Y%m%d-%H%M%S"))
        shutil.copy2(path, backup)
        print("  备份: %s" % backup)
    else:
        print("  目标文件不存在,将新建")

    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, path)

    load_json(path)  # 回读校验
    print("  写入 OK,顶层键 %d 个,hooks 事件 %s%s" % (
        len(cfg), "/".join(cfg["hooks"]),
        "" if had is None else "(覆盖了原有的 hooks)",
    ))
    return True


def main():
    ap = argparse.ArgumentParser(description="为 ai-look-up 安装 WorkBuddy / CodeBuddy CLI 钩子")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--client", choices=sorted(CLIENTS), help="只配指定客户端")
    g.add_argument("--all", action="store_true", help="两个客户端都配")
    ap.add_argument("--dry-run", action="store_true", help="只打印,不写入")
    ap.add_argument("--python", default=sys.executable, help="hook 里使用的 python 绝对路径")
    ap.add_argument("--adapter", default=ADAPTER, help="适配器脚本路径")
    args = ap.parse_args()

    python_exe = os.path.abspath(args.python)
    adapter = os.path.abspath(args.adapter)

    if not os.path.exists(python_exe):
        sys.exit("❌ python 不存在: %s" % python_exe)
    if not os.path.exists(adapter):
        sys.exit("❌ 适配器不存在: %s" % adapter)

    print("python : %s" % slash(python_exe))
    print("adapter: %s" % slash(adapter))

    ok, detail = self_test(python_exe)
    print("%s 自检: %s" % ("✅" if ok else "❌", detail))
    if not ok:
        sys.exit("适配器自身跑不通,先修这个再配钩子。")

    if args.client:
        targets = [args.client]
    elif args.all:
        targets = sorted(CLIENTS)
    else:
        targets = detect_clients()
        if not targets:
            sys.exit("未探测到已安装的客户端,请用 --client 指定。")

    for client in targets:
        install(client, python_exe, adapter, args.dry_run)

    if not args.dry_run:
        print("\n完成。下次工具调用后检查状态文件:")
        print("  %TEMP%/zcode-ai-look-up/<session_id>.json")
        print("若脚本提示 ok 但客户端无动作,新开会话或重启客户端。")


if __name__ == "__main__":
    main()
