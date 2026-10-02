---
description: 查看 AI 抬头插件对当前会话的监控统计
---

查看「AI 抬头」插件对当前会话的监控统计。状态文件存放在系统临时目录,按以下步骤获取并汇报:

**第一步:定位状态文件**

运行 Bash 列出状态目录,取**最近修改**的 `*.json`(它最可能属于当前会话):

```bash
ls -t "$TEMP/zcode-ai-look-up/"*.json 2>/dev/null || ls -t "${LOCALAPPDATA}/Temp/zcode-ai-look-up/"*.json 2>/dev/null || ls -t /tmp/zcode-ai-look-up/*.json 2>/dev/null
```

**第二步:读取并汇报**

读取该 JSON 文件,用一张小表向用户汇报:

- 会话运行时长(当前时间 − `started_at`)、工具调用总数 `tool_calls`、按工具的分布 `by_tool`;
- 文件修改次数 `edits`、连续失败 `fail_streak` / 累计失败 `total_failures`;
- 已注入的抬头提醒次数 `reminders`,以及最近一次触发原因 `last_trigger`;
- 最近 5 条归一化命令 `recent_cmds`(用于识别重复空转)。

**第三步:给出判定**

- 长时间无文件修改 + 调用量大 → **空跑嫌疑高**;
- 最近命令高度重复 → **空转试探**;
- 仅失败次数偏高 → **留意**;
- 其余 → **健康**。

如果状态目录不存在或为空:说明本会话还没有工具调用记录,或插件尚未安装生效——如实告知用户,不要编造数据。
