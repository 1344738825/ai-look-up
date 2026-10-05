# ai-look-up 平台适配层

核心检测逻辑只有两份实现:Python(`hooks/lookup_hook.py`,供钩子命令型宿主调用)与
JS(`index.js`,DeepSeek Harness cordis bundle)。**适配层不做逻辑复制**,只做三件事:

1. **事件名映射**:把宿主的生命周期事件翻译成核心协议的四个动作
   `prompt` / `post-use` / `post-fail` / `stop`;
2. **payload 归一**:把宿主的工具调用字段(tool 名、命令、成败)映射到核心读取的
   `tool_name` / `tool_input` / `session_id`;
3. **输出透传**:核心输出 `hookSpecificOutput.additionalContext`(与 Claude Code
   hook 协议同形)或 `{"decision":"block","reason":...}`,由 shim 决定直接透传或转换。

所有行为阈值来自仓库根的 `spec.json`(双端唯一事实源),适配层不引入新的行为常量。

## 支持矩阵

| 宿主 | 适配方式 | 事件完备性 | 状态 |
|---|---|---|---|
| ZCode | 原生(`hooks/hooks.json` + Python) | 完整(含 PostToolUseFailure) | ✅ 稳定 |
| DeepSeek Harness | 原生(cordis bundle,纯 JS) | 完整 | ✅ 稳定 |
| Claude Code | `claude-code/lookup_cc_adapter.py` shim | 失败事件经 `tool_response.is_error` 推导 | ⚠️ 实验性,未在真机验证 |
| OpenCode | `opencode/ai-look-up-adapter.js` shim(回调 Python 核心) | `tool.execute.after` 驱动;一次性模式(`opencode run`)下事件不可靠(官方已知问题) | ⚠️ 实验性,未在真机验证 |
| Gemini CLI | `gemini-cli/`(映射文档 + 待校准 shim) | 依赖其 hooks v1 事件模型,需按官方文档核对字段名 | 🚧 脚手架 |

## 写一个新适配器需要做什么

约 30-60 行,以 Claude Code shim 为参照:

- stdin 收宿主 JSON → 映射成核心协议字段(原样保留未知字段,核心会忽略);
- 按宿主事件名选择核心动作(`prompt`/`post-use`/`post-fail`/`stop`);
- 子进程调 `python hooks/lookup_hook.py <动作>`,stdin 喂归一后的 JSON;
- stdout 原样透传(核心输出的就是宿主可消费的 hook JSON);核心静默时无输出。

失败事件不独立的宿主(如 Claude Code)用"成功响应里带错误标志"推导 `post-fail`;
两者都没有的宿主(纯 MCP 型,如 WorkBuddy)无法实现旁路监控,只能降级为
主动查询型 MCP 工具或事后审计 skill——不在本目录范围内,见 README 主文档。

> ⚠️ 除 ZCode / dsh 外的适配器均为社区贡献级别的**实验性**脚手架:
> 字段名以各宿主当前版本文档为准,欢迎提 PR 补真机验证结果。
