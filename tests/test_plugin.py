"""Tests for SleepPauseTimer.

Run: `pytest`.

Level 1: in process, no daemon, no socket. Goes through the real gRPC
servicer, so a tool that is declared but not routed fails here.

Важно: тесты никогда не дают таймеру дотикать до нуля — действие по
завершении (playpause/shutdown/sleep) не должно сработать при тестах.
Все таймеры в тестах длинные и гасятся `cancel_timer`.
"""

import asyncio
import base64
import sys
from pathlib import Path

import pytest

# The daemon puts the bundle root on `sys.path` before importing `src.plugin`;
# do the same so `pytest` from the project root finds it.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from astra_plugin_sdk.testing import Harness, fuzz_configs  # noqa: E402

from src.plugin import SleepPauseTimer  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_alarm_state(tmp_path, monkeypatch):
    # Без этого AlarmEngine пишет расписание в %APPDATA%: тесты оставляли бы
    # настоящий будильник на мелодию из временного файла, который удалён.
    monkeypatch.setenv("SPT_ALARM_STATE_DIR", str(tmp_path))

EXPECTED_TOOLS = {
    "start_timer",
    "pause_timer",
    "resume_timer",
    "cancel_timer",
    "timer_status",
    "list_windows",
    "set_alarm",
    "set_alarm_sound",
    "alarm_status",
    "cancel_alarm",
    "stop_alarm_sound",
    "choose_alarm_sound",
    "set_monthly_alarm",
    "cancel_monthly_alarm",
    "calendar_alarm_status",
    "stop_calendar_alarm_sound",
    "start_idle_monitor",
    "resume_idle_monitor",
    "stop_idle_monitor",
    "idle_status",
}


def test_tools_are_registered_with_matching_schemas():
    with Harness(SleepPauseTimer()) as h:
        assert set(h.tool_names()) == EXPECTED_TOOLS

        # Схема, которую видит модель, действительно объявляет параметры
        # обработчика start_timer.
        h.assert_schema_accepts(
            "start_timer", "hours", "minutes", "seconds", "action", "window"
        )
        h.assert_schema_accepts("set_alarm", "time", "sound_path", "repeat", "interval", "days_of_week")
        h.assert_schema_accepts("start_idle_monitor", "minutes", "action", "window")
        assert h.schema("start_idle_monitor").get("required", []) == ["minutes"]
        # Все параметры опциональны — у каждого есть значение по умолчанию,
        # поэтому поля required в схеме нет вовсе.
        assert h.schema("start_timer").get("required", []) == []



def test_resume_idle_monitor_tool_reuses_current_or_saved_configuration():
    class IdleStub:
        def __init__(self, state):
            self.current = dict(state)
            self.started = None

        def state(self):
            return dict(self.current)

        def start(self, minutes, action, window):
            self.started = (minutes, action, window)
            self.current = {"running": True, "minutes": minutes,
                            "action": action, "window": window}
            return dict(self.current)

    plugin = SleepPauseTimer()
    plugin.local_settings = {
        "idle_minutes": 18,
        "idle_action": "sleep",
        "default_window": "",
    }
    plugin.idle = IdleStub({"running": True, "minutes": 7,
                            "action": "playpause", "window": "Player"})
    with Harness(plugin) as h:
        current = h.call_tool("resume_idle_monitor")
        assert current.success, current.code
        assert plugin.idle.started == (7, "playpause", "Player")
        assert "возобновлено" in current.json["message"].lower()

    plugin = SleepPauseTimer()
    plugin.local_settings = {
        "idle_minutes": 18,
        "idle_action": "sleep",
        "default_window": "",
    }
    plugin.idle = IdleStub({"running": False, "minutes": 0,
                            "action": "playpause", "window": ""})
    with Harness(plugin) as h:
        saved = h.call_tool("resume_idle_monitor")
        assert saved.success, saved.code
        assert plugin.idle.started == (18, "sleep", "")


