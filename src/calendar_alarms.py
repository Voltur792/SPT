"""Persistent one-shot alarms scheduled for selected calendar dates."""
from __future__ import annotations

import json
import logging
import threading
from datetime import date, datetime
from pathlib import Path

from .alarm import AlarmError, WmpPlayer, default_state_path


def _calendar_path() -> Path:
    return default_state_path().with_name("calendar.json")


class CalendarAlarmEngine:
    """Owns one-shot calendar alarms independently of the daily alarm."""

    def __init__(self, path: Path | None = None, player: WmpPlayer | None = None):
        self.path = path or _calendar_path()
        self.player = player or WmpPlayer()
        self._lock = threading.RLock()
        self._changed = threading.Condition(self._lock)
        self._stop = False
        # Internal keys are date|HH:MM so one date can hold multiple alarms.
        self._alarms: dict[str, dict[str, int | str]] = {}
        self._playing: str | None = None
        self._error = ""
        self._load()
        self._thread = threading.Thread(target=self._run, name="spt-calendar-alarms", daemon=True)
        self._thread.start()

    def _load(self) -> None:
        self._reload_locked()

    def _reload_locked(self) -> None:
        """Refresh the in-memory calendar from the durable shared state file."""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self._alarms = {}
            return
        except (OSError, ValueError, TypeError):
            return
        if not isinstance(data, dict):
            return
        today = date.today()
        loaded: dict[str, dict[str, int | str]] = {}
        stored_rows = data.get("alarms")
        if isinstance(stored_rows, list):
            candidates = stored_rows
        else:
            # Migrate the original {date: {hour, minute}} format on read.
            candidates = [
                {"date": key, **value}
                for key, value in data.items()
                if key != "alarms" and isinstance(value, dict)
            ]
        for item in candidates:
            if not isinstance(item, dict):
                continue
            try:
                parsed = date.fromisoformat(str(item["date"]))
                hour, minute = int(item["hour"]), int(item["minute"])
                if parsed < today or not 0 <= hour <= 23 or not 0 <= minute <= 59:
                    continue
                iso_date = parsed.isoformat()
                key = f"{iso_date}|{hour:02d}:{minute:02d}"
                loaded[key] = {"date": iso_date, "hour": hour, "minute": minute}
            except (KeyError, TypeError, ValueError):
                continue
        self._alarms = loaded

    def _save_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        rows = sorted(
            self._alarms.values(),
            key=lambda item: (str(item["date"]), int(item["hour"]), int(item["minute"])),
        )
        tmp.write_text(
            json.dumps({"alarms": rows}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(self.path)

    def set_alarm(self, day: date, hour: int | None, minute: int | None) -> dict:
        if not isinstance(day, date):
            raise AlarmError("Укажите дату в календаре")
        date_key = day.isoformat()
        with self._changed:
            self._reload_locked()
            if hour is None or minute is None:
                self._alarms = {
                    key: item for key, item in self._alarms.items()
                    if item.get("date") != date_key
                }
            else:
                try:
                    hour, minute = int(hour), int(minute)
                except (TypeError, ValueError) as exc:
                    raise AlarmError("Введите время в формате ЧЧ:ММ") from exc
                at = datetime.combine(day, datetime.min.time()).replace(hour=hour, minute=minute)
                if not 0 <= hour <= 23 or not 0 <= minute <= 59:
                    raise AlarmError("Время должно быть от 00:00 до 23:59")
                if at <= datetime.now():
                    raise AlarmError("Выберите дату и время в будущем")
                key = f"{date_key}|{hour:02d}:{minute:02d}"
                self._alarms[key] = {"date": date_key, "hour": hour, "minute": minute}
            try:
                self._save_locked()
            except OSError as exc:
                raise AlarmError(f"Не удалось сохранить календарь: {exc}") from exc
            self._changed.notify_all()
            return self._state_locked()

    def cancel_alarm(self, day: date, hour: int, minute: int) -> dict:
        """Remove one exact date/time alarm while preserving other times that day."""
        if not isinstance(day, date):
            raise AlarmError("Укажите дату в календаре")
        key = f"{day.isoformat()}|{int(hour):02d}:{int(minute):02d}"
        with self._changed:
            self._reload_locked()
            self._alarms.pop(key, None)
            try:
                self._save_locked()
            except OSError as exc:
                raise AlarmError(f"Не удалось сохранить календарь: {exc}") from exc
            self._changed.notify_all()
            return self._state_locked()

    def state(self) -> dict:
        with self._lock:
            self._reload_locked()
            return self._state_locked()

    def _state_locked(self) -> dict:
        rows = sorted(
            (dict(item) for item in self._alarms.values()),
            key=lambda item: (item["date"], item["hour"], item["minute"]),
        )
        next_alarm = rows[0] if rows else None
        if next_alarm:
            next_at = datetime.fromisoformat(next_alarm["date"]).replace(
                hour=next_alarm["hour"], minute=next_alarm["minute"]
            )
            remaining = max(0, int((next_at - datetime.now()).total_seconds()))
        else:
            remaining = 0
        return {"alarms": rows, "count": len(rows), "next": next_alarm,
                "remaining_seconds": remaining, "playing": self._playing,
                "error": self._error}

    def _claim_due_locked(self, key: str, now: datetime) -> bool:
        """Claim a due alarm from the latest persisted schedule snapshot."""
        self._reload_locked()
        item = self._alarms.get(key)
        if item is None:
            return False
        try:
            at = datetime.fromisoformat(str(item["date"])).replace(
                hour=int(item["hour"]), minute=int(item["minute"])
            )
        except (KeyError, TypeError, ValueError):
            return False
        if at > now:
            return False
        self._playing = key
        self._alarms.pop(key, None)
        try:
            self._save_locked()
        except OSError as exc:
            self._error = str(exc)
        return True

    def _run(self) -> None:
        log = logging.getLogger(__name__)
        while True:
            with self._changed:
                if self._stop:
                    return
                # Another UI call or a briefly overlapping plugin process may
                # have changed the shared file while this thread was waiting.
                self._reload_locked()
                now = datetime.now()
                due = []
                for key, item in self._alarms.items():
                    try:
                        at = datetime.fromisoformat(str(item["date"])).replace(
                            hour=int(item["hour"]), minute=int(item["minute"])
                        )
                        due.append((at, key))
                    except (KeyError, TypeError, ValueError):
                        continue
                if not due:
                    self._changed.wait(timeout=60)
                    continue
                at, key = min(due)
                delay = (at - now).total_seconds()
                if delay > 0:
                    self._changed.wait(timeout=min(delay, 3600))
                    continue
                # Reload once more immediately before writing: firing today's
                # alarm must not erase later dates added since the previous tick.
                if not self._claim_due_locked(key, datetime.now()):
                    continue
                sound = ""
            try:
                # The daily engine owns the configured melody; plugin wires it
                # in after construction with set_sound_provider.
                sound = self._sound_provider() if hasattr(self, "_sound_provider") else ""
                if not sound:
                    raise AlarmError("Сначала выберите мелодию в настройках")
                self.player.play_once(sound)
            except Exception as exc:
                self._error = str(exc)
                log.exception("Calendar alarm playback failed")
            finally:
                with self._changed:
                    self._playing = None
                    self._changed.notify_all()

    def set_sound_provider(self, provider) -> None:
        self._sound_provider = provider

    def stop_playback(self) -> None:
        self.player.stop()

    def stop_thread(self) -> None:
        with self._changed:
            self._stop = True
            self._changed.notify_all()
        self.player.stop()
        self._thread.join(timeout=1.5)
