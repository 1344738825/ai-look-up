# AI 抬头 (ai-look-up)

让 AI 在长时间空跑时**中途自我审查**的 ZCode 插件。

AI 长任务里最常见的浪费不是做错,而是**空跑**:换名字重跑同一个探针脚本(`_peek2.py`、`_peek3.py`、`_peek4.py`…)、盲目重试失败的命令、一两个小时只跑不改却毫无产出。本插件通过会话钩子持续观察 AI 的行为节奏,一旦命中空跑特征,就向对话注入一段「抬头」提醒,要求 AI 停下来对照目标、检查近几步是否带来新信息、决定继续 / 换方法 / 汇报。

## 安装

**方式一:插件市场(ZCode / DeepSeek Harness 等)**

把本仓库克隆或下载解压,在客户端中打开 **插件市场 → 添加 → 添加插件市场**,选择仓库目录(根目录即市场清单 `marketplace.json` 所在),然后在 **个人 → ai-look-up-market** 中安装「AI 抬头」。

**方式二:Git 仓库直装(DeepSeek Harness)**

DeepSeek Harness 的插件页支持直接粘贴 Git 仓库地址安装——本仓库同时是一个 **cordis bundle**(`package.json` 声明 `dsh.bundle.patch`),会以 Host 插件形式安装,通过 `tools/result` / `session/event` 事件观察行为节奏,经 `agent.inject()` 注入提醒,无需 Python。

**要求:** ZCode 安装方式需要系统可用 `python` 命令(3.8+),若可执行名不同,修改 `hooks/hooks.json` 中的 4 处 `python` 即可;dsh 安装方式无此要求(纯 JS)。

## 工作原理

插件注册 4 个会话钩子(不拦截、不阻断任何工具调用,纯观察):

| 钩子 | 作用 |
|---|---|
| `UserPromptSubmit` | 用户新指令到达时,开启新的工作回合(重置节奏计数) |
| `PostToolUse` | 记录每次工具调用,评估空跑特征,命中则注入提醒 |
| `PostToolUseFailure` | 累计连续失败,识别盲目重试循环 |
| `Stop` | 会话结束时若长期零产出,请求一次继续以强制复盘(自限 2 次) |

**空跑特征(按证据强度排序,任一命中即提醒,带冷却防刷屏):**

1. **完全相同命令原样重复** —— 最近 5 条 Bash 命令中有 3 条原样相同(参数级比对):同样的输入必然得到同样的结果;
2. **换汤不换药的相似命令** —— 最近 5 条命令归一化后有 3 条相同。归一化会抹掉文件名里的序号,所以 `python _peek2.py` 和 `python _peek3.py` 会被视为同一种试探;
3. **超长零产出** —— 会话持续超过 25 分钟、调用频繁,却从未修改过任何文件;
4. **调用密集零产出** —— 自上次提醒以来 30 次调用期间没有文件修改;
5. **失败循环** —— 工具连续失败 3 次,提醒停止盲目重试。

**升级阶梯**:同一会话累计 3 次提醒后仍未改善,后续提醒会加重语气,明确建议 AI 放弃当前方法、直接向用户汇报卡点——而不是继续轻声提示。

**本地时钟锚点(倍增间隔)**:AI 对时间流逝的感觉不可靠,自己预估的耗时几乎总会超时。空闲运行期间,钩子向对话注入真实本地时间和本段已耗时长,要求 AI 凡是涉及耗时预估、汇报一律以真实时钟为准;所有空跑提醒的文案里也都会带上当前时间和开始时间作为锚点。间隔从 5 分钟起步并逐次倍增(5→10→20→40,封顶 60),新回合重置——开局最密、稳定后最省,注入的始终是插件从真实时钟现算的值,不存在虚拟漂移。

**独立审查者(v0.4)**:空跑提醒触发时,插件会用**独立上下文**调一次 LLM 审查者——它拿到的材料是原始任务描述、最近 N 次工具调用摘要和运行统计,与主代理完全隔离,输出严格 JSON 判定(`on-track / drifting / stuck` + 依据 + 建议),把"自查"升级成"他审"。dsh 端默认开启(复用会话的 `llm` 服务与主代理同款模型路由,异步不阻塞);ZCode 端默认关闭,在 `~/.zcode/ai-look-up.json` 里给 `llm_api_key` 后启用(直调 OpenAI 兼容接口,默认指向 DeepSeek)。审查失败或超时,一律回退到静态自查清单。

**目标漂移巡检(v0.5)**:长上下文任务最常见的隐性失败是"子任务喧宾夺主"——最初为核验某个决定而查的资料,做着做着变成了独立目标。每 30 次调用,独立审查者以漂移专项提示词对照最初任务审查一次方向;判定 `drifting/stuck` 时注入收束建议,`on-track` 时保持静默。需要审查者可用(见上)。