def test_start_idle_monitor_accepts_and_saves_spoken_duration():
    class IdleStub:
        def __init__(self):
            self.started = None

        def start(self, minutes, action, window):
            self.started = (minutes, action, window)
            return {"running": True, "minutes": minutes, "action": action, "window": window}

    plugin = SleepPauseTimer()
    plugin.local_settings = {"idle_minutes": 30, "idle_action": "playpause"}
    plugin.idle = IdleStub()
    saved = {}

    def save_settings(settings):
        saved.update(settings)
        return dict(settings)

    plugin._save_local_settings = save_settings
    with Harness(plugin) as h:
        result = h.call_tool("start_idle_monitor", minutes=40)
        assert result.success, result.code
        assert plugin.idle.started == (40, "playpause", "")
        assert saved["idle_minutes"] == 40
        assert result.json["message"].endswith("40 мин")


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
    assert len(contributions) == 3
    timer_widget = next(c for c in contributions if c.id == "timer-widget")
    alarm_widget = next(c for c in contributions if c.id == "alarm-widget")
    assert timer_widget.slot == "home.widgets"
    assert timer_widget.url == "widget.html"
    assert timer_widget.transparent is True
    assert timer_widget.pointer_events is True
    assert timer_widget.height > 0
    assert alarm_widget.slot == "home.widgets"
    assert alarm_widget.url == "alarm-widget.html"
    assert alarm_widget.transparent is True
    assert alarm_widget.pointer_events is True
    assert alarm_widget.height > 0
    settings_page = next(c for c in contributions if c.id == "timer-settings-page")
    assert settings_page.slot == "page.custom"
    assert settings_page.url == "settings-v2.html"
    assert 'data:image/png;base64,' in settings_page.icon_svg
    icon_payload = settings_page.icon_svg.split('base64,', 1)[1].split('"', 1)[0]
    assert base64.b64decode(icon_payload)[:8] == b"\x89PNG\r\n\x1a\n"


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
        assert h.call_tool("alarm_status").success


def test_alarm_tools_registered():
    with Harness(SleepPauseTimer()) as h:
        tools = set(h.tool_names())
        alarm_tools = {"set_alarm", "set_alarm_sound", "alarm_status", "cancel_alarm", "stop_alarm_sound", "choose_alarm_sound"}
        assert alarm_tools.issubset(tools)


def test_alarm_set_requires_sound_path():
    with Harness(SleepPauseTimer()) as h:
        # Missing sound_path should fail
        result = h.call_tool("set_alarm", hour=7, minute=30)
        assert not result.success
        assert result.code == "BAD_ARGUMENTS"


def test_alarm_set_rejects_invalid_time():
    with Harness(SleepPauseTimer()) as h:
        # Create a temporary audio file
        import tempfile
        import os
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(b"RIFF\x24\x00\x00\x00WAVEfmt ")
            temp_path = f.name
        try:
            # Invalid hour
            result = h.call_tool("set_alarm", time="25:00", sound_path=temp_path)
            assert not result.success
            assert result.code == "BAD_ARGUMENTS"
            # Invalid minute
            result = h.call_tool("set_alarm", time="07:60", sound_path=temp_path)
            assert not result.success
            assert result.code == "BAD_ARGUMENTS"
        finally:
            os.unlink(temp_path)


def test_alarm_set_and_status():
    with Harness(SleepPauseTimer()) as h:
        import tempfile
        import os
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(b"RIFF\x24\x00\x00\x00WAVEfmt ")
            temp_path = f.name
        try:
            result = h.call_tool("set_alarm", time="07:30", sound_path=temp_path, repeat=True)
            assert result.success
            state = result.json["state"]
            assert state["scheduled"] is True
            assert state["repeat"] is True
            assert state["hour"] == 7
            assert state["minute"] == 30
            assert state["sound_path"] == temp_path
            assert state["status"] == "scheduled"
            
            # Check status
            status = h.call_tool("alarm_status")
            assert status.success
            assert status.json["scheduled"] is True
        finally:
            os.unlink(temp_path)


def test_alarm_cancel():
    with Harness(SleepPauseTimer()) as h:
        import tempfile
        import os
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(b"RIFF\x24\x00\x00\x00WAVEfmt ")
            temp_path = f.name
        try:
            h.call_tool("set_alarm", hour=7, minute=30, sound_path=temp_path)
            result = h.call_tool("cancel_alarm")
            assert result.success
            state = result.json["state"]
            assert state["scheduled"] is False
            assert state["status"] == "idle"
        finally:
            os.unlink(temp_path)


