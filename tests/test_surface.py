"""插件入口面（entry / llm_tool）冒烟测试：注册元数据与 async 约定。"""

from __future__ import annotations

import inspect
import sys
from importlib import import_module
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[4]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

pytest.importorskip("plugin.sdk.plugin")

InstantMemoPlugin = import_module("plugin.plugins.instant_memo").InstantMemoPlugin
_llm_tool_module = import_module("plugin.sdk.plugin.llm_tool")
LLM_TOOL_META_ATTR = _llm_tool_module.LLM_TOOL_META_ATTR
LlmToolMeta = _llm_tool_module.LlmToolMeta


def _llm_tool_metas() -> dict:
    """扫描类上的 @llm_tool 标记（无需实例化插件）。"""
    metas = {}
    for _, member in inspect.getmembers(InstantMemoPlugin):
        meta = getattr(member, LLM_TOOL_META_ATTR, None)
        if isinstance(meta, LlmToolMeta):
            metas[meta.name] = member
    return metas


def test_expected_llm_tools_registered() -> None:
    names = set(_llm_tool_metas())
    assert {
        "set_status_memo",
        "delete_status_memo",
        "set_scheduled_task",
        "delete_active_task",
        "set_active_reminder",
        "get_memo_overview",
    } <= names


def test_expected_entries_defined() -> None:
    for entry_id in ("list_items", "add_status_memo", "add_scheduled_task", "delete_item", "update_item"):
        method = getattr(InstantMemoPlugin, entry_id, None)
        assert method is not None, f"missing entry {entry_id}"
        assert inspect.iscoroutinefunction(method), f"entry {entry_id} must be async def"


def test_llm_tool_handlers_are_async() -> None:
    for name, handler in _llm_tool_metas().items():
        assert inspect.iscoroutinefunction(handler), f"llm tool {name} must be async def"


def test_llm_tool_schemas_are_provider_safe() -> None:
    """@llm_tool 的 description/parameters 会经 to_ipc_payload 原样进入模型的
    function schema，provider 要求其中的值必须是纯字符串。放入 tr() 延迟翻译
    对象（{"$i18n": ...}）会让所有对话请求 400 invalid_request_error。"""
    import json

    for name, handler in _llm_tool_metas().items():
        meta = getattr(handler, LLM_TOOL_META_ATTR)
        assert isinstance(meta.description, str), f"llm tool {name} description must be str"
        payload = json.dumps(meta.parameters, ensure_ascii=False)
        assert "$i18n" not in payload, (
            f"llm tool {name} parameters must not contain tr() i18n refs"
        )


def test_lifecycle_handlers_defined() -> None:
    for name in ("startup", "shutdown", "on_config_change"):
        method = getattr(InstantMemoPlugin, name, None)
        assert method is not None, f"missing lifecycle handler {name}"
        assert inspect.iscoroutinefunction(method)


def test_module_import_has_no_background_side_effects() -> None:
    """模块顶层不得启动线程/事件循环（父进程可能仅为扫描而 import）。"""
    import threading

    assert not any(
        t.name == "instant-memo-checker" and t.is_alive() for t in threading.enumerate()
    ), "checker thread must only start in the startup lifecycle handler"


def test_core_async_helpers_do_not_block_loop() -> None:
    """async 入口经 asyncio.to_thread 调用同步 helper（结构检查）。"""
    import plugin.plugins.instant_memo as mod

    source = inspect.getsource(mod)
    assert "asyncio.to_thread" in source


# ── Hosted UI 面 ──


def test_hosted_ui_surface_declared() -> None:
    root = Path(__file__).resolve().parents[1]
    manifest = (root / "plugin.toml").read_text(encoding="utf-8")
    assert "[plugin.ui]" in manifest
    assert 'entry = "ui/panel.tsx"' in manifest
    panel = root / "ui" / "panel.tsx"
    assert panel.is_file()
    source = panel.read_text(encoding="utf-8")
    assert "export default" in source
    assert "from \"@neko/plugin-ui\"" in source


def test_ui_context_and_action_registered() -> None:
    from plugin.sdk.plugin import ui as sdk_ui

    context_meta = getattr(InstantMemoPlugin, "main_context", None)
    assert context_meta is not None
    assert getattr(context_meta, sdk_ui.UI_CONTEXT_META_ATTR, None) is not None, (
        "main_context must carry @ui.context metadata"
    )
    delete_item = getattr(InstantMemoPlugin, "delete_item")
    assert getattr(delete_item, sdk_ui.UI_ACTION_META_ATTR, None) is not None, (
        "delete_item must carry @ui.action metadata"
    )


def test_locale_files_key_consistency() -> None:
    """8 个 locale 文件的键集必须一致（含 UI 文案键）。"""
    import json

    root = Path(__file__).resolve().parents[1]
    key_sets = {}
    for path in sorted((root / "i18n").glob("*.json")):
        key_sets[path.name] = set(json.loads(path.read_text(encoding="utf-8")))
    assert len(key_sets) == 8, f"expected 8 locale files, got {sorted(key_sets)}"
    base = key_sets["zh-CN.json"]
    assert base, "zh-CN.json must not be empty"
    for name, keys in key_sets.items():
        assert keys == base, f"{name} key set differs from zh-CN.json"
