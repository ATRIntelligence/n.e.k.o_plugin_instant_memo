"""core.py 纯函数单元测试（不依赖 plugin.sdk，可独立运行）。"""

from __future__ import annotations

import importlib.util
import time
from pathlib import Path

_CORE_PATH = Path(__file__).resolve().parents[1] / "core.py"
_spec = importlib.util.spec_from_file_location("instant_memo_core", _CORE_PATH)
core = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(core)


# ── 时间换算 ──

def test_get_next_daily_timestamp_is_in_the_future() -> None:
    ts = core.get_next_daily_timestamp("12:00")
    assert ts > time.time()


def test_get_next_daily_timestamp_tolerates_bad_input() -> None:
    ts = core.get_next_daily_timestamp("not-a-time")
    assert ts > time.time()


def test_get_next_workday_timestamp_skips_weekend() -> None:
    ts = core.get_next_workday_timestamp("09:30")
    assert ts > time.time()
    import datetime

    assert datetime.datetime.fromtimestamp(ts).weekday() < 5


# ── 排程值规范化 ──

def test_normalize_minutes_schedule() -> None:
    value, err = core.normalize_schedule_value("one_off", "30")
    assert err is None
    assert value == "30"


def test_normalize_minutes_schedule_extracts_digits() -> None:
    value, err = core.normalize_schedule_value("interval", "每 45 分钟")
    assert err is None
    assert value == "45"


def test_normalize_daily_schedule_formats_chinese_time() -> None:
    value, err = core.normalize_schedule_value("daily", "9点30")
    assert err is None
    assert value == "09:30"


def test_normalize_daily_schedule_hour_only() -> None:
    value, err = core.normalize_schedule_value("workday", "8点")
    assert err is None
    assert value == "08:00"


def test_normalize_rejects_bad_task_type() -> None:
    _, err = core.normalize_schedule_value("weekly", "30")
    assert err is not None


def test_normalize_rejects_bad_value() -> None:
    _, err = core.normalize_schedule_value("daily", "午后")
    assert err is not None


# ── 有效期解析 ──

def test_resolve_expiry_permanent() -> None:
    now = time.time()
    for raw in (0, -1, "0", "-1", "permanent", "永久"):
        exp, label = core.resolve_expiry(raw, now)
        assert exp == core.PERMANENT_EXPIRE
        assert label == "永久有效"


def test_resolve_expiry_minutes() -> None:
    now = time.time()
    exp, label = core.resolve_expiry(30, now)
    assert abs(exp - (now + 1800)) < 1
    assert label == "有效期 30 分钟"


def test_resolve_expiry_fallback_on_garbage() -> None:
    now = time.time()
    exp, label = core.resolve_expiry("abc", now)
    assert abs(exp - (now + 3600)) < 1
    assert label == "有效期 60 分钟"


# ── 过期清理 ──

def test_prune_expired_memos() -> None:
    now = time.time()
    memos = {
        "a": {"expire_timestamp": now - 10},
        "b": {"expire_timestamp": now + 100},
        "c": {"expire_timestamp": core.PERMANENT_EXPIRE},
    }
    kept, expired = core.prune_expired_memos(memos, now)
    assert expired == ["a"]
    assert set(kept) == {"b", "c"}


# ── 任务重排 ──

def test_reschedule_one_off_is_removed() -> None:
    task = {"type": "one_off", "scheduled_time": "30", "trigger_timestamp": time.time()}
    assert core.reschedule_task(task, time.time()) is None


def test_reschedule_interval_advances_past_now() -> None:
    now = time.time()
    task = {"type": "interval", "scheduled_time": "10", "trigger_timestamp": now - 3600}
    updated = core.reschedule_task(task, now)
    assert updated is not None
    assert updated["trigger_timestamp"] > now


def test_reschedule_daily_sets_future_trigger() -> None:
    now = time.time()
    task = {"type": "daily", "scheduled_time": "12:00", "trigger_timestamp": now - 10}
    updated = core.reschedule_task(task, now)
    assert updated is not None
    assert updated["trigger_timestamp"] > now
    assert updated["last_run_timestamp"] == now


# ── 短 ID 查找 ──

def test_find_by_short_id() -> None:
    items = {"abcdef123456": {}, "999988887777": {}}
    assert core.find_by_short_id(items, "abcdef123456") == "abcdef123456"
    assert core.find_by_short_id(items, "abcdef") == "abcdef123456"
    assert core.find_by_short_id(items, "zzz") is None
    assert core.find_by_short_id(items, "") is None


# ── 备忘板文本 ──

def test_build_memo_board_empty() -> None:
    assert core.build_memo_board({}, {}, time.time()) == ""


def test_build_memo_board_contains_items_and_privacy_rules() -> None:
    now = time.time()
    memos = {
        "aaaa11112222": {"content": "目标数字为 42", "expire_timestamp": core.PERMANENT_EXPIRE},
        "bbbb33334444": {"content": "已过期内容", "expire_timestamp": now - 5},
    }
    tasks = {
        "cccc55556666": {
            "type": "daily",
            "task_description": "提醒用户晾衣服",
            "scheduled_time": "12:00",
            "trigger_timestamp": core.get_next_daily_timestamp("12:00"),
        }
    }
    board = core.build_memo_board(memos, tasks, now)
    assert "目标数字为 42" in board
    assert "已过期内容" not in board
    assert "提醒用户晾衣服" in board
    assert "核心保密与人设原则" in board
    assert "aaaa1111" in board