def test_ui_calls_alarm():
    with Harness(SleepPauseTimer()) as h:
        # Test alarm_state UI call
        state = h.ui_call("alarm_state")
        assert state.success
        assert state.json["status"] == "idle"
        
        import tempfile
        import os
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(b"RIFF\x24\x00\x00\x00WAVEfmt ")
            temp_path = f.name
        try:
            h.call_tool("set_alarm", time="07:30", sound_path=temp_path)
            state = h.ui_call("alarm_state")
            assert state.success
            assert state.json["scheduled"] is True
            assert state.json["status"] == "scheduled"
            
            # Test cancel via UI
            cancel_state = h.ui_call("alarm_cancel")
            assert cancel_state.success
            assert cancel_state.json["scheduled"] is False
        finally:
            os.unlink(temp_path)


# ---------- контракт «виджет ↔ плагин» ----------
#
# Кнопка, которой не отвечает ни один @ui_call, просто молчит: iframe получает
# NOT_FOUND, и в интерфейсе это выглядит как «виджет не работает».


def test_every_widget_method_has_an_ui_call():
    # ui/widget.html зовёт state / pause / resume / cancel,
    # ui/alarm-widget.html — alarm_state / alarm_set / alarm_stop /
    # alarm_cancel / choose_alarm_sound.
    plugin = SleepPauseTimer()
    assert {
        "state",
        "pause",
        "resume",
        "cancel",
        "alarm_state",
        "alarm_set",
        "alarm_stop",
        "alarm_cancel",
        "choose_alarm_sound",
        "calendar_state",
        "calendar_set",
        "calendar_cancel",
        "calendar_stop",
        "settings_state",
        "settings_save",
        "idle_start",
        "idle_stop",
        "windows_list",
        "timer_start_local",
        "alarm_sound_choose_local",
        "alarm_sound_save",
    } <= set(plugin._ui_calls)


def test_alarm_rejects_bad_days_and_finds_next_selected_weekday(tmp_path):
    from datetime import datetime
    from src.alarm import AlarmEngine, AlarmError

    class SilentPlayer:
        def play_once(self, path, should_continue=None):
            return True
        def stop(self):
            pass

    now = datetime(2026, 9, 16, 7, 30)  # Wednesday
    sound = tmp_path / "alarm.wav"
    sound.write_bytes(b"audio")
    engine = AlarmEngine(state_path=tmp_path / "days.json", now_fn=lambda: now, player=SilentPlayer())
    try:
        with pytest.raises(AlarmError):
            engine.set_alarm(8, 0, str(sound), days_of_week=["1"])
        state = engine.set_alarm(8, 0, str(sound), days_of_week=[4])
        assert state["next_datetime"].startswith("2026-09-18T08:00")
        assert state["days_of_week"] == [4]
        engine.cancel()
    finally:
        engine.stop_thread()


def test_corrupt_saved_days_do_not_restore_alarm_or_hang(tmp_path):
    import json
    from datetime import datetime
    from src.alarm import AlarmEngine

    (tmp_path / "alarm.json").write_text(json.dumps({"alarm": {
        "hour": 8, "minute": 0, "days_of_week": [99]
    }}), encoding="utf-8")
    engine = AlarmEngine(state_path=tmp_path / "alarm.json", now_fn=lambda: datetime(2026, 9, 16, 7, 30))
    try:
        assert engine.state()["scheduled"] is False
    finally:
        engine.stop_thread()


def test_calendar_alarm_is_saved_and_removed(tmp_path):
    from datetime import date, timedelta
    from src.calendar_alarms import CalendarAlarmEngine

    class SilentPlayer:
        def play_once(self, path, should_continue=None):
            return True
        def stop(self):
            pass

    engine = CalendarAlarmEngine(path=tmp_path / "calendar.json", player=SilentPlayer())
    alarm_day = date.today() + timedelta(days=2)
    try:
        state = engine.set_alarm(alarm_day, 8, 15)
        assert state["count"] == 1
        assert state["alarms"][0] == {"date": alarm_day.isoformat(), "hour": 8, "minute": 15}
        state = engine.set_alarm(alarm_day, None, None)
        assert state["count"] == 0
    finally:
        engine.stop_thread()


