import asyncio
import json
from datetime import datetime, timedelta

import pytest

from src.radio_schedule import RadioSchedule, normalize_shift, next_start, shift_end
from src.plugin import SleepPauseTimer


START = datetime(2030, 1, 7, 18, 0)  # Monday, local clock.


def entry(**changes):
    return {"name": "Вечерний эфир", "start": "18:00", "end": "20:00", "days": [0],
            "guild_id": "111111111111111111", "channel_id": "222222222222222222",
            "music": {"service": "yandex", "mode": "wave"}, **changes}


class Discord:
    def __init__(self):
        self.active = None
        self.calls = []
    def __call__(self, service, method, **params):
        assert service == "discord"
        self.calls.append((method, params))
        if method == "radio_start":
            self.active = params.copy()
        elif method == "radio_stop" and self.active and self.active["run_id"] == params["run_id"]:
            self.active = None
        return {"active": self.active, "allowed": True}


@pytest.mark.parametrize("service,mode", [("yandex", "wave"), ("vk", "library"), ("spotify", "wave"), ("spotify", "playlist")])
def test_shift_starts_once_restores_and_stops_at_exact_end(tmp_path, service, mode):
    async def run():
        now = [START - timedelta(minutes=1)]
        discord = Discord()
        schedule = RadioSchedule(tmp_path / "radio.json", lambda: now[0], discord)
        await schedule.upsert(entry(music={"service": service, "mode": mode, "playlist_id": "playlist"}))
        await schedule.tick()
        assert not discord.calls
        now[0] = START
        await schedule.tick()
        assert schedule.active and schedule.rows[0]["result"] == "В эфире"
        assert json.loads(schedule.path.read_text())["active"]["run_id"] == discord.active["run_id"]
        assert discord.active["ends_at"] == int((START + timedelta(hours=2)).timestamp())
        assert discord.active["music"]["service"] == service and discord.active["every"] == 3
        await schedule.tick()
        restored = RadioSchedule(schedule.path, lambda: now[0], discord)
        await restored.tick()
        assert sum(method == "radio_start" for method, _ in discord.calls) == 1
        now[0] = START + timedelta(hours=2)
        await restored.tick()
        assert discord.active is None and restored.active is None
        assert restored.rows[0]["result"] == "Смена завершена"
        assert restored.state()["entries"][0]["next"] == "2030-01-14T18:00:00"
    asyncio.run(run())


def test_overnight_once_and_missed_start_are_not_replayed(tmp_path):
    async def run():
        now = [START]
        discord = Discord()
        schedule = RadioSchedule(tmp_path / "radio.json", lambda: now[0], discord)
        await schedule.upsert(entry(start="23:00", end="01:00", date="2030-01-07"))
        now[0] = START.replace(hour=23)
        await schedule.tick()
        assert schedule.rows[0]["enabled"] is False
        assert discord.active["ends_at"] == int(datetime(2030, 1, 8, 1, 0).timestamp())
        now[0] = datetime(2030, 1, 8, 0, 30)
        await schedule.tick()
        assert discord.active
        now[0] = datetime(2030, 1, 8, 1, 0)
        await schedule.tick()
        assert discord.active is None and schedule.state()["entries"][0]["next"] is None
        weekly = RadioSchedule(tmp_path / "weekly.json", lambda: now[0], discord)
        await weekly.upsert(entry())
        await weekly.tick()
        assert not any(method == "radio_start" for method, _ in discord.calls[3:])
    asyncio.run(run())


def test_disable_and_remove_stop_only_owned_shift_without_restarting_same_slot(tmp_path):
    async def run():
        discord = Discord()
        schedule = RadioSchedule(tmp_path / "radio.json", lambda: START, discord)
        await schedule.upsert(entry())
        await schedule.tick()
        run_id = schedule.active["run_id"]
        await schedule.upsert({**schedule.rows[0], "enabled": False})
        assert discord.active is None and schedule.active is None
        assert ("radio_stop", {"run_id": run_id}) in discord.calls
        await schedule.upsert({**schedule.rows[0], "enabled": True})
        await schedule.tick()
        assert sum(method == "radio_start" for method, _ in discord.calls) == 1
        await schedule.remove(schedule.rows[0]["id"])
        assert schedule.rows == []
    asyncio.run(run())


