"""速记备忘录（Instant Memo）— 从 AstrBot `astrbot_plugin_instant_memo` 迁移。

两个核心能力：

1. **状态/人设备忘录**：AI 通过 ``set_status_memo`` 工具隐秘记录短期状态，
   有效期内经 ``push_message(ai_behavior="read")`` 注入模型上下文，过期自动清理。
2. **定时提醒与周期计划**：one_off / daily / workday / interval 四类排程，
   到点后经 ``push_message(ai_behavior="respond")`` 让宿主 AI 以人设口吻主动提醒。

已按 N.E.K.O 场景裁剪：群聊作用域、群白/黑名单、关键词搭话、XML 触发模式、
自建 LLM 生成管线、Web 管理页。详见 DESIGN.md。
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from plugin.sdk.plugin import (
    Err,
    NekoPluginBase,
    Ok,
    PluginSettings,
    SdkError,
    SettingsField,
    lifecycle,
    llm_tool,
    neko_plugin,
    plugin_entry,
    tr,
    ui,
)

from .core import (
    PERMANENT_EXPIRE,
    TASK_TYPES,
    build_memo_board,
    compute_trigger_timestamp,
    expiry_label,
    find_by_short_id,
    format_memo_line,
    format_task_line,
    normalize_schedule_value,
    prune_expired_memos,
    reschedule_task,
    resolve_expiry,
    task_type_label,
)

_MEMOS_KEY = "status_memos"
_TASKS_KEY = "tasks"

_DEFAULT_SETTINGS: Dict[str, Any] = {
    "check_interval_seconds": 15,
    "max_status_memos": 50,
    "max_tasks": 50,
    "ai_allow_write": True,
    "ai_allow_delete": True,
}

# 工具返回值里的隐蔽提示（沿用原插件“后台隐蔽消息”约定）：
# 工具结果只回传给模型，指示其保持人设、不向用户泄漏备忘内容或系统确认话术。
_NOTE_SECRET = (
    "[后台隐蔽消息] 请【严格隐秘存储】，完全以人设身份正常回复用户，"
    "【严禁】在回复中向用户暴露备忘内容、条目ID或输出系统确认话术！"
)
_NOTE_TASK = (
    "[后台隐蔽消息] 调用成功仅表示排期完成，到点后系统会自动提醒。"
    "请完全以人设身份正常回复用户，【严禁】在回复中暴露任务内容或系统确认话术！"
)

# 注意：@llm_tool 的 description/parameters 会经 to_ipc_payload **原样**注册到
# main_server 并进入模型的 function schema，provider 要求其中的值必须是纯字符串，
# 放 tr() 延迟翻译对象（{"$i18n": ...}）会触发 400 invalid_request_error。
# 因此工具面一律用纯文本（与 game_agent_minecraft 等先例一致）；
# @plugin_entry 的 name/description/input_schema 走 query_service.resolve_i18n_refs，
# 那一面保留 tr() 多语言。
_MINUTES_PROP = {
    "type": "number",
    "description": "有效时长（分钟）。填 0 或 -1 表示永久有效。默认 60。",
}

_MEMO_ID_PROP = {
    "type": "string",
    "description": "已有备忘录 ID（前 8 位亦可），传入则覆盖更新而非新建。",
}

_TASK_ID_PROP = {
    "type": "string",
    "description": "已有任务 ID（前 8 位亦可），传入则覆盖更新而非新建。",
}


@neko_plugin
class InstantMemoPlugin(NekoPluginBase):
    """速记备忘录：AI 自我备忘录与主动定时提醒。"""

    class Settings(PluginSettings):
        """业务配置（plugin.toml 的 [instant_memo] 节）— hot 字段会出现在设置面板。"""

        model_config = {"toml_section": "instant_memo"}

        check_interval_seconds: int = SettingsField(
            15, hot=True, ge=5, le=600, description="后台检查间隔（秒）"
        )
        max_status_memos: int = SettingsField(
            50, hot=True, ge=1, le=500, description="状态备忘录数量上限"
        )
        max_tasks: int = SettingsField(
            50, hot=True, ge=1, le=500, description="定时任务数量上限"
        )
        ai_allow_write: bool = SettingsField(
            True, hot=True, description="允许 AI 新增/修改条目"
        )
        ai_allow_delete: bool = SettingsField(
            True, hot=True, description="允许 AI 删除条目"
        )

    def __init__(self, ctx: Any):
        super().__init__(ctx)
        self.logger = ctx.logger
        self._settings: Dict[str, Any] = dict(_DEFAULT_SETTINGS)
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._checker: Optional[threading.Thread] = None

    # ── 配置 ──

    async def _reload_config(self) -> None:
        cfg = await self.config.dump(timeout=5.0)
        cfg = cfg if isinstance(cfg, dict) else {}
        section = cfg.get("instant_memo")
        section = section if isinstance(section, dict) else {}
        merged = dict(_DEFAULT_SETTINGS)
        for key, default in _DEFAULT_SETTINGS.items():
            value = section.get(key, default)
            try:
                if isinstance(default, bool):
                    merged[key] = bool(value)
                elif isinstance(default, int):
                    merged[key] = int(value)
                else:
                    merged[key] = value
            except (TypeError, ValueError):
                merged[key] = default
        self._settings = merged

    def _setting(self, key: str) -> Any:
        return self._settings.get(key, _DEFAULT_SETTINGS[key])

    # ── Store 访问（同步 helper + threading.Lock；async 侧经 asyncio.to_thread 复用）──
    # 备忘/任务的读改写必须原子，故统一走锁内同步 helper；
    # PluginStore 的同步实现与官方 async API（asyncio.to_thread 包装）同源。
    # 参考 plugin/plugins/memo_reminder 的既有模式。

    def _store_get_sync(self, key: str, default: Any) -> Any:
        if not self.store.enabled:
            raise SdkError("PluginStore 未启用，无法读取备忘数据")
        try:
            return self.store._read_value(key, default)
        except Exception as exc:
            raise SdkError(f"读取备忘数据失败: {exc}") from exc

    def _store_set_sync(self, key: str, value: Any) -> None:
        if not self.store.enabled:
            raise SdkError("PluginStore 未启用，无法保存备忘数据")
        try:
            self.store._write_value(key, value)
        except Exception as exc:
            raise SdkError(f"保存备忘数据失败: {exc}") from exc

    def _load_state_unlocked(self) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        memos = self._store_get_sync(_MEMOS_KEY, {})
        tasks = self._store_get_sync(_TASKS_KEY, {})
        return (
            dict(memos) if isinstance(memos, dict) else {},
            dict(tasks) if isinstance(tasks, dict) else {},
        )

    def _save_memos_unlocked(self, memos: Dict[str, Any]) -> None:
        self._store_set_sync(_MEMOS_KEY, memos)

    def _save_tasks_unlocked(self, tasks: Dict[str, Any]) -> None:
        self._store_set_sync(_TASKS_KEY, tasks)

    # ── 业务核心（同步、锁内；返回 (结果dict 或 None, 错误str 或 None)）──

    def _add_or_update_memo_sync(
        self, content: str, minutes_later: Any, memo_id: str, now: float
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        content = (content or "").strip()
        if not content:
            return None, "content 不能为空。"
        expire_ts, dur_info = resolve_expiry(minutes_later, now)

        with self._lock:
            memos, _ = self._load_state_unlocked()
            target_id = find_by_short_id(memos, memo_id) if memo_id else None
            is_update = target_id is not None
            if not is_update:
                if len(memos) >= int(self._setting("max_status_memos")):
                    return None, f"状态备忘录数量已达上限 ({self._setting('max_status_memos')})"
                target_id = uuid.uuid4().hex

            memos[target_id] = {
                "content": content,
                "expire_timestamp": expire_ts,
                "updated_at": now,
                "created_at": memos.get(target_id, {}).get("created_at", now),
            }
            self._save_memos_unlocked(memos)

        return {
            "id": target_id,
            "is_update": is_update,
            "validity": dur_info,
            "line": format_memo_line(target_id, memos[target_id], now),
        }, None

    def _delete_memo_sync(self, memo_id: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        with self._lock:
            memos, _ = self._load_state_unlocked()
            real_id = find_by_short_id(memos, memo_id)
            if real_id is None:
                return None, f"未找到 ID 为 '{memo_id}' 的状态备忘录。"
            memo = memos.pop(real_id)
            self._save_memos_unlocked(memos)
        return {"id": real_id, "content": memo.get("content", "")}, None

    def _add_or_update_task_sync(
        self,
        task_description: str,
        task_type: str,
        schedule_value: str,
        task_id: str,
        now: float,
        lanlan_name: Optional[str],
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        description = (task_description or "").strip()
        if not description:
            return None, "task_description 不能为空。"
        normalized, err = normalize_schedule_value(task_type, schedule_value)
        if err:
            return None, err
        t_type = (task_type or "").strip().lower()
        trigger_ts = compute_trigger_timestamp(t_type, normalized, now)

        with self._lock:
            _, tasks = self._load_state_unlocked()
            target_id = find_by_short_id(tasks, task_id) if task_id else None
            is_update = target_id is not None
            if not is_update:
                if len(tasks) >= int(self._setting("max_tasks")):
                    return None, f"定时任务数量已达上限 ({self._setting('max_tasks')})"
                target_id = uuid.uuid4().hex

            existing = tasks.get(target_id, {})
            tasks[target_id] = {
                "type": t_type,
                "task_description": description,
                "scheduled_time": normalized,
                "trigger_timestamp": trigger_ts,
                "created_at": existing.get("created_at", now),
                "last_run_timestamp": existing.get("last_run_timestamp", 0.0),
                "lanlan_name": lanlan_name or existing.get("lanlan_name"),
            }
            self._save_tasks_unlocked(tasks)

        return {
            "id": target_id,
            "is_update": is_update,
            "type": t_type,
            "trigger_timestamp": trigger_ts,
            "line": format_task_line(target_id, tasks[target_id], now),
        }, None

    def _delete_task_sync(self, task_id: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        with self._lock:
            _, tasks = self._load_state_unlocked()
            real_id = find_by_short_id(tasks, task_id)
            if real_id is None:
                return None, f"未找到 ID 为 '{task_id}' 的定时任务。"
            task = tasks.pop(real_id)
            self._save_tasks_unlocked(tasks)
        return {"id": real_id, "task_description": task.get("task_description", "")}, None

    def _update_item_sync(
        self, item_id: str, field: str, value: str, now: float
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        field = (field or "").strip().lower()
        value = (value or "").strip()

        with self._lock:
            memos, tasks = self._load_state_unlocked()

            memo_id = find_by_short_id(memos, item_id)
            if memo_id is not None:
                memo = memos[memo_id]
                if field in ("content", "内容"):
                    if not value:
                        return None, "content 不能为空。"
                    memo["content"] = value
                elif field in ("expire", "time", "时间", "过期时间"):
                    expire_ts, dur_info = resolve_expiry(value, now)
                    if dur_info != "永久有效":
                        try:
                            mins = float(value)
                        except ValueError:
                            return None, "时间参数必须是整数分钟数，或填 '永久'。"
                        if mins <= 0:
                            return None, "时间参数必须是正整数分钟数，或填 '永久'。"
                    memo["expire_timestamp"] = expire_ts
                else:
                    return None, "状态备忘录支持修改的属性: content, expire"
                memo["updated_at"] = now
                self._save_memos_unlocked(memos)
                return {
                    "id": memo_id,
                    "kind": "status_memo",
                    "line": format_memo_line(memo_id, memo, now),
                }, None

            task_id_real = find_by_short_id(tasks, item_id)
            if task_id_real is not None:
                task = tasks[task_id_real]
                t_type = str(task.get("type", "one_off"))
                if field in ("desc", "description", "描述", "内容"):
                    if not value:
                        return None, "task_description 不能为空。"
                    task["task_description"] = value
                elif field in ("value", "time", "时间"):
                    normalized, err = normalize_schedule_value(t_type, value)
                    if err:
                        return None, err
                    task["scheduled_time"] = normalized
                    task["trigger_timestamp"] = compute_trigger_timestamp(t_type, normalized, now)
                elif field in ("type", "类型"):
                    new_type = value.lower().strip()
                    if new_type not in TASK_TYPES:
                        return None, f"类型必须是 {'/'.join(TASK_TYPES)} 之一。"
                    normalized, err = normalize_schedule_value(new_type, task.get("scheduled_time", ""))
                    if err:
                        task["type"] = new_type
                        self._save_tasks_unlocked(tasks)
                        return {
                            "id": task_id_real,
                            "kind": "task",
                            "line": format_task_line(task_id_real, task, now),
                            "warning": f"类型已修改为 '{new_type}'，但现有时间值不兼容，请继续修改 value 属性。",
                        }, None
                    task["type"] = new_type
                    task["scheduled_time"] = normalized
                    task["trigger_timestamp"] = compute_trigger_timestamp(new_type, normalized, now)
                else:
                    return None, "定时任务支持修改的属性: desc, value, type"
                self._save_tasks_unlocked(tasks)
                return {
                    "id": task_id_real,
                    "kind": "task",
                    "line": format_task_line(task_id_real, task, now),
                }, None

        return None, f"未找到序号/ID 为 '{item_id}' 的备忘录或定时任务。"

    def _overview_sync(self, now: float) -> Dict[str, Any]:
        with self._lock:
            memos, tasks = self._load_state_unlocked()
            memos, expired = prune_expired_memos(memos, now)
            if expired:
                self._save_memos_unlocked(memos)
        return {
            "memos": [
                {"id": mid, "line": format_memo_line(mid, memo, now), "raw": memo}
                for mid, memo in sorted(memos.items(), key=lambda kv: float(kv[1].get("expire_timestamp", 0) or 0))
            ],
            "tasks": [
                {"id": tid, "line": format_task_line(tid, task, now), "raw": task}
                for tid, task in sorted(tasks.items(), key=lambda kv: float(kv[1].get("trigger_timestamp", 0) or 0))
            ],
            "expired_pruned": len(expired),
        }

    # ── 消息推送 ──

    def _push_board(self) -> None:
        """把备忘板注入模型上下文（不打断、不直接展示给用户）。"""
        now = time.time()
        with self._lock:
            memos, tasks = self._load_state_unlocked()
        text = build_memo_board(memos, tasks, now)
        if not text:
            return
        result = self.push_message(
            source="instant_memo",
            visibility=[],
            ai_behavior="read",
            parts=[{"type": "text", "text": text}],
            priority=5,
            metadata={"event_type": "memo_board"},
        )
        if not result.get("submitted"):
            self.logger.warning("memo board submission failed: {}", result.get("reason"))

    def _push_board_if_any(self) -> None:
        now = time.time()
        with self._lock:
            memos, tasks = self._load_state_unlocked()
        if build_memo_board(memos, tasks, now):
            self._push_board()

    def _push_reminder(self, task_id: str, task: Dict[str, Any]) -> None:
        """到点提醒：交给宿主 AI 以人设口吻生成并说出自然的提醒。"""
        description = task.get("task_description", "")
        cue = (
            f"【主动台词时刻】现在轮到你主动开口。你要对 {{MASTER_NAME}} 说的主题是：{description}\n"
            "请把这句提醒当作你自己的台词直接说出来（完整、自然、符合人设，叫起床就真的叫起床）。\n"
            "⚠️注意：这是你的台词输出，不属于『对话外的实际操作』——"
            "系统提示中『执行实际操作只能简短说明会尝试处理』的条款不适用于本时刻，"
            "绝不能只回一句『我试试』『好哒』之类的短句应付！\n"
            "同时严禁提及系统机制、任务ID或后台设置过程。"
        )
        kwargs: Dict[str, Any] = dict(
            source="instant_memo",
            visibility=[],
            ai_behavior="respond",
            parts=[{"type": "text", "text": cue}],
            priority=8,
            metadata={
                "task_id": task_id,
                "event_type": "task_fired",
                "task_type": task.get("type", "one_off"),
            },
        )
        if task.get("lanlan_name"):
            kwargs["target_lanlan"] = task["lanlan_name"]
        result = self.push_message(**kwargs)
        if result.get("submitted"):
            self.logger.info("task reminder submitted: id={}", task_id[:8])
        else:
            self.logger.warning(
                "task reminder submission failed: id={} reason={}", task_id[:8], result.get("reason")
            )

    # ── 后台检查线程（事件驱动：睡到最近触发点，配置间隔封顶）──

    def _checker_loop(self) -> None:
        self.logger.info("InstantMemo checker thread started")
        while not self._stop_event.is_set():
            try:
                self._tick()
                wait_sec = self._next_wake_seconds()
            except Exception:
                self.logger.exception("InstantMemo checker tick failed, retry in 5s")
                wait_sec = 5.0

            self._wake_event.clear()
            if self._stop_event.is_set():
                break
            self._wake_event.wait(timeout=wait_sec)
        self.logger.info("InstantMemo checker thread exiting")

    def _tick(self) -> None:
        now = time.time()
        board_dirty = False
        fired: List[Tuple[str, Dict[str, Any]]] = []

        with self._lock:
            memos, tasks = self._load_state_unlocked()
            memos, expired = prune_expired_memos(memos, now)
            if expired:
                self._save_memos_unlocked(memos)
                board_dirty = True

            due_ids = [
                tid for tid, task in tasks.items()
                if float(task.get("trigger_timestamp", 0) or 0) <= now
            ]
            for tid in due_ids:
                task = tasks.pop(tid)
                fired.append((tid, task))
                rescheduled = reschedule_task(task, now)
                if rescheduled is not None:
                    tasks[tid] = rescheduled
                self.logger.info(
                    "task fired: id={} type={} delay_s={}",
                    tid[:8],
                    task.get("type", "one_off"),
                    int(now - float(task.get("trigger_timestamp", 0) or 0)),
                )
            if due_ids:
                self._save_tasks_unlocked(tasks)

        for tid, task in fired:
            try:
                self._push_reminder(tid, task)
            except Exception:
                self.logger.exception("task reminder push failed: {}", tid[:8])

        if board_dirty:
            self._push_board()

    def _next_wake_seconds(self) -> float:
        now = time.time()
        cap = max(float(self._setting("check_interval_seconds")), 5.0)
        earliest: Optional[float] = cap
        with self._lock:
            memos, tasks = self._load_state_unlocked()
        for memo in memos.values():
            exp = float(memo.get("expire_timestamp", PERMANENT_EXPIRE) or PERMANENT_EXPIRE)
            if exp > 0:
                delta = exp - now
                earliest = min(earliest, delta) if earliest is not None else delta
        for task in tasks.values():
            trigger = float(task.get("trigger_timestamp", 0) or 0)
            if trigger > 0:
                earliest = min(earliest, trigger - now) if earliest is not None else trigger - now
        return max(float(earliest or cap), 0.5)

    def _notify_change(self) -> None:
        """条目变更后唤醒检查线程；线程异常退出时自动重启。"""
        alive = self._checker.is_alive() if self._checker else False
        if self._checker and not alive and not self._stop_event.is_set():
            self.logger.warning("InstantMemo checker thread found dead, restarting")
            self._checker = threading.Thread(
                target=self._checker_loop, daemon=True, name="instant-memo-checker"
            )
            self._checker.start()
        self._wake_event.set()

    # ── 生命周期 ──

    @lifecycle(id="startup")
    async def startup(self, **_):
        await self._reload_config()
        if not self.store.enabled:
            self.store.enabled = True
            self.logger.warning("PluginStore was disabled at init; force-enabled for memo persistence")

        self._stop_event.clear()
        self._wake_event.clear()
        self._checker = threading.Thread(
            target=self._checker_loop, daemon=True, name="instant-memo-checker"
        )
        self._checker.start()

        overview = await asyncio.to_thread(self._overview_sync, time.time())
        await asyncio.to_thread(self._push_board_if_any)
        self.logger.info(
            "InstantMemo started: {} memos, {} tasks, check_interval={}s",
            len(overview["memos"]), len(overview["tasks"]), self._setting("check_interval_seconds"),
        )
        return Ok({
            "status": "ready",
            "memos": len(overview["memos"]),
            "tasks": len(overview["tasks"]),
        })

    @lifecycle(id="shutdown")
    async def shutdown(self, **_):
        self._stop_event.set()
        self._wake_event.set()
        if self._checker and self._checker.is_alive():
            self._checker.join(timeout=3.0)
        self.logger.info("InstantMemo stopped")
        return Ok({"status": "stopped"})

    @lifecycle(id="config_change")
    async def on_config_change(self, **_):
        await self._reload_config()
        self._notify_change()
        return Ok({"status": "config_updated"})

    # ── LLM 工具（对话期调用，返回值仅回传给模型）──

    @llm_tool(
        name="set_status_memo",
        description=(
            "记录/修改 AI 自我状态/人设备忘录，有效期内会作为私密备忘持续生效。"
            "⚠️重要：本工具是【对话内工具】，调用工具本身就是执行方式——"
            "系统提示中『执行实际操作只能简短说明会尝试处理』的条款不适用于本工具，"
            "绝不能只口头说『我试试』而不调用。"
            "当用户要求记住事情、保存备忘、记录游戏状态/暗号时，必须在本轮立即调用本工具完成。"
            "调用成功后请保持人设自然回复，严禁向用户暴露备忘内容、条目ID或输出系统确认话术。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "状态或备忘内容。",
                },
                "minutes_later": _MINUTES_PROP,
                "memo_id": _MEMO_ID_PROP,
            },
            "required": ["content"],
        },
    )
    async def set_status_memo(
        self, *, content: Any = None, minutes_later: Any = 60, memo_id: Any = "", **_
    ):
        if not await asyncio.to_thread(self._guard_write):
            return {
                "status": "denied",
                "note": "[后台隐蔽消息] 新增/修改状态备忘的操作已被禁用。请保持人设回复用户。",
            }
        now = time.time()
        result, err = await asyncio.to_thread(
            self._add_or_update_memo_sync, str(content or ""), minutes_later, str(memo_id or ""), now
        )
        if err:
            return {"status": "error", "note": f"[后台隐蔽消息] {err}"}
        await asyncio.to_thread(self._push_board)
        action = "修改更新" if result["is_update"] else "记录"
        return {
            "status": "ok",
            "memo_id": result["id"][:8],
            "validity": result["validity"],
            "note": f"[后台隐蔽消息] 已成功{action}状态备忘录(ID: {result['id'][:8]}，{result['validity']})。{_NOTE_SECRET}",
        }

    @llm_tool(
        name="delete_status_memo",
        description="删除指定的状态备忘录。",
        parameters={
            "type": "object",
            "properties": {
                "memo_id": {
                    "type": "string",
                    "description": "要删除的状态备忘录 ID 或前 8 位短 ID。",
                },
            },
            "required": ["memo_id"],
        },
    )
    async def delete_status_memo(self, *, memo_id: Any = "", **_):
        if not await asyncio.to_thread(self._guard_delete):
            return {
                "status": "denied",
                "note": "[后台隐蔽消息] 删除状态备忘的操作已被禁用。请保持人设回复用户。",
            }
        result, err = await asyncio.to_thread(self._delete_memo_sync, str(memo_id or ""))
        if err:
            return {"status": "error", "note": f"[后台隐蔽消息] {err}"}
        await asyncio.to_thread(self._push_board)
        return {
            "status": "ok",
            "memo_id": result["id"][:8],
            "note": f"[后台隐蔽消息] 已成功删除状态备忘录(ID: {result['id'][:8]})。请保持人设回复用户。",
        }

    @llm_tool(
        name="set_scheduled_task",
        description=(
            "设立/修改定时提醒或循环任务（到点后由系统主动提醒主人）。"
            "⚠️重要：本工具是【对话内工具】，调用工具本身就是执行方式——"
            "系统提示中『执行实际操作只能简短说明会尝试处理』的条款不适用于本工具，"
            "绝不能只口头说『我试试』而不调用。"
            "当用户要求定时提醒、闹钟、每日/周期计划时，必须在本轮立即调用本工具（或 set_active_reminder）完成。"
            "调用成功仅表示排期完成，请勿立即提醒用户，也严禁暴露任务内容或系统确认话术。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "task_description": {
                    "type": "string",
                    "description": "任务描述/提醒内容。",
                },
                "task_type": {
                    "type": "string",
                    "enum": list(TASK_TYPES),
                    "description": "'one_off'(单次)|'daily'(每日)|'workday'(工作日)|'interval'(周期循环)。",
                },
                "schedule_value": {
                    "type": "string",
                    "description": "one_off/interval 填分钟数（如 '30'）；daily/workday 填 HH:MM（如 '12:30'）。",
                },
                "task_id": _TASK_ID_PROP,
            },
            "required": ["task_description", "task_type", "schedule_value"],
        },
    )
    async def set_scheduled_task(
        self,
        *,
        task_description: Any = None,
        task_type: Any = "one_off",
        schedule_value: Any = "",
        task_id: Any = "",
        **kwargs,
    ):
        if not await asyncio.to_thread(self._guard_write):
            return {
                "status": "denied",
                "note": "[后台隐蔽消息] 创建定时提醒任务的操作已被禁用。请保持人设回复用户。",
            }
        now = time.time()
        result, err = await asyncio.to_thread(
            self._add_or_update_task_sync,
            str(task_description or ""),
            str(task_type or "one_off"),
            str(schedule_value or ""),
            str(task_id or ""),
            now,
            self._extract_lanlan(kwargs),
        )
        if err:
            return {"status": "error", "note": f"[后台隐蔽消息] {err}"}
        self._notify_change()
        time_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(result["trigger_timestamp"]))
        action = "修改更新" if result["is_update"] else "设立"
        return {
            "status": "ok",
            "task_id": result["id"][:8],
            "trigger_at": time_str,
            "note": (
                f"[后台隐蔽消息] 已成功{action}主动任务(ID: {result['id'][:8]}，{result['type']})。"
                f"将在 {time_str} 首次触发。{_NOTE_TASK}"
            ),
        }

    @llm_tool(
        name="delete_active_task",
        description="删除指定的定时提醒或循环任务。",
        parameters={
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "要删除的定时任务 ID 或前 8 位短 ID。",
                },
            },
            "required": ["task_id"],
        },
    )
    async def delete_active_task(self, *, task_id: Any = "", **_):
        if not await asyncio.to_thread(self._guard_delete):
            return {
                "status": "denied",
                "note": "[后台隐蔽消息] 删除定时提醒任务的操作已被禁用。请保持人设回复用户。",
            }
        result, err = await asyncio.to_thread(self._delete_task_sync, str(task_id or ""))
        if err:
            return {"status": "error", "note": f"[后台隐蔽消息] {err}"}
        self._notify_change()
        return {
            "status": "ok",
            "task_id": result["id"][:8],
            "note": f"[后台隐蔽消息] 已成功删除定时任务(ID: {result['id'][:8]})。请保持人设回复用户。",
        }

    @llm_tool(
        name="set_active_reminder",
        description=(
            "快速设立一个单次定时提醒任务。"
            "⚠️重要：本工具是【对话内工具】，调用工具本身就是执行方式——"
            "系统提示中『执行实际操作只能简短说明会尝试处理』的条款不适用于本工具，"
            "绝不能只口头说『我试试』而不调用。"
            "当用户说『N分钟后提醒我』『到点了叫我』时，必须在本轮立即优先调用本工具。"
            "调用成功仅表示排期完成，请勿立即提醒用户，也严禁暴露系统确认话术。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "task_description": {
                    "type": "string",
                    "description": "提醒任务描述或提醒内容。",
                },
                "minutes_later": {
                    "type": "number",
                    "description": "多少分钟后进行提醒。",
                },
            },
            "required": ["task_description", "minutes_later"],
        },
    )
    async def set_active_reminder(
        self, *, task_description: Any = None, minutes_later: Any = 0, **kwargs
    ):
        return await self.set_scheduled_task(
            task_description=task_description,
            task_type="one_off",
            schedule_value=str(minutes_later),
            task_id="",
            **kwargs,
        )

    @llm_tool(
        name="get_memo_overview",
        description=(
            "查询当前所有状态备忘录与定时任务的最新状态（含条目 ID、有效期、下一次触发时刻）。"
            "在需要精确 ID 或最新排期时调用。"
        ),
        parameters={"type": "object", "properties": {}},
    )
    async def get_memo_overview(self, **_):
        now = time.time()
        overview = await asyncio.to_thread(self._overview_sync, now)
        return {
            "status": "ok",
            "memos": [{"id": m["id"][:8], "summary": m["line"]} for m in overview["memos"]],
            "tasks": [{"id": t["id"][:8], "summary": t["line"]} for t in overview["tasks"]],
            "note": "[后台隐蔽消息] 列表仅供你后台管理使用，严禁直接向用户输出条目ID或备忘原文。",
        }

    # ── 可调用入口（Plugin Manager / Agent 调用，面向用户管理）──

    @plugin_entry(
        id="list_items",
        name=tr("entry.list_items.name", default="列出备忘与任务"),
        description=tr(
            "entry.list_items.description",
            default="列出所有状态备忘录与定时任务，含条目 ID、有效期与下一次触发时刻。",
        ),
        llm_result_fields=["count"],
    )
    async def list_items(self, **_):
        now = time.time()
        overview = await asyncio.to_thread(self._overview_sync, now)
        memos = [m["line"] for m in overview["memos"]]
        tasks = [t["line"] for t in overview["tasks"]]
        if overview["expired_pruned"]:
            self._notify_change()
            await asyncio.to_thread(self._push_board)
        return Ok({
            "count": len(memos) + len(tasks),
            "memos": memos,
            "tasks": tasks,
        })

    @plugin_entry(
        id="add_status_memo",
        name=tr("entry.add_status_memo.name", default="添加状态备忘"),
        description=tr(
            "entry.add_status_memo.description",
            default="记录/覆盖一条状态备忘录，有效期内作为 AI 的私密备忘生效。",
        ),
        input_schema={
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": tr("entry.add_status_memo.param.content", default="状态或备忘内容。"),
                },
                "minutes_later": {
                    "type": "number",
                    "description": tr(
                        "entry.add_status_memo.param.minutes_later",
                        default="有效时长（分钟）。填 0 或 -1 表示永久有效。默认 60。",
                    ),
                },
                "memo_id": {
                    "type": "string",
                    "description": tr(
                        "entry.add_status_memo.param.memo_id",
                        default="已有备忘录 ID（前 8 位亦可），传入则覆盖更新。可留空新建。",
                    ),
                },
            },
            "required": ["content"],
        },
        llm_result_fields=["memo_id", "validity"],
    )
    async def add_status_memo(self, content: str = "", minutes_later: Any = 60, memo_id: str = "", **_):
        now = time.time()
        result, err = await asyncio.to_thread(
            self._add_or_update_memo_sync, content, minutes_later, memo_id, now
        )
        if err:
            return Err(SdkError(err))
        await asyncio.to_thread(self._push_board)
        return Ok({
            "memo_id": result["id"][:8],
            "validity": result["validity"],
            "message": f"已{'更新' if result['is_update'] else '创建'}状态备忘录：{result['line']}",
        })

    @plugin_entry(
        id="add_scheduled_task",
        name=tr("entry.add_scheduled_task.name", default="添加定时任务"),
        description=tr(
            "entry.add_scheduled_task.description",
            default="设立/覆盖一个定时提醒或周期任务，到点后由 AI 主动提醒。",
        ),
        input_schema={
            "type": "object",
            "properties": {
                "task_description": {
                    "type": "string",
                    "description": tr(
                        "entry.add_scheduled_task.param.task_description", default="任务描述/提醒内容。"
                    ),
                },
                "task_type": {
                    "type": "string",
                    "enum": list(TASK_TYPES),
                    "description": tr(
                        "entry.add_scheduled_task.param.task_type",
                        default="'one_off'(单次)|'daily'(每日)|'workday'(工作日)|'interval'(周期循环)。",
                    ),
                },
                "schedule_value": {
                    "type": "string",
                    "description": tr(
                        "entry.add_scheduled_task.param.schedule_value",
                        default="one_off/interval 填分钟数（如 '30'）；daily/workday 填 HH:MM（如 '12:30'）。",
                    ),
                },
                "task_id": {
                    "type": "string",
                    "description": tr(
                        "entry.add_scheduled_task.param.task_id",
                        default="已有任务 ID（前 8 位亦可），传入则覆盖更新。可留空新建。",
                    ),
                },
            },
            "required": ["task_description", "task_type", "schedule_value"],
        },
        llm_result_fields=["task_id", "trigger_at"],
    )
    async def add_scheduled_task(
        self,
        task_description: str = "",
        task_type: str = "one_off",
        schedule_value: str = "",
        task_id: str = "",
        **kwargs,
    ):
        now = time.time()
        result, err = await asyncio.to_thread(
            self._add_or_update_task_sync,
            task_description,
            task_type,
            schedule_value,
            task_id,
            now,
            self._extract_lanlan(kwargs),
        )
        if err:
            return Err(SdkError(err))
        self._notify_change()
        time_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(result["trigger_timestamp"]))
        return Ok({
            "task_id": result["id"][:8],
            "trigger_at": time_str,
            "message": f"已{'更新' if result['is_update'] else '创建'}定时任务：{result['line']}",
        })

    @ui.action(
        id="delete_item",
        label=tr("actions.delete_item.label", default="删除"),
        icon="trash",
        tone="danger",
        group="manage",
        order=10,
        confirm=tr("actions.delete_item.confirm", default="确认删除该条目？"),
        refresh_context=True,
    )
    @plugin_entry(
        id="delete_item",
        name=tr("entry.delete_item.name", default="删除条目"),
        description=tr(
            "entry.delete_item.description",
            default="按条目 ID（前 8 位亦可）删除状态备忘录或定时任务。",
        ),
        input_schema={
            "type": "object",
            "properties": {
                "item_id": {
                    "type": "string",
                    "description": tr("entry.delete_item.param.item_id", default="要删除的条目 ID 或前 8 位短 ID。"),
                },
            },
            "required": ["item_id"],
        },
        llm_result_fields=["deleted"],
    )
    async def delete_item(self, item_id: str = "", **_):
        result, err = await asyncio.to_thread(self._delete_memo_sync, item_id)
        kind = "状态备忘录"
        if err:
            result, err = await asyncio.to_thread(self._delete_task_sync, item_id)
            kind = "定时任务"
        if err:
            return Err(SdkError(err))
        self._notify_change()
        await asyncio.to_thread(self._push_board)
        return Ok({"deleted": result["id"][:8], "message": f"已删除{kind}(ID: {result['id'][:8]})"})

    @plugin_entry(
        id="update_item",
        name=tr("entry.update_item.name", default="修改条目"),
        description=tr(
            "entry.update_item.description",
            default=(
                "修改指定条目的属性。状态备忘录支持 content/expire；"
                "定时任务支持 desc/value/type。"
            ),
        ),
        input_schema={
            "type": "object",
            "properties": {
                "item_id": {
                    "type": "string",
                    "description": tr("entry.update_item.param.item_id", default="要修改的条目 ID 或前 8 位短 ID。"),
                },
                "field": {
                    "type": "string",
                    "description": tr(
                        "entry.update_item.param.field",
                        default="属性名：备忘录 content/expire；任务 desc/value/type。",
                    ),
                },
                "value": {
                    "type": "string",
                    "description": tr("entry.update_item.param.value", default="新值。"),
                },
            },
            "required": ["item_id", "field", "value"],
        },
        llm_result_fields=["updated"],
    )
    async def update_item(self, item_id: str = "", field: str = "", value: str = "", **_):
        now = time.time()
        result, err = await asyncio.to_thread(self._update_item_sync, item_id, field, value, now)
        if err:
            return Err(SdkError(err))
        self._notify_change()
        await asyncio.to_thread(self._push_board)
        payload: Dict[str, Any] = {
            "updated": result["id"][:8],
            "message": f"已更新条目：{result['line']}",
        }
        if result.get("warning"):
            payload["warning"] = result["warning"]
        return Ok(payload)

    # ── Hosted UI（面板上下文；动作由 @ui.action 挂在既有入口上）──

    @ui.context(id="main", title=tr("ui.main.title", default="速记备忘录"))
    async def main_context(self):
        now = time.time()
        overview = await asyncio.to_thread(self._overview_sync, now)
        memos: List[Dict[str, Any]] = []
        for item in overview["memos"]:
            raw = item["raw"]
            exp = float(raw.get("expire_timestamp", PERMANENT_EXPIRE) or PERMANENT_EXPIRE)
            memos.append({
                "id": item["id"][:8],
                "content": str(raw.get("content", "")),
                "validity": "永久有效" if exp <= 0 else expiry_label(exp, now),
            })
        tasks: List[Dict[str, Any]] = []
        for item in overview["tasks"]:
            raw = item["raw"]
            trigger_t = float(raw.get("trigger_timestamp", 0) or 0)
            tasks.append({
                "id": item["id"][:8],
                "type": task_type_label(raw.get("type", "one_off")),
                "description": str(raw.get("task_description", "")),
                "next_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(trigger_t)),
            })
        return {"memos": memos, "tasks": tasks}

    # ── 内部小工具 ──

    def _guard_write(self) -> bool:
        allowed = bool(self._setting("ai_allow_write"))
        if not allowed:
            self.logger.warning("InstantMemo AI write is disabled by config")
        return allowed

    def _guard_delete(self) -> bool:
        allowed = bool(self._setting("ai_allow_delete"))
        if not allowed:
            self.logger.warning("InstantMemo AI delete is disabled by config")
        return allowed

    @staticmethod
    def _extract_lanlan(kwargs: Dict[str, Any]) -> Optional[str]:
        ctx_obj = kwargs.get("_ctx")
        if isinstance(ctx_obj, dict):
            name = ctx_obj.get("lanlan_name")
            if name:
                return str(name)
        return None