def test_calendar_keeps_multiple_dates_and_restores_them(tmp_path):
    from datetime import date, timedelta
    import json
    from src.calendar_alarms import CalendarAlarmEngine

    class SilentPlayer:
        def play_once(self, path, should_continue=None):
            return True
        def stop(self):
            pass

    path = tmp_path / "calendar.json"
    first = CalendarAlarmEngine(path=path, player=SilentPlayer())
    dates = [date.today() + timedelta(days=offset) for offset in (1, 2, 3)]
    try:
        first.set_alarm(dates[0], 7, 30)
        # Emulate another live instance updating the shared durable calendar.
        path.write_text(json.dumps({
            dates[0].isoformat(): {"hour": 7, "minute": 30},
            dates[1].isoformat(): {"hour": 7, "minute": 30},
        }), encoding="utf-8")
        state = first.set_alarm(dates[2], 7, 30)
        assert state["count"] == 3
        assert [item["date"] for item in state["alarms"]] == [day.isoformat() for day in dates]
    finally:
        first.stop_thread()

    restored = CalendarAlarmEngine(path=path, player=SilentPlayer())
    try:
        assert restored.state()["count"] == 3
    finally:
        restored.stop_thread()


def test_calendar_supports_multiple_times_on_one_date_and_cancels_one(tmp_path):
    from datetime import date, timedelta
    from src.calendar_alarms import CalendarAlarmEngine

    class SilentPlayer:
        def play_once(self, path, should_continue=None):
            return True
        def stop(self):
            pass

    path = tmp_path / "calendar.json"
    engine = CalendarAlarmEngine(path=path, player=SilentPlayer())
    alarm_day = date.today() + timedelta(days=2)
    try:
        engine.set_alarm(alarm_day, 9, 0)
        state = engine.set_alarm(alarm_day, 11, 0)
        assert state["count"] == 2
        assert [(row["hour"], row["minute"]) for row in state["alarms"]] == [(9, 0), (11, 0)]

        # Adding the same date/time is idempotent and must not remove its sibling.
        state = engine.set_alarm(alarm_day, 9, 0)
        assert state["count"] == 2

        state = engine.cancel_alarm(alarm_day, 9, 0)
        assert state["count"] == 1
        assert [(row["hour"], row["minute"]) for row in state["alarms"]] == [(11, 0)]
    finally:
        engine.stop_thread()

    restored = CalendarAlarmEngine(path=path, player=SilentPlayer())
    try:
        assert restored.state()["count"] == 1
        assert restored.state()["alarms"][0]["hour"] == 11
    finally:
        restored.stop_thread()


def test_firing_one_calendar_alarm_preserves_newer_dates(tmp_path):
    from datetime import date, datetime, timedelta
    import json
    from src.calendar_alarms import CalendarAlarmEngine

    class SilentPlayer:
        def play_once(self, path, should_continue=None):
            return True
        def stop(self):
            pass

    path = tmp_path / "calendar.json"
    engine = CalendarAlarmEngine(path=path, player=SilentPlayer())
    days = [date.today() + timedelta(days=offset) for offset in (1, 2, 3)]
    try:
        engine.set_alarm(days[0], 7, 30)
        # Simulate a newer UI write while the worker has an older in-memory view.
        path.write_text(json.dumps({
            day.isoformat(): {"hour": 7, "minute": 30} for day in days
        }), encoding="utf-8")
        due_time = datetime.combine(days[0], datetime.min.time()).replace(hour=7, minute=31)
        with engine._changed:
            assert engine._claim_due_locked(f"{days[0].isoformat()}|07:30", due_time)
        state = engine.state()
        assert [item["date"] for item in state["alarms"]] == [day.isoformat() for day in days[1:]]
        assert state["count"] == 2
    finally:
        engine.stop_thread()


def test_home_alarm_state_includes_upcoming_calendar_alarm():
    from datetime import date, timedelta

    plugin = SleepPauseTimer()
    try:
        day = date.today() + timedelta(days=1)
        plugin.calendar.set_alarm(day, 8, 15)
        state = asyncio.run(plugin.alarm_state())
        assert state["source"] == "calendar"
        assert state["scheduled"] is True
        assert state["playing"] is False
        assert state["next_time"] == f"{day.isoformat()} 08:15"
        assert state["calendar_count"] == 1
    finally:
        plugin.alarm.stop_thread()
        plugin.calendar.stop_thread()


