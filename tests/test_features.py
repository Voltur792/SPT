import asyncio
import json
import threading
import time
from datetime import datetime, timedelta

import pytest

from src.actions import music_sound, decode_music, parse_hotkey, AlarmPlayer
from src.alarm import normalize_sound_path
from src.home_schedule import HomeSchedule, next_occurrence
from src.integrations import IntegrationServer, integration_call
from src.plugin import SleepPauseTimer, TimerEngine
from src.desktop_widgets import DesktopWidgets
from astra_plugin_sdk.testing import Harness


@pytest.fixture(autouse=True)
def isolation(tmp_path, monkeypatch):
    monkeypatch.setenv("SPT_ALARM_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("SPT_INTEGRATION_DIR", str(tmp_path / "integrations"))


def test_custom_actions_validate_before_creating_timer():
    with Harness(SleepPauseTimer()) as h:
        for action, params in [("hotkey", {"keys": "Ctrl+unknown"}), ("close_app", {}), ("astra_command", {}), ("music", {})]:
            assert not h.call_tool("start_timer", seconds=10, action=action, action_params=params).success
        assert not h.call_tool("timer_status").json["running"]
        result = h.call_tool("start_timer", minutes=1, action="hotkey", action_params={"keys": "Ctrl+Alt+P"}, name="Перерыв")
        assert result.success
        assert result.json["state"]["name"] == "Перерыв"
        h.call_tool("cancel_timer", timer_id=result.json["timer_id"])


def test_custom_completion_errors_visible_without_real_actions():
    def failed(action, params):
        raise RuntimeError("Нет подключения")
    engine = TimerEngine(failed)
    engine.TICK = .005
    engine.start(1, "music_pause", "")
    with engine._lock:
        engine._remaining = .001
    for _ in range(100):
        if engine.state()["outcome"] == "error":
            break
        time.sleep(.01)
    assert engine.state()["outcome"] == "error"
    assert "Нет подключения" in engine.state()["info"]
    engine.stop_thread()


def test_music_sound_roundtrip_and_validation():
    track = {"service": "vk", "track_id": "42", "extra": {"owner_id": "5"}, "title": "Музыка", "secret": "do not persist"}
    sound = music_sound(track)
    assert normalize_sound_path(sound) == sound
    decoded = decode_music(sound)
    assert decoded["title"] == "Музыка"
    assert "secret" not in decoded
    with pytest.raises(ValueError):
        decode_music("music://bad")
    assert parse_hotkey("Ctrl+Alt+P") == [17, 18, 80]


def test_next_occurrence_weekday_and_once():
    now = datetime(2030, 1, 7, 18, 0)  # Monday
    assert next_occurrence({"time": "17:00", "days": [0]}, now) == datetime(2030, 1, 14, 17, 0)
    assert next_occurrence({"time": "19:00", "date": "2030-01-07"}, now) == datetime(2030, 1, 7, 19, 0)
    assert next_occurrence({"time": "17:00", "date": "2030-01-07"}, now) is None


def test_successful_once_schedule_is_removed_from_memory_and_disk(tmp_path, monkeypatch):
    calls=[]
    monkeypatch.setattr("src.home_schedule.integration_call", lambda *a, **kw: calls.append(kw))
    store=HomeSchedule(tmp_path / "once.json")
    store.upsert({"kind":"scenario", "target_id":"scene", "time":"17:00", "date":"2030-01-07"})
    now=datetime(2030,1,7,17,0)
    asyncio.run(store.tick(now))
    assert store.state()["entries"] == []
    assert json.loads(store.path.read_text(encoding="utf-8")) == []
    asyncio.run(HomeSchedule(store.path).tick(now))
    assert len(calls)==1


def test_existing_completed_once_schedules_are_cleaned_without_losing_other_rows(tmp_path):
    path=tmp_path / "old.json"
    rows=[{"id":"completed", "kind":"scenario", "target_id":"s", "time":"17:00", "date":"2020-01-07", "enabled":False, "last_slot":"2020-01-07T17:00", "result":"Выполнено"}]
    rows += [{**rows[0], "id":"failed", "result":"Ошибка: нет связи"},
             {**rows[0], "id":"disabled", "result":"", "last_slot":""},
             {**rows[0], "id":"daily", "date":"", "days":[0,1,2,3,4,5,6], "enabled":True},
             {**rows[0], "id":"future", "date":"2030-01-07", "enabled":True, "result":"", "last_slot":""}]
    path.write_text(json.dumps(rows,ensure_ascii=False),encoding="utf-8")
    store=HomeSchedule(path)
    assert [r["id"] for r in store.rows] == ["failed","disabled","daily","future"]
    assert json.loads(path.read_text(encoding="utf-8")) == store.rows


def test_failed_once_schedule_remains_visible(tmp_path, monkeypatch):
    def failed(*args,**kwargs):raise RuntimeError("Нет связи")
    monkeypatch.setattr("src.home_schedule.integration_call",failed)
    store=HomeSchedule(tmp_path / "failed.json")
    store.upsert({"kind":"scenario","target_id":"s","time":"17:00","date":"2030-01-07"})
    asyncio.run(store.tick(datetime(2030,1,7,17,0)))
    restored=HomeSchedule(store.path)
    assert restored.rows[0]["result"] == "Ошибка: Нет связи"
    assert restored.rows[0]["enabled"] is False


def test_once_cleanup_storage_failure_retries_without_replaying_action(tmp_path, monkeypatch):
    calls=[]
    monkeypatch.setattr("src.home_schedule.integration_call",lambda *a,**kw:calls.append(1))
    store=HomeSchedule(tmp_path / "retry.json")
    store.upsert({"kind":"scenario","target_id":"s","time":"17:00","date":"2030-01-07"})
    save=store.save
    writes=[]
    def fail_cleanup():
        writes.append(1)
        if len(writes)==3:raise OSError("Disk full")
        save()
    monkeypatch.setattr(store,"save",fail_cleanup)
    now=datetime(2030,1,7,17,0)
    with pytest.raises(OSError):asyncio.run(store.tick(now))
    assert len(store.rows)==1
    asyncio.run(store.tick(now))
    assert store.rows == []
    assert calls == [1]


def test_schedule_persists_claim_and_does_not_repeat_after_restart(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr("src.home_schedule.integration_call", lambda *args, **kwargs: calls.append((args, kwargs)))
    store = HomeSchedule(tmp_path / "schedule.json")
    store.upsert({"kind": "scenario", "target_id": "scene", "name": "Вечер", "time": "17:00", "days": list(range(7))})
    now = datetime(2030, 1, 7, 17, 0, 5)
    asyncio.run(store.tick(now))
    asyncio.run(store.tick(now))
    other = HomeSchedule(store.path)
    asyncio.run(other.tick(now))
    assert len(calls) == 1
    asyncio.run(other.tick(now+timedelta(days=1)))
    assert len(calls) == 2
    assert other.state()["entries"][0]["result"] == "Выполнено"


def test_schedule_device_failure_and_invalid_input(tmp_path, monkeypatch):
    store = HomeSchedule(tmp_path / "schedule.json")
    for entry in [{"kind": "scenario", "target_id": "s", "time": "25:00", "days": [0]},
                  {"kind": "scenario", "target_id": "s", "time": "17:00", "days": []},
                  {"kind": "device", "target_id": "d", "time": "17:00", "days": [0]}]:
        with pytest.raises(ValueError):
            store.upsert(entry)
    def failed(*args, **kwargs):
        assert kwargs["value"] is False
        raise RuntimeError("Устройство недоступно")
    monkeypatch.setattr("src.home_schedule.integration_call", failed)
    store.upsert({"kind": "device", "target_id": "d", "time": "17:00", "days": [0],
                  "capability_type": "devices.capabilities.on_off", "instance": "on", "value": False})
    asyncio.run(store.tick(datetime(2030, 1, 7, 17, 0)))
    assert "Устройство недоступно" in store.rows[0]["result"]


def test_local_bridge_auth_and_allowlist(tmp_path):
    import urllib.request
    import urllib.error
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever)
    thread.start()
    server = IntegrationServer("test", {"echo": lambda text: {"text": text}})
    try:
        server.start(loop)
        assert integration_call("test", "echo", text="Привет") == {"text": "Привет"}
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{server.server.server_port}/api", data=b'{}'))
        assert exc.value.code == 403
        with pytest.raises(RuntimeError):
            integration_call("test", "save_token", token="not allowed")
    finally:
        server.close()
        loop.call_soon_threadsafe(loop.stop)
        thread.join()
        loop.close()
    assert not (tmp_path / "integrations" / "test.json").exists()


def test_desktop_settings_independent_and_durable(monkeypatch):
    widgets = DesktopWidgets()
    widgets.set("timer", {"enabled": True, "rounded": False, "transparency": 45})
    widgets.set("alarm", {"enabled": False, "rounded": True, "transparency": 20})
    restored = DesktopWidgets().state()
    assert restored["timer"]["enabled"]
    assert not restored["timer"]["rounded"]
    assert restored["timer"]["transparency"] == 45
    assert not restored["alarm"]["enabled"]
    with pytest.raises(ValueError):
        widgets.set("alarm", {"transparency": 100})
    assert widgets.state()["alarm"]["transparency"] == 20


def test_alarm_stop_only_controls_owned_music_revision(monkeypatch):
    calls = []
    def fake(name, method, **params):
        calls.append((method, params))
        return {"revision": 3, "status": "paused"}
    monkeypatch.setattr("src.actions.integration_call", fake)
    player = AlarmPlayer()
    player.play_once(music_sound({"service": "yandex", "track_id": "9"}))
    assert calls[0][0] == "play"
    deadline = time.monotonic() + 1
    while calls[-1][0] != "stop" and time.monotonic() < deadline:
        time.sleep(.005)
    assert calls[-1] == ("stop", {"revision": 3})


def test_astra_command_emits_named_event_without_chat():
    from astra_plugin_sdk.testing.recording_host import RecordingHost
    async def run():
        plugin = SleepPauseTimer()
        host = RecordingHost()
        plugin.host = host
        plugin._integration_loop = asyncio.get_running_loop()
        try:
            result = await asyncio.to_thread(plugin._execute_custom, "astra_command", {"command_key": "вечер"})
            assert "Событие отправлено" in result
            events = host.fired_triggers()
            assert len(events) == 1
            assert events[0].trigger_type == "timer_command"
            assert json.loads(events[0].payload_json) == {"command_key": "вечер"}
            assert not host.chat_messages()
        finally:
            await plugin.on_shutdown()
    asyncio.run(run())


def test_schedule_does_not_execute_if_claim_cannot_be_saved(tmp_path, monkeypatch):
    calls = []
    store = HomeSchedule(tmp_path / "schedule.json")
    store.upsert({"kind": "scenario", "target_id": "s", "time": "17:00", "days": [0]})
    monkeypatch.setattr("src.home_schedule.integration_call", lambda *args, **kw: calls.append(1))
    monkeypatch.setattr(store, "save", lambda: (_ for _ in ()).throw(OSError("Disk full")))
    with pytest.raises(OSError):
        asyncio.run(store.tick(datetime(2030, 1, 7, 17, 0)))
    assert not calls
    assert store.rows[0]["last_slot"] == ""


def test_corrupt_schedule_error_is_visible(tmp_path):
    path = tmp_path / "schedule.json"
    path.write_text('{"unexpected":"format"}', encoding="utf-8")
    store = HomeSchedule(path)
    asyncio.run(store.tick(datetime(2030, 1, 7, 17, 0)))
    assert "прочитать" in store.state()["storage_error"]


@pytest.mark.parametrize("date,days", [("2030-01-07", list(range(7))), ("", list(range(7))), ("", [0, 2, 4])])
def test_schedule_entries_survive_real_sdk_ui_response(date, days):
    with Harness(SleepPauseTimer()) as h:
        entry = {"kind": "scenario", "target_id": "scene", "time": "17:00", "date": date, "days": days}
        saved = h.ui_call("home_schedule_save", entry=entry)
        assert saved.success, saved.error
        assert len(saved.json["entries"]) == 1
        state = h.ui_call("home_schedule_state")
        assert state.json["entries"][0]["id"] == saved.json["entries"][0]["id"]
        deleted = h.ui_call("home_schedule_remove", id=saved.json["entries"][0]["id"])
        assert deleted.json["entries"] == []


def test_completed_and_cancelled_timers_leave_active_list():
    with Harness(SleepPauseTimer()) as h:
        assert h.ui_call("state").json["timers"] == []
        first = h.call_tool("start_timer", minutes=5).json["timer_id"]
        second = h.call_tool("start_timer", minutes=10).json["timer_id"]
        h.call_tool("cancel_timer", timer_id=first)
        assert [t["id"] for t in h.ui_call("state").json["timers"]] == [second]
        engine = h.plugin.timers.get(second)
        with engine._lock:
            engine._active = False
            engine._outcome = "done"
        assert h.ui_call("state").json["timers"] == []


@pytest.mark.parametrize("action,params", [
    ("hotkey", {"keys": "Win+D"}), ("close_app", {"window": "Test Window"}),
    ("astra_command", {"command_key": "вечер"}), ("music_pause", {}),
    ("music", {"service": "vk", "track_id": "42", "extra": {"owner_id": "5"}}),
])
def test_idle_custom_actions_persist_resume_and_dispatch(action, params, monkeypatch):
    plugin = SleepPauseTimer()
    calls = []
    monkeypatch.setattr(plugin, "_execute_custom", lambda a, p: calls.append((a, p)))
    with Harness(plugin) as h:
        result = h.ui_call("idle_start", minutes=30, action=action, action_params=params)
        assert result.success, result.error
        assert result.json["action_params"] == params
        assert h.ui_call("idle_stop").json["running"] is False
        assert h.call_tool("resume_idle_monitor").json["state"]["action_params"] == params
        plugin._execute_idle(action, "", params)
        assert calls == [(action, params)]
        assert SleepPauseTimer().local_settings["idle_action_params"] == params


def test_alarm_stop_does_not_wait_for_music_endpoint(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    def blocking(*args, **params):
        entered.set()
        release.wait(2)
        return {}
    monkeypatch.setattr("src.actions.integration_call", blocking)
    player = AlarmPlayer()
    player.revision = 5
    start = time.monotonic()
    player.stop()
    assert time.monotonic() - start < .3
    assert entered.wait(1)
    release.set()


def test_alarm_cancel_during_slow_start_stops_late_owned_revision(monkeypatch):
    entered, release, stopped = threading.Event(), threading.Event(), threading.Event()
    def bridge(name, method, **params):
        if method == "play":
            entered.set()
            release.wait(2)
            return {"revision": 11}
        assert method == "stop" and params["revision"] == 11
        stopped.set()
        return {}
    monkeypatch.setattr("src.actions.integration_call", bridge)
    player = AlarmPlayer()
    thread = threading.Thread(target=player.play_once, args=(music_sound({"service": "yandex", "track_id": "9"}),))
    thread.start()
    try:
        assert entered.wait(1)
        player.stop()
        release.set()
        thread.join(2)
        assert stopped.wait(1)
        assert not thread.is_alive()
    finally:
        release.set()
        thread.join(2)
