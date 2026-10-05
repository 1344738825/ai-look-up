# AI 抬头 (ai-look-up)

[![ci](https://github.com/1344738825/ai-look-up/actions/workflows/ci.yml/badge.svg)](https://github.com/1344738825/ai-look-up/actions/workflows/ci.yml)

让 AI 在长时间空跑时**中途自我审查**的 ZCode 插件。

AI 在长任务里最常见的浪费是**空跑**。表现是:同一个探针脚本换个名字反复跑(`_peek2.py`、`_peek3.py`、`_peek4.py`…),失败的命令原样重试,一两个小时里一次文件都没改。本插件用会话钩子盯着这些行为,命中就让 AI 停一下:对照最初目标,看最近几步有没有带来新东西,然后决定继续、换方法还是汇报。

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

**升级阶梯**:同一会话累计 3 次提醒后仍未改善,后续提醒加重语气,直接建议 AI 放弃当前方法、向用户汇报卡点。

**本地时钟锚点(倍增间隔)**:AI 估不准自己的耗时,预估值几乎总是偏低。空闲运行期间,钩子把真实本地时间和本段已耗时长注入对话,要求 AI 凡是涉及耗时预估和汇报都以真实时钟为准;所有空跑提醒的文案里也都带上当前时间和开始时间作锚点。间隔从 5 分钟起步并逐次倍增(5→10→20→40,封顶 60),新回合重置——开局最密、稳定后最省。注入的值由插件从真实时钟现算。

**独立审查者(v0.4)**:空跑提醒触发时,插件用**独立上下文**调一次 LLM 审查者。它拿到的材料是原始任务描述、最近 N 次工具调用摘要和运行统计,与主代理完全隔离,输出严格 JSON 判定(`on-track / drifting / stuck` + 依据 + 建议),把"自查"升级成"他审"。dsh 端默认开启(复用会话的 `llm` 服务与主代理同款模型路由,异步不阻塞);ZCode 端默认关闭,在 `~/.zcode/ai-look-up.json` 里给 `llm_api_key` 后启用(直调 OpenAI 兼容接口,默认指向 DeepSeek)。审查失败或超时,一律回退到静态自查清单。

**目标漂移巡检(v0.5)**:长上下文任务的隐性失败是子任务喧宾夺主——最初为核验某个决定而查的资料,做着做着变成了独立目标。每 30 次调用,独立审查者以漂移专项提示词对照最初任务审查一次方向;判定 `drifting/stuck` 时注入收束建议,`on-track` 时保持静默。需要审查者可用(见上)。

**折返编辑检测(v0.5)**:对每次文件修改记录内容指纹,内容"改了又改回"到先前见过的状态即触发——原地打转靠纯统计就能抓,双端零 LLM 成本。

**提醒有效性自适应(v0.5)**:每次提醒发出 10 次调用后,结算它是否改变了行为(出现修改/失败停止/命令模式变化),再按各类提醒的历史有效率自动调整冷却——无效的降噪(冷却翻倍),有效的加强(冷却缩到 3/4)。dsh 端统计随宿主进程存续,ZCode 端持久化到 `adaptive.json`。

**教训登记簿(v0.5)**:失败循环触发时,把失败命令的归一化模式自动登记为"坑位"(按被踩次数排序);每个新 agent / 新回合开始时,把 Top 3 已知坑位注入对话——教训不再依赖对话记忆,上下文压缩也带不走。后续计划:命令执行前按模式匹配账本,命中即拦截(dsh `ctx.tools.guard()` / ZCode `PreToolUse`),把"不再踩"从建议变成硬约束。

**第三通道:投递请求 + 独立 watcher(v0.7)**:前面几种审查都还在"钩子/宿主自己这一侧"完成,主代理理论上仍能影响它。第三通道把审查**整体搬出宿主**:钩子在**升级阶梯末档**(同一会话第 3 次提醒仍未改善)时,只往投递区写一张"请求单"就立刻返回;一个常驻的独立进程(自带 API key)轮询请求单、读材料、调审查者、把结论写回;钩子在下一次事件里捎带注入结论。主代理**全程不在链路上**:它既不知道审查发生过,也没有任何环节能改写提示词、不发起、或丢弃结果。结论回注前过**三道闸**——场景指纹(用户换过指令则作废)、时效(默认 15 分钟)、步数(默认 60 步内),不过闸只删文件不注入,避免用"几十步前对着旧目标"的判定误导当前决策。默认关闭(dsh 端 `deliverReview` / ZCode 端 `deliver_review`),因为它花的是**用户自己的 API 额度**,只有到末档且显式配置了 key 才投递。协议契约见 `hooks/REVIEW_CHANNEL.md`,工作进程为 `hooks/lookup_watcher.py`。

**并发状态安全(v0.7)**:钩子是短命进程,多个会话事件可能**并发**落到同一个状态文件上。原实现是「读-改-写」无保护,20 个并发事件实测只保住 7~9 次记录,其余被整体覆盖丢失。v0.7 在整段「读 → 处理 → 写」外加了一把跨进程文件锁(`state_lock`),并解决了两个平台坑:释放锁时必须**先关闭文件句柄再删除**(Windows 上句柄未关会静默删除失败),接管失效锁用**原子 rename** 而非直接删除(直接删会误删刚被别人抢到的新锁)。锁的获取带**有界等待**(默认 2 秒),拿不到就放行——绝不为了计数精确而卡住会话。

提醒通过 `additionalContext` / `agent.inject()` 注入,内容为结构化的自我审查清单:对照最初目标 → 检查最近 5 次调用是否带来新信息 → 用一句话决定继续 / 换方法 / 汇报。

## 斜杠命令

- `/lookup-review` —— 手动触发一次完整的「抬头」自我审查(不依赖任何脚本路径)。
- `/lookup-status` —— 查看插件对当前会话的监控统计与判定。
- `/lookup-config` —— 查看 / 修改阈值配置。
- `/lookup-watch` —— 管理第三通道的独立审查 watcher(启动 / 停止 / 查看状态)。

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

## 双端能力差异(ZCode vs dsh)

两个宿主共用同一套检测逻辑(阈值来自仓库根 `spec.json`,由 `test/golden_check.py` 与
`test/golden.mjs` 双端金标测试钉住),但加载机制与配置面不同,**按宿主区分配置**:

| 能力 | ZCode (Python hook) | DeepSeek Harness (JS bundle) |
|---|---|---|
| 触发检测(重复/失败/零产出/折返) | ✅ | ✅ |
| 本地时钟锚点 | ✅ | ✅ |
| 独立审查者 | 需配 `llm_api_key`(默认关) | 复用会话 `llm` 服务(默认开) |
| 第三通道(独立 watcher) | ✅ 由 `hooks/lookup_watcher.py` 承担(自带 key,需显式开 `deliver_review`) | ✅ 同一套目录协议,钩子侧投递/取回已实现;watcher 进程复用同一脚本 |
| 配置键命名 | `snake_case`(`llm_review`) | `camelCase`(`llmReview`) |
| `llm_api_base` / `llm_model` / `llm_timeout_sec` | ✅ 生效 | ❌ 不读取;审查者模型跟随会话,如需换模型请在会话层设置 |
| 状态持久化 | 落盘 `%TEMP%/zcode-ai-look-up/`(带跨进程锁) | 会话状态在内存,**宿主重启即丢状态**;第三通道的请求/结论仍落同一目录 |

> ⚠️ `llm_api_key` / `llm_api_base` / `llm_model` / `llm_timeout_sec` 只在 ZCode(Python)
> 端被读取;dsh 用户配置它们不会生效也不会报错。

## 行为规格与跨宿主适配

- **`spec.json`** 是双端共享的行为规格(阈值、窗口、冷却、解释器清单等唯一事实源)。
  两端启动时读取它覆盖内建默认值;缺文件时回退内建值。**改行为请改 spec.json,不要分别改两端代码。**
- **`test/golden_cases.json`** 是双端金标用例(命令归一化签名 + 会话回放触发序列),
  `test/golden_check.py`(Python)与 `test/golden.mjs`(JS)对同一份用例各跑一遍,
  CI 里任一端漂移即红。
- **`test/channel_check.py`**(第三通道端到端)与 **`test/revert_verify.py`**(反向验证:
  把每条修复改回缺陷形态,确认对应测试确实变红)是 v0.7 新增的两道自检——后者用来
  证明防线不是装饰性的。
- **`adapters/`** 提供其他钩子型宿主(Claude Code / OpenCode / Gemini CLI)的实验性
  适配 shim——适配层只做事件与字段映射,不复制逻辑,详见 `adapters/README.md`。

## 状态与调试

状态文件位于 `%TEMP%/zcode-ai-look-up/<session_id>.json`(每会话一个)。手动检查:

```bash
python <插件目录>/hooks/lookup_hook.py status
python <插件目录>/hooks/lookup_hook.py reset
```

第三通道的投递区在同一个目录下的 `review/`:`req_*.json` 是待处理请求单,
`res_*.json` 是待取回结论,`watcher.log` 是 watcher 运行日志。手动查看 watcher:

```bash
python <插件目录>/hooks/lookup_watcher.py --status
python <插件目录>/hooks/lookup_watcher.py --stop
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

与这些方案不同,本项目在运行中途(PostToolUse)分析行为节奏,用 `additionalContext` 轻量注入提醒,不打断也不重启;另外增加了按文件名序号归一化的变体检测。

