"""Tests for SleepPauseTimer.

Run: `pytest`.

Level 1: in process, no daemon, no socket. Goes through the real gRPC
servicer, so a tool that is declared but not routed fails here.

Важно: тесты никогда не дают таймеру дотикать до нуля — действие по
завершении (playpause/shutdown/sleep) не должно сработать при тестах.
Все таймеры в тестах длинные и гасятся `cancel_timer`.
"""

import asyncio
import sys
from pathlib import Path

# The daemon puts the bundle root on `sys.path` before importing `src.plugin`;
# do the same so `pytest` from the project root finds it.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from astra_plugin_sdk.testing import Harness, fuzz_configs  # noqa: E402

from src.plugin import SleepPauseTimer  # noqa: E402

EXPECTED_TOOLS = {
    "start_timer",
    "pause_timer",
    "resume_timer",
    "cancel_timer",
    "timer_status",
    "list_windows",
}


def test_tools_are_registered_with_matching_schemas():
    with Harness(SleepPauseTimer()) as h:
        assert set(h.tool_names()) == EXPECTED_TOOLS

        # Схема, которую видит модель, действительно объявляет параметры
        # обработчика start_timer.
        h.assert_schema_accepts(
            "start_timer", "hours", "minutes", "seconds", "action", "window"
        )
        # Все параметры опциональны — у каждого есть значение по умолчанию,
        # поэтому поля required в схеме нет вовсе.
        assert h.schema("start_timer").get("required", []) == []


def test_zero_duration_is_a_bad_argument():
    with Harness(SleepPauseTimer()) as h:
        result = h.call_tool("start_timer", hours=0, minutes=0, seconds=0)
        assert not result.success
        assert result.code == "BAD_ARGUMENTS", result.code


def test_unknown_action_is_a_bad_argument():
    with Harness(SleepPauseTimer()) as h:
        result = h.call_tool("start_timer", minutes=5, action="explode")
        assert not result.success
        assert result.code == "BAD_ARGUMENTS", result.code


def test_start_pause_resume_cancel_lifecycle():
    with Harness(SleepPauseTimer()) as h:
        started = h.call_tool("start_timer", minutes=30)
        assert started.success, started.code
        assert started.json["state"]["running"] is True

        status = h.call_tool("timer_status")
        assert status.success
        assert status.json["running"] is True
        assert status.json["remaining_seconds"] <= 30 * 60

        paused = h.call_tool("pause_timer")
        assert paused.success
        assert "Пауза" in paused.json

        status = h.call_tool("timer_status")
        assert status.json["paused"] is True

        resumed = h.call_tool("resume_timer")
        assert resumed.success
        assert "Продолжаем" in resumed.json

        cancelled = h.call_tool("cancel_timer")
        assert cancelled.success
        assert cancelled.json == "Таймер отменён"

        status = h.call_tool("timer_status")
        assert status.json["running"] is False


def test_second_start_is_refused_until_cancel():
    with Harness(SleepPauseTimer()) as h:
        first = h.call_tool("start_timer", minutes=30)
        assert first.success, first.code

        second = h.call_tool("start_timer", minutes=10)
        assert not second.success
        assert second.code == "UNAVAILABLE", second.code

        # Прибираемся: гасим таймер, чтобы он не дотикал до действия.
        assert h.call_tool("cancel_timer").success


def test_pause_and_cancel_without_timer_are_friendly():
    with Harness(SleepPauseTimer()) as h:
        assert h.call_tool("pause_timer").json == "Таймер не запущен"
        assert h.call_tool("resume_timer").json == "Таймер не запущен"
        assert h.call_tool("cancel_timer").json == "Таймер не запущен"


def test_list_windows_returns_numbered_titles():
    with Harness(SleepPauseTimer()) as h:
        result = h.call_tool("list_windows")
        assert result.success, result.code
        assert isinstance(result.json["windows"], list)
        for entry in result.json["windows"]:
            assert entry.startswith("#")
            assert " — " in entry


def test_widget_contribution_is_a_transparent_home_widget():
    plugin = SleepPauseTimer()
    contributions = asyncio.run(plugin.get_ui_contributions())
    assert len(contributions) == 1
    c = contributions[0]
    assert c.slot == "home.widgets"
    assert c.url == "widget.html"
    assert c.transparent is True  # иначе — «чёрный фон виджета» (§8.2)
    assert c.pointer_events is True
    assert c.height > 0


def test_ui_calls_report_engine_state():
    with Harness(SleepPauseTimer()) as h:
        state = h.ui_call("state")
        assert state.success, state.code
        assert state.json["running"] is False


def test_no_config_the_daemon_can_deliver_crashes_this_plugin():
    # The daemon delivers config it did not author: the user's typing, and an
    # older version of this plugin's own schema. `{}` — a fresh install — is
    # the first payload every plugin ever sees. None of it may throw.
    with Harness(SleepPauseTimer()) as h:
        for payload in fuzz_configs():
            h.set_config(payload)
        # И инструменты после любого конфига отвечают, а не падают.
        assert h.call_tool("timer_status").success

