---
description: 查看/修改 AI 抬头插件的阈值配置
argument-hint: "[要调整的参数=新值, 或留空查看]"
---

管理「AI 抬头」插件的阈值配置。$ARGUMENTS

**配置文件位置(按优先级从低到高):**

1. 内置默认值(见下方参数表)
2. `~/.zcode/ai-look-up.json` —— 用户全局配置
3. `<项目>/.zcode/ai-look-up.json` —— 项目级配置
4. 环境变量 `LOOKUP_<参数名大写>`(如 `LOOKUP_COOLDOWN_SEC=60`)

**可调参数:**

| 参数 | 默认值 | 说明 |
|---|---|---|
| `enabled` | `true` | 总开关 |
| `call_nudge_interval` | `30` | 连续 N 次调用且零修改 → 提醒 |
| `repeat_window` | `5` | 重复检测的窗口大小(最近 N 条命令) |
| `repeat_cmd_count` | `3` | 窗口内 N 条相似命令 → 判定空转 |
| `repeat_min_calls` | `8` | 重复检测要求的最少调用数 |
| `fail_streak_threshold` | `3` | 连续失败 N 次 → 提醒 |
| `fail_cooldown_sec` | `180` | 失败提醒的冷却(秒) |
| `long_run_minutes` | `25` | 会话持续 N 分钟且零修改 → 提醒 |
| `long_run_min_calls` | `15` | 超长提醒要求的最少调用数 |
| `cooldown_sec` | `300` | 普通提醒冷却(秒) |
| `max_reminders` | `12` | 单会话最多提醒次数 |
| `stop_check` | `true` | 结束时若长期零产出,请求一次继续以复盘 |
| `stop_min_calls` | `40` | 结束审查要求的最低调用数 |
| `clock_tick` | `true` | 定期注入真实本地时间,校准 AI 时间感 |
| `clock_tick_minutes` | `5` | 首次时钟锚点的间隔(分钟),之后倍增 |
| `clock_tick_backoff` | `true` | 时钟间隔倍增(5→10→20→40),新回合重置 |
| `clock_tick_max_minutes` | `60` | 时钟间隔封顶(分钟) |
| `edit_foldback` | `true` | 文件内容改了又改回 → 折返提醒 |
| `foldback_window` | `5` | 每文件记录的内容指纹个数 |
| `adaptive` | `true` | 按提醒历史有效率自适应冷却 |
| `drift_check` | `true` | 目标漂移巡检(需审查者可用) |
| `drift_check_calls` | `30` | 每 N 次调用巡检一次 |
| `drift_cooldown_sec` | `900` | 巡检冷却(秒) |
| `lessons` | `true` | 教训登记簿:失败登记坑位,回合开始注入 Top 3 |
| `llm_review` | `false` | 触发提醒时用独立 LLM 上下文审查是否空跑 |
| `llm_api_base` | `https://api.deepseek.com` | OpenAI 兼容接口地址 |
| `llm_api_key` | 空 | 接口密钥,填写后审查才生效 |
| `llm_model` | `deepseek-chat` | 审查用模型 |
| `llm_timeout_sec` | `12` | 审查调用超时(秒),超时回退静态清单 |
| `review_log_size` | `12` | 审查材料携带的最近调用条数 |

**用户指令处理:**

- 若用户给出了 `参数=值` 形式的调整:把给出的键值对合并写入项目级配置 `<项目>/.zcode/ai-look-up.json`(保留已有键),然后向用户确认生效的参数与值。数值参数用数字,开关用 `true`/`false`。
- 若用户未给参数:展示上面的参数表,并提示当前项目级配置文件是否存在、内容是什么(若存在则读取展示)。

配置在下次会话事件发生时即生效,无需重装插件。