def test_calendar_state_exposes_all_future_alarms_in_calendar_and_alarm_tabs():
    from datetime import date, timedelta

    plugin = SleepPauseTimer()
    dates = [date.today() + timedelta(days=offset) for offset in (1, 2, 3)]
    try:
        for day in dates:
            plugin.calendar.set_alarm(day, 7, 30)
        calendar_state = asyncio.run(plugin.calendar_state())
        alarm_state = asyncio.run(plugin.alarm_state())
        assert calendar_state["count"] == 3
        assert [row["date"] for row in calendar_state["alarms"]] == [day.isoformat() for day in dates]
        assert alarm_state["calendar_count"] == 3
        assert alarm_state["calendar_next"]["date"] == dates[0].isoformat()
    finally:
        plugin.alarm.stop_thread()
        plugin.calendar.stop_thread()
        plugin.idle.stop()


def test_calendar_lists_and_cancels_one_shot_alarm_from_alarm_tab(tmp_path):
    from datetime import datetime, timedelta

    plugin = SleepPauseTimer()
    sound = tmp_path / "alarm.wav"
    sound.write_bytes(b"audio")
    trigger = datetime.now() + timedelta(minutes=5)
    try:
        plugin.alarm.set_alarm(trigger.hour, trigger.minute, str(sound), repeat=False)
        state = asyncio.run(plugin.calendar_state())
        assert any(item["source"] == "alarm" for item in state["alarms"])
        alarm = next(item for item in state["alarms"] if item["source"] == "alarm")
        cancelled = asyncio.run(plugin.calendar_cancel(alarm["date"], "alarm"))
        assert not any(item["source"] == "alarm" for item in cancelled["alarms"])
    finally:
        plugin.alarm.stop_thread()
        plugin.calendar.stop_thread()


def test_widget_cancel_stops_the_timer():
    with Harness(SleepPauseTimer()) as h:
        assert h.call_tool("start_timer", minutes=30).success
        cancelled = h.ui_call("cancel")
        assert cancelled.success, cancelled.code
        assert cancelled.json["running"] is False


def test_alarm_widget_can_set_with_saved_melody_and_interval():
    import os
    import tempfile

    with Harness(SleepPauseTimer()) as h:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(b"RIFF\x24\x00\x00\x00WAVEfmt ")
            temp_path = f.name
        try:
            assert h.call_tool("set_alarm_sound", sound_path=temp_path).success

            # Пока мелодия в виджете не выбрана, поле пустое — берётся
            # сохранённая, а не ошибка «укажите путь».
            result = h.ui_call(
                "alarm_set",
                hour=7,
                minute=30,
                repeat=True,
                interval=9,
                sound_path="",
            )
            assert result.success, result.code
            assert result.json["scheduled"] is True
            assert result.json["interval"] == 9
            assert result.json["sound_path"] == temp_path

            assert h.ui_call("alarm_cancel").json["scheduled"] is False
        finally:
            os.unlink(temp_path)


def test_stop_alarm_sound_keeps_the_schedule():
    import os
    import tempfile

    with Harness(SleepPauseTimer()) as h:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(b"RIFF\x24\x00\x00\x00WAVEfmt ")
            temp_path = f.name
        try:
            assert h.call_tool("set_alarm", time="07:30", sound_path=temp_path).success
            stopped = h.call_tool("stop_alarm_sound")
            assert stopped.success
            assert stopped.json["message"] == "Будильник не играл"
            # «Стоп» глушит звонок, но не отменяет расписание.
            assert stopped.json["state"]["scheduled"] is True
        finally:
            h.call_tool("cancel_alarm")
            os.unlink(temp_path)


def test_repeat_alarm_never_reschedules_into_the_past():
    import tempfile
    from datetime import datetime

    from src.alarm import AlarmEngine

    class SilentPlayer:
        def play_once(self, path, should_continue=None):
            return True

        def stop(self):
            pass

    engine = AlarmEngine(
        state_path=Path(tempfile.mkdtemp()) / "alarm.json",
        now_fn=lambda: datetime(2026, 9, 16, 7, 30),
        player=SilentPlayer(),
    )
    try:
        # 07:30 уже наступило — звоним завтра, а не «сейчас» в бесконечный цикл.
        assert engine._next_occurrence(7, 30) == datetime(2026, 9, 17, 7, 30)
        assert engine._next_occurrence(9, 0) == datetime(2026, 9, 16, 9, 0)
        assert engine._next_occurrence(
            7, 30, after=datetime(2026, 9, 16, 7, 30)
        ) == datetime(2026, 9, 17, 7, 30)
    finally:
        engine.stop_thread()