**折返编辑检测(v0.5)**:对每次文件修改记录内容指纹,内容"改了又改回"回到先前见过的状态即触发——原地打转是纯统计就能抓的空跑形态,双端零 LLM 成本。

**提醒有效性自适应(v0.5)**:每次提醒发出 10 次调用后结算是否改变了行为(出现修改/失败停止/命令模式变化),按各类提醒的历史有效率自动调整冷却——无效的提醒降噪(冷却翻倍),有效的加强(冷却缩到 3/4)。dsh 端统计随宿主进程存续,ZCode 端持久化到 `adaptive.json`。

**教训登记簿(v0.5)**:失败循环触发时,把失败命令的归一化模式自动登记为"坑位"(按被踩次数排序);每个新 agent / 新回合开始时,把 Top 3 已知坑位注入对话——教训不再依赖对话记忆,上下文压缩也带不走。后续版本计划:命令执行前按模式匹配账本,命中即拦截(dsh `ctx.tools.guard()` / ZCode `PreToolUse`),把"不再踩"从建议升级为约束。

**独立审查者(v0.4)**:空跑提醒触发时,插件会用**独立上下文**调一次 LLM 审查者——它拿到的材料是原始任务描述、最近 N 次工具调用摘要和运行统计,与主代理完全隔离,输出严格 JSON 判定(`on-track / drifting / stuck` + 依据 + 建议),把"自查"升级成"他审"。dsh 端默认开启(复用会话的 `llm` 服务与主代理同款模型路由,异步不阻塞);ZCode 端默认关闭,在 `~/.zcode/ai-look-up.json` 里给 `llm_api_key` 后启用(直调 OpenAI 兼容接口,默认指向 DeepSeek)。审查失败或超时,一律回退到静态自查清单。

提醒通过 `additionalContext` / `agent.inject()` 注入,内容为结构化的自我审查清单:对照最初目标 → 检查最近 5 次调用是否带来新信息 → 用一句话决定继续 / 换方法 / 汇报。

## 斜杠命令

- `/lookup-review` —— 手动触发一次完整的「抬头」自我审查(不依赖任何脚本路径)。
- `/lookup-status` —— 查看插件对当前会话的监控统计与判定。
- `/lookup-config` —— 查看 / 修改阈值配置。

## 配置

按优先级:内置默认 ← `~/.zcode/ai-look-up.json` ← `<项目>/.zcode/ai-look-up.json` ← 环境变量 `LOOKUP_<参数名大写>`。

```jsonc
// <项目>/.zcode/ai-look-up.json 示例:更激进地提醒
{
  "call_nudge_interval": 15,
  "cooldown_sec": 120,
  "long_run_minutes": 10,
  "stop_check": true
}
```

全部参数见 `/lookup-config`。

## 状态与调试

状态文件位于 `%TEMP%/zcode-ai-look-up/<session_id>.json`(每会话一个)。手动检查:

```bash
python <插件目录>/hooks/lookup_hook.py status
python <插件目录>/hooks/lookup_hook.py reset
```

## 要求与排错

- 需要系统可用 `python` 命令(3.8+)。若你的环境里 Python 需要完整路径,编辑 `hooks/hooks.json`,把 `python` 换成绝对路径即可。
- 钩子失败不影响会话(只记录在 ZCode 日志);若提醒未出现,先在 **设置 → 插件** 确认插件已启用、钩子显示为可运行,再用上面的命令手动跑一次脚本验证。
- 若插件详情中显示钩子输出校验失败,说明当前版本的输出信封格式有差异,把 `lookup_hook.py` 中 `post_use_output` / `stop_output` 的返回结构按日志提示调整即可。

## Acknowledgments

本项目为原创实现,思路受以下社区项目的启发,在此致谢:

- [disler/claude-code-hooks-mastery](https://github.com/disler/claude-code-hooks-mastery) —— 钩子生命周期与 Stop 钩子强制复盘模式;
- [Princeu3/agent-loop-detector](https://github.com/Princeu3/agent-loop-detector) —— 工具调用哈希比对检测循环(本项目的"原样重复"触发器借鉴了参数级比对的思路);
- [How I Built a Watchdog That Stops My AI Coding Agent From Looping Forever](https://dev.to/yureki_lab/how-i-built-a-watchdog-that-stops-my-ai-coding-agent-from-looping-forever-61h) —— 检测 + 逐级升级的策略。

与这些方案不同,本项目在运行中途(PostToolUse)分析行为节奏并以 `additionalContext` 轻量注入提醒,不打断、不重启;并新增了基于文件名序号归一化的"换汤不换药"变体检测。

