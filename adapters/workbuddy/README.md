# WorkBuddy / CodeBuddy CLI 适配

WorkBuddy 的 hook 协议与 Claude Code 同形，payload 字段同名
（`hook_event_name` / `session_id` / `tool_name` / `tool_input` / `tool_response` /
`transcript_path`），并且**额外提供独立的 `PostToolUseFailure` 事件**，所以失败循环判定
不用像 Claude Code 那样从 `tool_response.is_error` 推导。

已实测(WorkBuddy 5.9.x, Windows, 2026-10-30):四个事件均可触发,提醒通过
`hookSpecificOutput.additionalContext` 注入,呈 `system-reminder` 形式进入主上下文。

## 事件映射

| 宿主事件 | 核心动作 | 说明 |
|---|---|---|
| `UserPromptSubmit` | `prompt` | 新回合,重置节奏计数 |
| `PostToolUse` | `post-use` | 记录调用,判空跑 |
| `PostToolUseFailure` | `post-fail` | 原生失败事件,payload 带 `error` 字符串 |
| `Stop` | `stop` | 回合结束,零产出时请求继续复盘(自限 2 次) |
| `SubagentStop` / 其他 | 忽略 | 子代理结束不代表主回合结束 |

## 配置

**先确认你在配哪个客户端——两者配置文件不同,写错不报错、钩子永不触发:**

| 客户端 | 用户级配置 |
|---|---|
| WorkBuddy 桌面端 | `~/.workbuddy/settings.json` |
| CodeBuddy CLI | `~/.codebuddy/settings.json` |

用户级（全局生效）写在对应文件；项目级按同目录规则（`<项目>/.workbuddy/settings.json`
或 `<项目>/.codebuddy/settings.json`，未逐一实测，以用户级为准）。格式相同：

推荐直接跑安装脚本，避免手改路径出错：

```bash
python adapters/workbuddy/install.py                     # 自动识别已装客户端
python adapters/workbuddy/install.py --client workbuddy  # 强制指定
python adapters/workbuddy/install.py --all               # 两个都配
```

脚本会：探测 python 绝对路径 → 备份已有配置 → 按顶层键合并 `hooks`（不覆盖其他键）
→ 用假 payload 跑一次 shim 自检 → 打印 `✅/❌`。

手改则原样抄下面这段：

```json
{
  "hooks": {
    "UserPromptSubmit": [
      { "hooks": [{ "type": "command", "timeout": 15,
        "command": "C:/path/to/python.exe C:/path/to/ai-look-up/adapters/workbuddy/lookup_wb_adapter.py" }] }
    ],
    "PostToolUse": [
      { "hooks": [{ "type": "command", "timeout": 15,
        "command": "C:/path/to/python.exe C:/path/to/ai-look-up/adapters/workbuddy/lookup_wb_adapter.py" }] }
    ],
    "PostToolUseFailure": [
      { "hooks": [{ "type": "command", "timeout": 15,
        "command": "C:/path/to/python.exe C:/path/to/ai-look-up/adapters/workbuddy/lookup_wb_adapter.py" }] }
    ],
    "Stop": [
      { "hooks": [{ "type": "command", "timeout": 20,
        "command": "C:/path/to/python.exe C:/path/to/ai-look-up/adapters/workbuddy/lookup_wb_adapter.py" }] }
    ]
  }
}
```

四条命令完全一样：shim 读 stdin 的 `hook_event_name` 自动判定动作，不需要传参。

改完配置通常**热生效**——实测(WorkBuddy 5.9.x, 2026-10-31)在写入后同会话内 7 秒即触发，
无需重启。若下一次工具调用后状态文件仍未刷新，再新开会话/重启客户端（hooks 也可能在会话
启动时缓存）。

### Stop 的 decision 字段

WorkBuddy 仍识别 `{"decision":"block","reason":...}`，但源码里已标 deprecated
（日志会告警 "Hook uses deprecated decision field"）。shim 在 Stop 动作上会自动补一份
`hookSpecificOutput.additionalContext`，两条路径同时给，任一被消费即生效，不必等上游改。

### 两个 Windows 坑

1. **路径用正斜杠**。hook 命令经 `bash -c` 执行，`C:\path\python.exe` 里的反斜杠会被
   bash 当转义吃掉，结果是 `C:pathpython.exe` 找不到。
2. **python 用绝对路径**。`bash -c` 不读 `.bashrc`，GUI 启动的进程 PATH 里通常没有
   用户自行安装的 python 目录。WorkBuddy 自带解释器在
   `~/.workbuddy/binaries/python/versions/<版本>/python.exe`，升版本后要同步改这里。

## 手动调试

```bash
# 状态与判定
python hooks/lookup_hook.py status
# 模拟一次重复命令事件（第 8 次会出提醒）
echo '{"hook_event_name":"PostToolUse","session_id":"dbg","tool_name":"Bash",
       "tool_input":{"command":"python _peek2.py"}}' | python adapters/workbuddy/lookup_wb_adapter.py
```

状态落在 `%TEMP%/zcode-ai-look-up/<session_id>.json`，可用 `LOOKUP_STATE_DIR` 改。
