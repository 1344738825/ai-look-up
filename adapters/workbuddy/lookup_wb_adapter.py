#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WorkBuddy / CodeBuddy CLI -> ai-look-up 适配 shim。

WorkBuddy 的 hook 协议与 Claude Code 同形（payload 字段 session_id / tool_name /
tool_input / tool_response / hook_event_name 全部同名），并且额外提供**独立的**
PostToolUseFailure 事件，因此失败判定不需要从 tool_response.is_error 推导。

事件映射（核心协议四个动作：prompt / post-use / post-fail / stop）：
  UserPromptSubmit     -> prompt
  PostToolUse          -> post-use
  PostToolUseFailure   -> post-fail        （原生事件，比 CC 的推导更准）
  Stop                 -> stop
  SubagentStop         -> 忽略（子代理结束不代表主回合结束）

用法二选一，推荐 auto（读 stdin 的 hook_event_name 自动判定，配置只需一条命令）：
  python lookup_wb_adapter.py            # auto
  python lookup_wb_adapter.py post-use   # 强制指定动作

stdout 原样透传核心输出（hookSpecificOutput.additionalContext 同形），
任何异常都静默 exit 0，绝不影响主会话。
"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CORE = os.path.abspath(os.path.join(HERE, "..", "..", "hooks", "lookup_hook.py"))

EVENT_TO_ACTION = {
    "UserPromptSubmit": "prompt",
    "PostToolUse": "post-use",
    "PostToolUseFailure": "post-fail",
    "Stop": "stop",
}

VALID_ACTIONS = ("prompt", "post-use", "post-fail", "stop")


def read_stdin():
    # 手动在终端运行时没有管道输入，直接读会挂住，先看是不是 tty。
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return {}
    except Exception:
        return {}
    try:
        raw = sys.stdin.buffer.read()
        text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
        return json.loads(text) if text and text.strip() else {}
    except Exception:
        return {}


def resolve_action(argv, data):
    """命令行显式动作优先；否则按 hook_event_name 判定；再无则视为 post-use。"""
    if len(argv) > 1 and argv[1] in VALID_ACTIONS:
        return argv[1]

    event = ""
    if isinstance(data, dict):
        event = data.get("hook_event_name") or data.get("hookEventName") or ""
    if event:
        return EVENT_TO_ACTION.get(event, "")  # 未知事件返回空 -> 静默退出
    return "post-use"


def build_payload(data):
    """归一成核心读取的字段；未识别字段原样保留，核心会忽略。"""
    if not isinstance(data, dict):
        data = {}
    payload = dict(data)
    payload["session_id"] = data.get("session_id") or data.get("sessionId") or ""
    payload["tool_name"] = data.get("tool_name") or data.get("toolName") or "Unknown"
    payload["tool_input"] = data.get("tool_input") or data.get("toolInput") or {}
    # WorkBuddy 的失败事件把错误信息放在 error 字段（string）
    err = data.get("error")
    if isinstance(err, str) and err:
        payload["error"] = err
    return payload


def normalize_output(action, raw):
    """核心输出原样透传，但 Stop 的 `{"decision":"block"}` 在 WorkBuddy 里是
    deprecated 字段（仍被识别，日志会告警）。Stop 时额外补一份
    hookSpecificOutput.additionalContext，两条路径任一被消费即可生效。
    """
    try:
        obj = json.loads(raw.decode("utf-8"))
    except Exception:
        return raw
    if not isinstance(obj, dict):
        return raw
    if action == "stop" and obj.get("decision") == "block":
        reason = obj.get("reason") or ""
        hso = obj.setdefault("hookSpecificOutput", {})
        if isinstance(hso, dict) and reason:
            hso.setdefault("hookEventName", "Stop")
            hso.setdefault("additionalContext", reason)
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    try:
        data = read_stdin()
        action = resolve_action(sys.argv, data)
        if action not in VALID_ACTIONS:
            return 0
        if not os.path.exists(CORE):
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
            sys.stdout.buffer.write(normalize_output(action, proc.stdout))
            sys.stdout.buffer.flush()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
