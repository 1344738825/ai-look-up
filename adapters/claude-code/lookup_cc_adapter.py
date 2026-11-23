#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Claude Code → ai-look-up 适配 shim(实验性)。

事件映射(核心协议四个动作:prompt / post-use / post-fail / stop):
  UserPromptSubmit          → prompt     (重置回合计数)
  PostToolUse  (成功响应)    → post-use
  PostToolUse  (is_error)   → post-fail  (Claude Code 无独立失败事件,按响应推导)
  Stop                      → stop

settings.json 配置(把 <REPO> 换成仓库路径):
  {
    "hooks": {
      "UserPromptSubmit": [{"hooks": [{"type": "command",
        "command": "python <REPO>/adapters/claude-code/lookup_cc_adapter.py prompt"}]}],
      "PostToolUse": [{"hooks": [{"type": "command",
        "command": "python <REPO>/adapters/claude-code/lookup_cc_adapter.py post-use"}]}],
      "Stop": [{"hooks": [{"type": "command",
        "command": "python <REPO>/adapters/claude-code/lookup_cc_adapter.py stop"}]}]
    }
  }

stdout 直接透传核心输出(hookSpecificOutput.additionalContext 同形);
任何错误都静默退出(退出码 0),不影响主会话。
"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CORE = os.path.abspath(os.path.join(HERE, "..", "..", "hooks", "lookup_hook.py"))


EVENT_TO_ACTION = {
    "UserPromptSubmit": "prompt",
    "PreToolUse": "pre-use",
    "PostToolUse": "post-use",
    "PostToolUseFailure": "post-fail",
    "Stop": "stop",
}

VALID_ACTIONS = ("prompt", "pre-use", "post-use", "post-fail", "stop")


def read_stdin():
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return {}
    except Exception:
        return {}
    try:
        raw = sys.stdin.buffer.read()
        text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
        data = json.loads(text) if text and text.strip() else {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def resolve_action(argv, data):
    if len(argv) > 1 and argv[1] in VALID_ACTIONS:
        action = argv[1]
    else:
        ev = data.get("hook_event_name") or data.get("hookEventName") or ""
        action = EVENT_TO_ACTION.get(ev, "post-use" if not ev else "")
    if action == "post-use":
        resp = data.get("tool_response")
        err = (isinstance(resp, dict) and bool(resp.get("is_error") or resp.get("isError"))) or bool(data.get("error"))
        if err:
            action = "post-fail"
    return action


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    data = read_stdin()
    action = resolve_action(sys.argv, data)
    if action not in VALID_ACTIONS:
        return 0

    payload = dict(data)
    payload["session_id"] = data.get("session_id") or data.get("sessionId") or ""
    payload["tool_name"] = data.get("tool_name") or data.get("toolName") or "Unknown"
    payload["tool_input"] = data.get("tool_input") or data.get("toolInput") or {}

    try:
        proc = subprocess.run(
            [sys.executable, CORE, action],
            input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30,
        )
        out = proc.stdout
        if out:
            sys.stdout.buffer.write(out)
            sys.stdout.buffer.flush()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
