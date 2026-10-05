---
description: 管理第三通道的独立审查 watcher(启动 / 停止 / 查看状态)
---

管理「AI 抬头」第三通道的**独立审查 watcher**。watcher 是宿主外面的常驻进程,
从投递区读请求单、用自己的 API key 调审查者、把结论写回,钩子在下一次事件捎带注入。

按用户意图执行以下之一(`${CLAUDE_PLUGIN_ROOT}` 为插件根目录):

**查看状态**(默认)

```bash
python "${CLAUDE_PLUGIN_ROOT}/hooks/lookup_watcher.py" --status
```

汇报:锁是否被占用、待处理请求单数、各状态文件数、watcher.log 末几行。

**启动**(后台常驻)

```bash
python "${CLAUDE_PLUGIN_ROOT}/hooks/lookup_watcher.py" &
```

单例锁保证只有一个实例;若已在跑,会提示"已有 watcher 在运行"而不重复启动。
启动前提醒用户:第三通道会**消耗用户自己的 API 额度**,请确认 `deliver_review` 与
`llm_api_key` 已在配置中显式开启。

**停止**

```bash
python "${CLAUDE_PLUGIN_ROOT}/hooks/lookup_watcher.py" --stop
```

**单次处理**(调试用,跑完即退)

```bash
python "${CLAUDE_PLUGIN_ROOT}/hooks/lookup_watcher.py" --once
```

注意事项:

- watcher 与钩子**共用 state_dir**(默认 `%TEMP%/zcode-ai-look-up/`,可用 `LOOKUP_STATE_DIR` 覆盖);
- 两个平台(ZCode 钩子 / dsh 宿主)落的是**同一套目录协议**,见 `hooks/REVIEW_CHANNEL.md`;
- 若 `--status` 显示有锁但进程已死,watcher 下次启动会自动接管失效锁(Windows 下用 `OpenProcess` 探活)。
