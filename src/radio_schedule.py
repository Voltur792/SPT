"""Opt-in radio shifts: durable local-clock starts and owned Discord sessions."""
from __future__ import annotations

import asyncio
import copy
import json
import math
import re
import uuid
from datetime import date, datetime, timedelta
from urllib.parse import urlsplit

from .alarm import default_state_path
from .integrations import integration_call


def normalize_shift(value, now=None, restoring=False):
    if not isinstance(value, dict):
        raise ValueError("Некорректная смена")
    now = now or datetime.now()
    row = {key: value[key] for key in ("id", "name", "enabled", "start", "end", "date", "days", "guild_id", "channel_id", "music", "every", "when", "announce_next", "skip_empty") if key in value}
    for key in ("start", "end"):
        if not isinstance(row.get(key), str) or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", row[key]):
            raise ValueError("Укажите начало и конец в формате ЧЧ:ММ")
    if row["start"] == row["end"]:
        raise ValueError("Начало и конец смены должны различаться")
    for key in ("guild_id", "channel_id"):
        if not isinstance(row.get(key), str) or not re.fullmatch(r"[0-9]{17,20}", row[key]):
            raise ValueError("Укажите ID сервера и голосового канала Discord")
    row["name"] = str(row.get("name") or "Радиоэфир Astra")[:100]
    row.setdefault("enabled", True)
    if type(row["enabled"]) is not bool:
        raise ValueError("Некорректное состояние смены")
    if row.get("date"):
        date.fromisoformat(row["date"])
        row["days"] = []
        if not restoring and row["enabled"] and row["date"] + "T" + row["start"] < now.strftime("%Y-%m-%dT%H:%M"):
            raise ValueError("Выберите дату и время начала в будущем")
    else:
        row["date"] = ""
        days = row.get("days")
        if not isinstance(days, list) or not days or any(type(day) is not int or day not in range(7) for day in days):
            raise ValueError("Выберите дни недели")
        row["days"] = sorted(set(days))
    music = row.get("music")
    modes = {"yandex": ("wave", "playlist"), "vk": ("library", "playlist"), "spotify": ("wave", "playlist")}
    if not isinstance(music, dict) or music.get("mode") not in modes.get(music.get("service"), ()):
        raise ValueError("Выберите плейлист, волну, треки VK или микс Spotify")
    playlist = music.get("playlist_id", "")
    if not isinstance(playlist, str):
        raise ValueError("Некорректный плейлист")
    playlist = playlist.strip()
    if playlist.startswith("https://") and music["service"] == "spotify":
        url = urlsplit(playlist)
        if url.netloc != "open.spotify.com" or not re.fullmatch(r"/playlist/[a-zA-Z0-9]+/?", url.path):
            raise ValueError("Укажите ссылку на плейлист open.spotify.com")
        playlist = url.path.strip("/").split("/")[1]
    if music["mode"] == "playlist" and (not playlist or len(playlist) > 300 or re.search(r"[\r\n\x00]", playlist)):
        raise ValueError("Выберите плейлист или укажите его ID")
    row["music"] = {"service": music["service"], "mode": music["mode"], "playlist_id": playlist if music["mode"] == "playlist" else ""}
    row["every"] = row.get("every", 3)
    if type(row["every"]) is not int or row["every"] not in (1, 3, 5):
        raise ValueError("Выберите частоту подводок: 1, 3 или 5 песен")
    row["when"] = "start" if row.get("when") == "start" else "end"
    row["announce_next"] = row.get("announce_next", True) is True
    row["skip_empty"] = row.get("skip_empty", True) is True
    if row.get("id") and (not isinstance(row["id"], str) or not re.fullmatch(r"[a-f0-9]{32}", row["id"])):
        raise ValueError("Некорректный идентификатор смены")
    if restoring and not row.get("id"):
        raise ValueError("В смене отсутствует идентификатор")
    return row


def shift_end(row, start):
    hour, minute = map(int, row["end"].split(":"))
    end = start.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return end + timedelta(days=1) if end <= start else end


def next_start(row, now):
    if not row["enabled"]:
        return None
    hour, minute = map(int, row["start"].split(":"))
    for offset in range(8):
        day = date.fromisoformat(row["date"]) if row["date"] else (now + timedelta(days=offset)).date()
        at = datetime.combine(day, datetime.min.time()).replace(hour=hour, minute=minute)
        if at > now and (row["date"] or at.weekday() in row["days"]) and at.strftime("%Y-%m-%dT%H:%M") != row.get("last_slot"):
            return at
        if row["date"]:
            return None
    return None