def test_failed_start_error_survives_poll_and_no_duplicate_start(tmp_path):
    async def run():
        calls = []
        def failed(service, method, **params):
            calls.append(method)
            if method == "radio_start":
                raise RuntimeError("Бот уже занят")
            return {"active": None}
        schedule = RadioSchedule(tmp_path / "radio.json", lambda: START, failed)
        await schedule.upsert(entry())
        await schedule.tick()
        await schedule.tick()
        await schedule.tick()
        assert calls.count("radio_start") == 1
        assert schedule.rows[0]["result"] == "Ошибка: Бот уже занят"
        assert schedule.active is None
    asyncio.run(run())


def test_storage_failure_never_fires_external_start(tmp_path, monkeypatch):
    async def run():
        discord = Discord()
        schedule = RadioSchedule(tmp_path / "radio.json", lambda: START, discord)
        await schedule.upsert(entry())
        monkeypatch.setattr(schedule, "save", lambda: (_ for _ in ()).throw(OSError("readonly")))
        with pytest.raises(OSError): await schedule.tick()
        assert not discord.calls and schedule.active is None
        assert schedule.rows[0]["last_slot"] == ""
    asyncio.run(run())


def test_stop_connection_failure_retries_owned_stop(tmp_path):
    async def run():
        discord = Discord()
        schedule = RadioSchedule(tmp_path / "radio.json", lambda: START, discord)
        await schedule.upsert(entry())
        await schedule.tick()
        def offline(*args, **kwargs): raise RuntimeError("Нет связи")
        schedule.call = offline
        with pytest.raises(RuntimeError): await schedule.stop()
        assert schedule.active["stop_requested"]
        schedule.call = discord
        await schedule.tick()
        assert schedule.active is None and discord.active is None
    asyncio.run(run())


@pytest.mark.parametrize("changes", [{"start": "25:00"}, {"end": "18:00"}, {"days": []}, {"days": [True]},
    {"guild_id": "Nickname"}, {"channel_id": ""}, {"enabled": "true"}, {"date": "bad"}, {"every": True},
    {"music": {"service": "vk", "mode": "wave"}}, {"music": {"service": "spotify", "mode": "playlist", "playlist_id": ""}}])
def test_invalid_schedules_rejected(changes):
    with pytest.raises((ValueError, TypeError)): normalize_shift(entry(**changes), START)


def test_metadata_only_playlist_link_and_ui_routing(tmp_path, monkeypatch):
    row = normalize_shift(entry(music={"service": "spotify", "mode": "playlist", "playlist_id": "https://open.spotify.com/playlist/abc?si=secret", "token": "secret"}), START)
    assert row["music"] == {"service": "spotify", "mode": "playlist", "playlist_id": "abc"}
    assert row["skip_empty"] is True
    assert normalize_shift(entry(skip_empty=False), START)["skip_empty"] is False
    monkeypatch.setenv("SPT_ALARM_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("SPT_INTEGRATION_DIR", str(tmp_path / "integrations"))
    async def run():
        plugin = SleepPauseTimer()
        result = await plugin.radio_save(entry=entry())
        assert len(result["entries"]) == 1
        assert (await plugin.radio_save(entry=entry(start="bad")))["error"]
        assert (await plugin.radio_remove(id=result["entries"][0]["id"]))["entries"] == []
        await plugin.on_shutdown()
    asyncio.run(run())


def test_schedule_starts_after_registration_without_config_or_open_page(tmp_path, monkeypatch):
    monkeypatch.setenv("SPT_ALARM_STATE_DIR", str(tmp_path))
    async def run():
        plugin = SleepPauseTimer()
        started = asyncio.Event()
        async def scheduler():
            started.set()
            await asyncio.Event().wait()
        monkeypatch.setattr(plugin.radio_schedule, "run", scheduler)
        await plugin.get_ui_contributions()
        assert not started.is_set()
        plugin.host = object()
        await asyncio.wait_for(started.wait(), 1)
        task = plugin._radio_task
        await plugin.on_language_changed("ru")
        assert plugin._radio_task is task
        await plugin.on_shutdown()
    asyncio.run(run())
