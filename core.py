"""速记备忘录纯函数层：时间换算、排程、过期清理、备忘板文本。

本模块不依赖 ``plugin.sdk``，只用标准库，便于单元测试。
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

# 状态备忘录：expire_timestamp <= 0 表示永久有效
PERMANENT_EXPIRE = -1.0

TASK_TYPES = ("one_off", "daily", "workday", "interval")

_TASK_TYPE_CN = {
    "one_off": "单次定时",
    "daily": "每日定时",
    "workday": "工作日定时",
    "interval": "周期性循环",
}

_PERMANENT_KEYWORDS = frozenset({"-1", "0", "permanent", "inf", "永久"})


def get_next_daily_timestamp(time_str: str) -> float:
    """根据 HH:MM 获取下一次的绝对时间戳（今天已过则为明天）。"""
    try:
        parts = time_str.strip().split(":")
        hour = int(parts[0])
        minute = int(parts[1])
    except Exception:
        hour, minute = 12, 0

    now = datetime.now()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target.timestamp()


def get_next_workday_timestamp(time_str: str) -> float:
    """根据 HH:MM 获取下一次工作日（周一至周五）的绝对时间戳。"""
    try:
        parts = time_str.strip().split(":")
        hour = int(parts[0])
        minute = int(parts[1])
    except Exception:
        hour, minute = 12, 0

    now = datetime.now()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    while target.weekday() >= 5:
        target += timedelta(days=1)
    return target.timestamp()


def _parse_minutes(value_str: str) -> Optional[int]:
    match = re.search(r"\d+", value_str)
    return int(match.group(0)) if match else None


def normalize_schedule_value(task_type: str, schedule_value: str) -> Tuple[Optional[str], Optional[str]]:
    """规范化排程值。

    返回 ``(normalized_value, error)``；成功时 error 为 None。
    one_off/interval 归一为整数分钟字符串；daily/workday 归一为 ``HH:MM``。
    """
    t_type = (task_type or "").strip().lower()
    if t_type not in TASK_TYPES:
        return None, f"task_type 参数必须是 {'/'.join(TASK_TYPES)} 之一。"

    val = (schedule_value or "").strip()
    if t_type in ("one_off", "interval"):
        try:
            mins = int(val)
        except ValueError:
            mins = _parse_minutes(val)
        if mins is None:
            return None, f"对于 {t_type} 任务，schedule_value 必须为整数分钟数（如 '30'）。"
        return str(mins), None

    # daily / workday
    if ":" not in val:
        match = re.search(r"(\d{1,2})[:：点\s](\d{2})", val)
        if match:
            hours, minutes = int(match.group(1)), int(match.group(2))
            if 0 <= hours <= 23 and 0 <= minutes <= 59:
                val = f"{hours:02d}:{minutes:02d}"
        else:
            match_single = re.search(r"^(\d{1,2})(?:点|时)?$", val)
            if match_single:
                hours = int(match_single.group(1))
                if 0 <= hours <= 23:
                    val = f"{hours:02d}:00"
    if ":" not in val:
        return None, f"对于 {t_type} 任务，schedule_value 必须是 HH:MM 格式（如 '12:00'）。"
    return val, None


def compute_trigger_timestamp(task_type: str, scheduled_time: str, now: float) -> float:
    """计算任务的下一次触发时间戳。"""
    t_type = (task_type or "").strip().lower()
    if t_type == "one_off":
        return now + int(scheduled_time) * 60
    if t_type == "interval":
        return now + int(scheduled_time) * 60
    if t_type == "daily":
        return get_next_daily_timestamp(scheduled_time)
    return get_next_workday_timestamp(scheduled_time)


def resolve_expiry(minutes_later: Any, now: float) -> Tuple[float, str]:
    """把 ``minutes_later`` 解析为 ``(expire_timestamp, 有效期描述)``。"""
    raw = str(minutes_later).strip().lower()
    if raw in _PERMANENT_KEYWORDS:
        return PERMANENT_EXPIRE, "永久有效"
    try:
        mins = float(minutes_later)
    except (TypeError, ValueError):
        return now + 60 * 60, "有效期 60 分钟"
    if mins <= 0:
        return PERMANENT_EXPIRE, "永久有效"
    return now + mins * 60, f"有效期 {int(mins)} 分钟"


def expiry_label(expire_timestamp: float, now: float) -> str:
    """备忘有效期的可读标签。"""
    if expire_timestamp <= 0:
        return "永久有效"
    if now >= expire_timestamp:
        return "已过期"
    minutes = int((expire_timestamp - now) / 60)
    return f"{minutes} 分钟后过期" if minutes > 0 else "即将过期"


def prune_expired_memos(memos: Dict[str, Any], now: float) -> Tuple[Dict[str, Any], List[str]]:
    """剔除过期备忘，返回 ``(保留的备忘, 被清理的ID列表)``。"""
    kept: Dict[str, Any] = {}
    expired: List[str] = []
    for memo_id, memo in memos.items():
        exp = float(memo.get("expire_timestamp", PERMANENT_EXPIRE) or PERMANENT_EXPIRE)
        if exp > 0 and now > exp:
            expired.append(memo_id)
        else:
            kept[memo_id] = memo
    return kept, expired


def reschedule_task(task: Dict[str, Any], now: float) -> Optional[Dict[str, Any]]:
    """触发后重排任务；返回 None 表示任务应被删除（one_off）。

    daily/workday/interval 推进到下一次触发点并清除 transient 状态。
    """
    t_type = str(task.get("type", "one_off"))
    if t_type == "one_off":
        return None

    scheduled_time = str(task.get("scheduled_time", ""))
    if t_type == "interval":
        try:
            interval_mins = int(scheduled_time)
        except ValueError:
            interval_mins = 60
        next_ts = float(task.get("trigger_timestamp", now)) + interval_mins * 60
        while next_ts <= now:
            next_ts += interval_mins * 60
    elif t_type == "daily":
        next_ts = get_next_daily_timestamp(scheduled_time)
    else:
        next_ts = get_next_workday_timestamp(scheduled_time)

    updated = dict(task)
    updated["trigger_timestamp"] = next_ts
    updated["last_run_timestamp"] = now
    return updated


def find_by_short_id(items: Dict[str, Any], key: str) -> Optional[str]:
    """按完整 ID 或短 ID 前缀查找条目 ID。"""
    clean = (key or "").strip()
    if not clean:
        return None
    if clean in items:
        return clean
    for real_id in items:
        if real_id.startswith(clean):
            return real_id
    return None


def build_memo_board(memos: Dict[str, Any], tasks: Dict[str, Any], now: float) -> str:
    """构建注入模型上下文的备忘板文本（ai_behavior="read"）。"""
    sections: List[str] = []

    memo_lines: List[str] = []
    for memo_id, memo in sorted(memos.items(), key=lambda kv: float(kv[1].get("expire_timestamp", 0) or 0)):
        exp = float(memo.get("expire_timestamp", PERMANENT_EXPIRE) or PERMANENT_EXPIRE)
        if exp > 0 and now > exp:
            continue
        label = "永久有效" if exp <= 0 else f"有效期至 {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(exp))}"
        memo_lines.append(f" - [备忘录ID: {memo_id[:8]}] [{label}]: {memo.get('content', '')}")
    if memo_lines:
        sections.append("【临时人设/状态备忘录（请严格遵循扮演）】\n" + "\n".join(memo_lines))

    task_lines: List[str] = []
    for task_id, task in sorted(tasks.items(), key=lambda kv: float(kv[1].get("trigger_timestamp", 0) or 0)):
        t_type = str(task.get("type", "one_off"))
        trigger_t = float(task.get("trigger_timestamp", 0) or 0)
        time_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(trigger_t))
        type_cn = _TASK_TYPE_CN.get(t_type, t_type)
        task_lines.append(
            f" - [任务ID: {task_id[:8]}] [{type_cn} - 下一次触发: {time_str}]: {task.get('task_description', '')}"
        )
    if task_lines:
        sections.append(
            "【正在后台运行的定时提醒与周期计划（触发时刻会自动顺延，最新排期以 get_memo_overview 工具为准）】\n"
            + "\n".join(task_lines)
        )

    if not sections:
        return ""

    return (
        "=== [系统动态备忘录与运行状态提示] ===\n"
        + "\n\n".join(sections)
        + "\n\n【说明】您可以调用对应的工具传入条目 ID 取消并移除不再需要的状态备忘或计划任务。"
        "\n\n=== [核心保密与人设原则] ===\n"
        "1. AI备忘录/定时任务是你后台的【私密记事本】。\n"
        "2. 当你在备忘录中记下信息（如猜数字游戏的目标数字、暗号、秘密状态等）时，"
        "【绝对不要】在给用户的回复中泄漏备忘的具体内容、答案、条目ID，"
        "也不要输出'已为您记下'、'成功设置备忘'等机械确认话术！\n"
        "3. 记下备忘后，请完全以你的人设角色自然地回复用户（例如玩猜数字时，"
        "你只需自然回答：'好呀，我已经记好数字了，你来猜吧！'，决不能在回复中把数字说出来）。\n"
        "======================================="
    )


def format_memo_line(memo_id: str, memo: Dict[str, Any], now: float) -> str:
    exp = float(memo.get("expire_timestamp", PERMANENT_EXPIRE) or PERMANENT_EXPIRE)
    label = "永久有效" if exp <= 0 else expiry_label(exp, now)
    return f"[{memo_id[:8]}] {memo.get('content', '')} ({label})"


def task_type_label(task_type: str) -> str:
    """任务类型的可读标签（Hosted UI 展示用）。"""
    t_type = str(task_type or "")
    return _TASK_TYPE_CN.get(t_type, t_type)


def format_task_line(task_id: str, task: Dict[str, Any], now: float) -> str:
    t_type = str(task.get("type", "one_off"))
    type_cn = _TASK_TYPE_CN.get(t_type, t_type)
    trigger_t = float(task.get("trigger_timestamp", 0) or 0)
    time_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(trigger_t))
    return f"[{task_id[:8]}] [{type_cn}] {task.get('task_description', '')} (下一次触发: {time_str})"


__all__ = [
    "PERMANENT_EXPIRE",
    "TASK_TYPES",
    "get_next_daily_timestamp",
    "get_next_workday_timestamp",
    "normalize_schedule_value",
    "compute_trigger_timestamp",
    "resolve_expiry",
    "expiry_label",
    "prune_expired_memos",
    "reschedule_task",
    "find_by_short_id",
    "build_memo_board",
    "format_memo_line",
    "format_task_line",
    "task_type_label",
]
