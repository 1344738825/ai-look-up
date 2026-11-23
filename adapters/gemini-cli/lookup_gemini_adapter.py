#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Gemini CLI -> ai-look-up 适配 shim。

同时兼容 Gemini CLI hooks v1 事件名(BeforeAgent / BeforeTool / AfterTool / SessionEnd)
与 Claude Code 风格事件名(UserPromptSubmit / PreToolUse / PostToolUse / Stop)。
支持零参数 auto 模式(从 stdin JSON 的 hook_event_name 自动识别动作)或命令行显式传参。
"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CORE = os.path.abspath(os.path.join(HERE, "..", "..", "hooks", "lookup_hook.py"))

EVENT_TO_ACTION = {
    "BeforeAgent": "prompt",
    "UserPromptSubmit": "prompt",
    "BeforeTool": "pre-use",
    "PreToolUse": "pre-use",
    "AfterTool": "post-use",
    "PostToolUse": "post-use",
    "PostToolUseFailure": "post-fail",
    "SessionEnd": "stop",
    "AfterAgent": "stop",
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
        ev = data.get("hook_event_name") or data.get("hookEventName") or data.get("event") or ""
        action = EVENT_TO_ACTION.get(ev, "post-use" if not ev else "")
    if action == "post-use":
        resp = data.get("tool_response") or data.get("toolResponse") or data.get("result")
        is_err = (isinstance(resp, dict) and bool(resp.get("is_error") or resp.get("isError") or resp.get("error"))) or bool(data.get("error"))
        if is_err:
            action = "post-fail"
    return action


def build_payload(data):
    payload = dict(data)
    payload["session_id"] = data.get("session_id") or data.get("sessionId") or ""
    payload["tool_name"] = data.get("tool_name") or data.get("toolName") or data.get("tool") or "Unknown"
    payload["tool_input"] = data.get("tool_input") or data.get("toolInput") or data.get("args") or {}
    return payload


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    try:
        data = read_stdin()
        action = resolve_action(sys.argv, data)
        if action not in VALID_ACTIONS or not os.path.exists(CORE):
            return 0
        payload = build_payload(data)
        proc = subprocess.run(
            [sys.executable, CORE, action],
            input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
        if proc.stdout:
            sys.stdout.buffer.write(proc.stdout)
            sys.stdout.buffer.flush()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
