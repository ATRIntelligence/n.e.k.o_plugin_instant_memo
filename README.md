# 速记备忘录 (Instant Memo)

**主要功能**——让猫娘拥有自己的"私密记事本"和"日程表"，全部操作在对话中自然完成：

- **状态/人设备忘录**：AI 隐秘记录短期状态（游戏目标、暗号、临时人设等），
  有效期内作为私密备忘持续生效，过期自动清理，支持分钟级时效或永久；
- **定时提醒与周期计划**：单次 / 每日 / 工作日 / 间隔循环四类排程，
  到点后由猫娘以人设口吻主动开口提醒，循环任务自动顺延；
- **AI 原生工具**：6 个 LLM 工具（记录备忘、删除备忘、设立任务、删除任务、
  快速单次提醒、状态总览），AI 自主管理，用户无需学习任何指令；
- **管理面板**：Hosted UI 面板一屏查看全部备忘与任务（ID、内容、有效期、
  下一次触发），支持逐条删除与一键刷新；
- **数据安全**：备忘与任务持久化在 PluginStore，重启不丢；备忘内容仅注入
  模型上下文，不向用户泄漏。

## 功能

### 1. 状态/人设备忘录

AI 在对话中通过 `set_status_memo` 工具隐秘记录短期状态（游戏目标、暗号、
临时人设等），有效期内作为"私密记事本"生效：

- 有效期支持分钟数或永久（`minutes_later=0/-1`）；
- 条目变化时通过 `push_message(ai_behavior="read")` 把备忘板注入模型上下文，
  附带保密与人设原则（不向用户泄漏备忘内容与确认话术）；
- 过期自动清理；AI 也可通过 `get_memo_overview` 随时核对最新状态。

### 2. 定时提醒与周期计划

`set_scheduled_task` / `set_active_reminder` 排程四类任务：

| task_type | schedule_value | 含义 |
| --- | --- | --- |
| `one_off` | 分钟数（如 `30`） | 单次提醒，触发后删除 |
| `daily` | `HH:MM` | 每天定时 |
| `workday` | `HH:MM` | 工作日（周一至周五）定时 |
| `interval` | 分钟数 | 周期循环，触发后自动顺延 |

到点后由宿主 AI 以人设口吻主动推送提醒（`push_message(ai_behavior="respond")`）。

## 面向 AI 的工具（LLM Tool）

| 工具 | 说明 |
| --- | --- |
| `set_status_memo` | 记录/覆盖状态备忘录 |
| `delete_status_memo` | 删除状态备忘录 |
| `set_scheduled_task` | 设立/覆盖定时任务 |
| `delete_active_task` | 删除定时任务 |
| `set_active_reminder` | 快速单次提醒 |
| `get_memo_overview` | 查询全部条目最新状态 |

工具返回值中带 `[后台隐蔽消息]` 标记，指示模型保持人设、不向用户泄漏
备忘内容、条目 ID 或系统确认话术（沿用原插件约定）。

## 面向用户的入口（Plugin Manager）

`list_items`（列出）、`add_status_memo`（添加备忘）、`add_scheduled_task`
（添加任务）、`delete_item`（删除）、`update_item`（修改属性：备忘
`content`/`expire`，任务 `desc`/`value`/`type`）。

## 管理面板（Hosted UI）

插件详情页内置 Hosted TSX 面板（`ui/panel.tsx`，v0.2.0 起）：

- 状态备忘 / 定时任务两张列表（ID、内容/描述、有效期/下一次触发）；
- 每行删除按钮（带确认）、一键刷新；
- 权限最小化：`state:read` + `action:call`；
- 新建/修改仍通过对话（LLM 工具）或插件入口完成（简化版面板暂不带表单）。

## 配置（plugin.toml 的 `[instant_memo]` 节）

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `check_interval_seconds` | `15` | 后台检查间隔上限（秒） |
| `max_status_memos` | `50` | 状态备忘录数量上限 |
| `max_tasks` | `50` | 定时任务数量上限 |
| `ai_allow_write` | `true` | 允许 AI 新增/修改条目 |
| `ai_allow_delete` | `true` | 允许 AI 删除条目 |

数据持久化在 PluginStore（`[plugin.store]`），键为 `status_memos` / `tasks`。

## Development

This plugin lives at:

```text
N.E.K.O/plugin/plugins/instant_memo
```

From the N.E.K.O repository root:

```bash
uv run neko-plugin check instant_memo
uv run neko-plugin check instant_memo --release
```

Plugin-local tests (run from the repository root with the project uv environment):

```bash
uv run python -m pytest plugin/plugins/instant_memo/tests -q
```

Python runtime dependencies are declared in `pyproject.toml` and synced into
`vendor/` for packaging. This plugin currently has no external dependencies.

## Entry

```toml
entry = "plugin.plugins.instant_memo:InstantMemoPlugin"
```
