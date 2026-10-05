# 第三通道：投递请求 + 独立 watcher（v0.7）

本文档是**协议契约**。实现分三部分：钩子端投递、watcher 端处理、钩子端取回。
改协议先改这里。

## 为什么需要它

ZCode 的钩子是**宿主外面的裸 Python 进程**，够不到会话的 `llm` 凭证。所以"钩子自己调
独立审查者"这条路必须自带 key。自带 key 解决的是凭证，但**不解决时机**：

- 钩子是同步阻塞的，LLM 往返最长 12 秒会拖住下一次工具调用；
- 如果审查在钩子里调，主 agent 的下一步就卡着。

第三通道把这两件事拆开：**钩子只写一张"请求单"就立刻返回；另一个常驻进程慢慢去审。**

## 独立性为什么最强

| 环节 | 谁做 | 主 agent 能干预吗 |
|---|---|---|
| 决定要不要审 | 钩子（按升级阶梯） | 不能 |
| 写请求单 | 钩子 | 不能 |
| 读材料 | watcher（从磁盘） | 不能 |
| 调 LLM | watcher（自有 key） | 不能 |
| 写结论 | watcher | 不能 |
| 注入结论 | 钩子（下一次事件捎带） | 不能 |

**主 agent 全程不在链路上。**它既不知道审查发生过，也没有任何环节可以改写提示词、
不发起、或丢弃结果。这是它和"②子代理审查"的本质区别——后者至少有一个环节是
主 agent 按的按钮。

## 目录布局

全部落在 `state_dir()`（默认 `%TEMP%/zcode-ai-look-up/`）之下：

```
<state_dir>/
  review/                      投递区（第三通道专用）
    req_<request_id>.json      请求单 + 材料（钩子写，watcher 读后删）
    res_<request_id>.json      结论（watcher 写，钩子读后删）
    claimed_<request_id>.json  watcher 领取标记（防重复处理）
    watcher.lock               watcher 单例锁
    watcher.log                watcher 日志（追加）
```

`request_id` = `"<session_id>-<tool_calls>-<kind>"`，**确定性**——同一场景重复投递天然去重。

## 请求单 schema（`req_<id>.json`）

```jsonc
{
  "v": 1,
  "request_id": "sess-42-long-run",
  "session_id": "sess",
  "kind": "long-run",              // 触发类别
  "detail": "运行 26 分钟零修改",    // 人类可读的触发描述
  "created_at": 1791221136.9,      // 墙上时间戳
  "tool_calls": 42,                // 投递时的调用数（用于取回时算「几步前」）
  "prompt_resets": 1,              // 用户换指令次数（取回时校验，见下）
  "goal_snapshot": "……",          // 投递时的任务快照
  "system": "你是 AI 编码代理的独立行为审查员。……",   // 固定模板，钩子从常量注入
  "material": "【审查材料】……"      // 由 state 构造，主 agent 无法触碰
}
```

**`system` 与 `material` 都由钩子生成，`material` 由 `build_review_material()` 从
`state` 拼出。**这是"固定模板"那一层的落实——主 agent 不在场，天然无法软化。

## 结论 schema（`res_<id>.json`）

```jsonc
{
  "v": 1,
  "request_id": "sess-42-long-run",
  "session_id": "sess",
  "kind": "long-run",
  "verdict": "drifting",           // on-track | drifting | stuck | error
  "reason": "一句话依据",
  "suggestion": "一句话建议",
  "finished_at": 1791221140.1,
  "elapsed_ms": 3200,
  "model": "deepseek-chat",
  "tool_calls": 42,                // 从请求单回填，供取回时比对
  "prompt_resets": 1
}
```

`verdict: "error"` 是合法的——watcher 调用失败时也要写一份，让钩子知道"这次没结果，
别等了"，而不是无限期占着等待队列。

## 取回（钩子端）的校验规则

钩子在**每次事件**（post-use / post-fail / prompt / stop）开头扫一次投递区，
有 `res_*.json` 就取回。取回**不是无脑注入**，要过三道闸：

1. **场景指纹校验**：`res.prompt_resets != state.prompt_resets` → **作废**。
   理由：用户中途换了目标，结论是对着旧目标下的，注入会误导（见设计征询回信 Q2）。
2. **过期校验**：`now - res.finished_at > result_ttl_sec`（默认 900s）→ 作废。
   理由：太老的结论没有现场感。
3. **步数校验**：`state.tool_calls - res.tool_calls > result_settle_calls`（默认 60）→ 作废。
   理由：结论对应的是几十步前的现场，AI 已经走远了。

三道闸任一条不过，**只删文件不注入**（静默作废）。

过闸的结论注入时**必须标注场景**，不能只说"N 步前"：

```
🎯 【AI 抬头 · 独立审查者】判定: 有偏航迹象
该结论基于 3 步前「运行 26 分钟零修改」的现场。
漂移表现: ……
收束建议: ……
```

## 投递（钩子端）的触发条件

只在**升级阶梯末档**投递：`state["reminders"] >= escalate_after_reminders`（默认 3）。

理由：前几次交给静态清单就够；只有"提醒了还不改"才值得动用自己的 API 额度。
这也让成本可控——绝大多数会话根本到不了这一档。

**投递前置条件**（缺一不投）：
- `cfg["deliver_review"]` 为真（用户显式开启，默认关）；
- `cfg["llm_api_key"]` 非空（watcher 要用）；
- 投递区不存在同 `request_id` 的未处理请求（幂等）。

## watcher 的领取与幂等

watcher 轮询间隔 `request_poll_sec`（默认 2s）。处理流程：

1. 扫 `req_*.json`，按 `created_at` 升序；
2. 若 `claimed_<id>.json` 存在 → 跳过（已在处理或已处理）；
3. 原子创建 `claimed_<id>.json`（`O_CREAT|O_EXCL`）→ 抢到才处理，抢不到说明别的
   watcher 实例拿了（虽然单例锁已防，双保险）；
4. 读请求 → 调 LLM → 写 `res_<id>.json` → **删请求单**；
5. 请求单 `created_at` 超过 `request_ttl_sec` 仍未处理 → 写一份 `verdict: "error"`
   的结论再删请求单（不静默丢弃，让钩子能收尾）。

**单例**：`watcher.lock` 用 `O_CREAT|O_EXCL` + PID 写入。启动时若锁存在且 PID 已死，
接管（删锁重建）；若 PID 活着，报错退出，不启动第二个。

## 已知边界（如实声明）

- **自带 key = 花用户的钱**。所以默认关，且只在末档触发。
- **watcher 不感知会话终止**。会话结束后投递区可能滞留请求单——靠 TTL 清理。
- **不处理 LLM 返回格式错误**。解析失败按 `verdict: "error"` 处理，不回退重试
  （重试的收益低于多花的钱）。
- **Windows 上 PID 存活检测**用 `OpenProcess`；POSIX 用 `os.kill(pid, 0)`。
