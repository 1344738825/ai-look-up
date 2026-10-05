# Gemini CLI 适配(脚手架,待校准)

Gemini CLI 于 2026 年初上线了 hooks 机制(HookRunner 以子进程方式执行命令钩子,
事件点覆盖 agent loop 的关键生命周期)。**本目录目前只提供映射表与骨架,未在真机验证**——
字段名以所用版本的官方 hooks 文档为准,欢迎 PR 补充验证结果。

## 事件映射草案(待按官方文档核对)

| Gemini CLI hook | 核心协议动作 | 说明 |
|---|---|---|
| 会话/用户输入开始类事件 | `prompt` | 重置回合计数 |
| 工具调用完成类事件(成功) | `post-use` | payload 需映射 tool 名与命令 |
| 工具调用完成类事件(失败) | `post-fail` | 若无独立失败事件,按响应中的错误标志推导 |
| 会话结束类事件 | `stop` | 结束复盘 |

## 接入步骤(校准后)

1. 在 `~/.gemini/settings.json`(或项目 `.gemini/settings.json`)的 hooks 配置里,
   把上表四个事件指向本 shim(参照 `../claude-code/lookup_cc_adapter.py` 的写法,
   核心 stdin 协议与 Claude Code hook payload 同形,大概率只需改字段名);
2. 用 `gemini` 跑一个 `_peek2.py / _peek3.py` 序列,确认控制台出现「AI 抬头」注入;
3. 把实际验证的字段映射回填到本文件并移除"待校准"标记。

## 为什么不复用 JS bundle

Gemini CLI hooks 是命令型钩子(与 Claude Code / ZCode 同类),走 Python 核心最短;
JS cordis bundle 是 dsh 专有的宿主内事件模型。核心检测逻辑两份实现已被
`spec.json` + `test/golden_*` 钉住,适配层任选一条路径都不产生行为漂移。
