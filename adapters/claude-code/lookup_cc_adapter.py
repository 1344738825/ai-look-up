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


def main():
    action = sys.argv[1] if len(sys.argv) > 1 else "post-use"
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
        if not isinstance(data, dict):
            data = {}
    except Exception:
        return 0

    payload = {
        "session_id": data.get("session_id") or data.get("sessionId") or "",
        "tool_name": data.get("tool_name") or data.get("toolName") or "Unknown",
        "tool_input": data.get("tool_input") or data.get("toolInput") or {},
    }

    if action == "post-use":
        resp = data.get("tool_response")
        err = isinstance(resp, dict) and bool(resp.get("is_error") or resp.get("isError"))
        if err:
            action = "post-fail"
    if action == "prompt":
        action = "prompt"
    if action not in ("prompt", "post-use", "post-fail", "stop"):
        return 0

    try:
        proc = subprocess.run(
            [sys.executable, CORE, action],
            input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30,
        )
        out = proc.stdout
        if out:
            sys.stdout.buffer.write(out)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