class RadioSchedule:
    def __init__(self, path=None, now_fn=None, call=None):
        self.path = path or default_state_path().with_name("radio-shifts.json")
        self.now = now_fn or datetime.now
        self.call = call or integration_call
        self.rows, self.active = [], None
        self.storage_error = ""
        self.error = ""
        self.lock = asyncio.Lock()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("entries"), list) or len(data["entries"]) > 50:
                raise ValueError("Некорректный формат смен")
            self.rows = [{**normalize_shift(row, restoring=True), "last_slot": str(row.get("last_slot", "")), "result": str(row.get("result", ""))[:320]} for row in data["entries"]]
            active = data.get("active")
            if active is not None:
                if not isinstance(active, dict) or not re.fullmatch(r"[a-f0-9]{32}", str(active.get("run_id", ""))) or not re.fullmatch(r"[a-f0-9]{32}", str(active.get("shift_id", ""))) or type(active.get("ends_at")) not in (int, float) or not math.isfinite(active["ends_at"]):
                    raise ValueError("Некорректное состояние эфира")
                self.active = {key: active[key] for key in ("run_id", "shift_id", "ends_at", "stop_requested") if key in active}
        except FileNotFoundError:
            pass
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.rows, self.active = [], None
            self.storage_error = f"Не удалось прочитать смены: {exc}"

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"version": 1, "entries": self.rows, "active": self.active}, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)
        self.storage_error = ""

    def state(self):
        entries = []
        for row in self.rows:
            start = next_start(row, self.now())
            entries.append({**row, "next": start.isoformat() if start else None, "next_end": shift_end(row, start).isoformat() if start else None})
        return {"entries": entries, "active": copy.deepcopy(self.active), "storage_error": self.storage_error, "runtime_error": self.error}

    async def upsert(self, value):
        row = normalize_shift(value, self.now())
        existing = next((r for r in self.rows if r["id"] == row.get("id")), None)
        if not existing and len(self.rows) >= 50:
            raise ValueError("Можно сохранить до 50 смен")
        row["id"] = existing["id"] if existing else uuid.uuid4().hex
        row["last_slot"] = existing.get("last_slot", "") if existing else ""
        row["result"] = existing.get("result", "") if existing else ""
        previous = self.rows
        self.rows = [r for r in self.rows if r["id"] != row["id"]] + [row]
        try:
            self.save()
        except OSError:
            self.rows = previous
            raise
        if self.active and self.active["shift_id"] == row["id"]:
            await self.stop()
        return self.state()

    async def remove(self, id):
        previous = self.rows
        self.rows = [r for r in self.rows if r["id"] != id]
        try:
            self.save()
        except OSError:
            self.rows = previous
            raise
        if self.active and self.active["shift_id"] == id:
            await self.stop()
        return self.state()

    def _result(self, shift_id, text):
        row = next((r for r in self.rows if r["id"] == shift_id), None)
        if row:
            row["result"] = text[:320]

    async def stop(self):
        active = self.active
        if not active:
            return self.state()
        active["stop_requested"] = True
        try:
            self.save()
        finally:
            await asyncio.to_thread(self.call, "discord", "radio_stop", run_id=active["run_id"])
        if self.active is active:
            self.active = None
            self._result(active["shift_id"], "Смена завершена")
            self.save()
        return self.state()

    async def tick(self):
        async with self.lock:
            if self.storage_error:
                return
            now = self.now()
            if self.active:
                active = self.active
                if active.get("stop_requested") or now.timestamp() >= active["ends_at"]:
                    await self.stop()
                else:
                    state = await asyncio.to_thread(self.call, "discord", "radio_state")
                    if self.active is not active:
                        return
                    remote = state.get("active") if isinstance(state, dict) else None
                    if not remote or remote.get("run_id") != active["run_id"]:
                        self.active = None
                        row = next((r for r in self.rows if r["id"] == active["shift_id"]), None)
                        if not row or not row.get("result", "").startswith("Ошибка:"):
                            self._result(active["shift_id"], "Эфир завершён или прерван в Discord")
                        self.save()
                return
            slot = now.strftime("%Y-%m-%dT%H:%M")
            for row in list(self.rows):
                if not row["enabled"] or row.get("last_slot") == slot or row["start"] != now.strftime("%H:%M"):
                    continue
                if row["date"] and row["date"] != now.date().isoformat() or not row["date"] and now.weekday() not in row["days"]:
                    continue
                backup = copy.deepcopy((self.rows, self.active))
                row["last_slot"] = slot
                if row["date"]:
                    row["enabled"] = False
                end = shift_end(row, now.replace(second=0, microsecond=0))
                active = {"run_id": uuid.uuid4().hex, "shift_id": row["id"], "ends_at": int(end.timestamp())}
                self.active = active
                row["result"] = "Подключение к эфиру…"
                try:
                    self.save()  # Claim durably before any external action.
                except OSError:
                    self.rows, self.active = backup
                    raise
                try:
                    result = await asyncio.to_thread(self.call, "discord", "radio_start", **active,
                        **{key: row[key] for key in ("name", "guild_id", "channel_id", "music", "every", "when", "announce_next", "skip_empty")})
                    if self.active is not active:
                        return
                    remote = result.get("active") if isinstance(result, dict) else None
                    if not remote or remote.get("run_id") != active["run_id"]:
                        raise RuntimeError("Discord не подтвердил начало смены")
                    self._result(row["id"], "В эфире")
                except Exception as exc:
                    self._result(row["id"], "Ошибка: " + str(exc))
                    # A timed-out start can still finish remotely. Retain its ID
                    # until the next state check or owned stop at the deadline.
                self.save()
                return

    async def run(self):
        while True:
            try:
                await self.tick()
                self.error = ""
            except Exception as exc:
                self.error = str(exc)[:320]
            await asyncio.sleep(5)
