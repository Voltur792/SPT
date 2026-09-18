from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable


class AlarmError(ValueError):
    pass


def default_state_path() -> Path:
    base = os.environ.get("SPT_ALARM_STATE_DIR")
    if not base:
        base = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "sleep-pause-timer" / "alarm.json"


class AlarmStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> dict:
        try:
            with self.path.open("r", encoding="utf-8") as file:
                data = json.load(file)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError, TypeError):
            return {}

    def save(self, payload: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.path)


def normalize_sound_path(path: str) -> str:
    raw = str(path or "").strip()
    if not raw:
        raise AlarmError("Укажите путь к аудиофайлу")
    expanded = os.path.expandvars(os.path.expanduser(raw))
    candidate = Path(expanded)
    if not candidate.is_file():
        raise AlarmError(f"Аудиофайл не найден: {expanded}")
    return str(candidate)


class WmpPlayer:
    """Windows Media Player (legacy wmplayer.exe) через subprocess — надежно играет MP3, WAV, WMA, M4A на Windows."""
    
    def __init__(self):
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._process = None
        self._wmp_path = self._find_wmplayer()
        
    def _find_wmplayer(self) -> str:
        """Найти путь к wmplayer.exe в стандартных местах Windows."""
        candidates = [
            r"C:\Program Files (x86)\Windows Media Player\wmplayer.exe",
            r"C:\Program Files\Windows Media Player\wmplayer.exe",
        ]
        for path in candidates:
            if os.path.exists(path):
                return path
        # Фоллбэк — полагаемся на PATH (редко работает)
        return "wmplayer.exe"
        
    def play_once(self, path: str, should_continue: Callable[[], bool] | None = None) -> bool:
        log = logging.getLogger(__name__)
        
        if '"' in path:
            raise AlarmError("Путь к аудиофайлу не должен содержать кавычки")
        
        if not Path(path).exists():
            raise AlarmError(f"Аудиофайл не найден: {path}")
            
        self._stop_event.clear()
        
        try:
            # Используем wmplayer.exe (legacy Windows Media Player) с флагами /play /close
            # /play — начать воспроизведение сразу
            # /close — закрыть после завершения
            # Путь с пробелами/юникодом передаётся как есть
            cmd = [self._wmp_path, "/play", "/close", path]
            
            log.info("Starting wmplayer.exe: %s", " ".join(cmd))
            
            # Запускаем процесс
            creation_flags = 0
            if sys.platform == "win32":
                creation_flags = subprocess.CREATE_NO_WINDOW
            
            proc = subprocess.Popen(
                cmd,
                creationflags=creation_flags,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            
            self._process = proc
            log.info("Started wmplayer.exe PID=%d for: %s", proc.pid, path)
            
            # Ждём завершения процесса или стоп-события
            while not self._stop_event.wait(0.2):
                if should_continue is not None:
                    try:
                        allowed = should_continue()
                    except Exception:
                        allowed = False
                    if not allowed:
                        break
                # Проверяем, не завершился ли процесс сам
                ret = proc.poll()
                if ret is not None:
                    log.info("WMP process exited with code %d", ret)
                    break
                    
            return True
        finally:
            self.stop()
            
    def stop(self) -> None:
        self._stop_event.set()
        if self._process:
            try:
                if self._process.poll() is None:
                    self._process.terminate()
                    self._process.wait(timeout=2)
            except Exception:
                pass
            self._process = None


class AlarmEngine:
    def __init__(
        self,
        state_path: Path | None = None,
        now_fn: Callable[[], datetime] | None = None,
        player: WmpPlayer | None = None,
    ):
        self._store = AlarmStore(state_path or default_state_path())
        self._player = player or WmpPlayer()
        self._now = now_fn or datetime.now
        self._condition = threading.Condition(threading.RLock())
        self._generation = 0
        self._thread: threading.Thread | None = None
        self._scheduled = False
        self._playing = False
        self._hour = 0
        self._minute = 0
        self._repeat = True
        self._interval = 5
        self._sound_path = ""
        self._next_trigger: datetime | None = None
        self._outcome = ""
        self._info = ""
        self._storage_error = ""
        with self._condition:
            self._restore_locked()

    def _restore_locked(self) -> None:
        data = self._store.load()
        sound_path = str(data.get("sound_path", "") or "")
        self._sound_path = sound_path
        alarm = data.get("alarm")
        if not isinstance(alarm, dict):
            return
        try:
            hour = int(alarm["hour"])
            minute = int(alarm["minute"])
            if not 0 <= hour <= 23 or not 0 <= minute <= 59:
                raise ValueError
            repeat = bool(alarm.get("repeat", True))
            interval = int(alarm.get("interval", 5))
            if interval < 1:
                interval = 5
        except (KeyError, TypeError, ValueError):
            return
        self._scheduled = True
        self._hour = hour
        self._minute = minute
        self._repeat = repeat
        self._interval = interval
        self._next_trigger = self._next_occurrence(hour, minute)
        self._info = "будильник восстановлен"
        self._start_thread_locked()

    def _next_occurrence(
        self,
        hour: int,
        minute: int,
        after: datetime | None = None,
    ) -> datetime:
        """Ближайшее HH:MM строго после `after` (по умолчанию — после «сейчас»).

        Интервал повтора здесь ни при чём: он задаёт паузу между звонками уже
        зазвонившего будильника (см. _run), а не шаг расписания.
        """
        base = after if after is not None else self._now()
        candidate = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= base:
            candidate += timedelta(days=1)
        return candidate

    def _payload_locked(self) -> dict:
        if not self._scheduled:
            return {"sound_path": self._sound_path}
        return {
            "sound_path": self._sound_path,
            "alarm": {
                "hour": self._hour,
                "minute": self._minute,
                "repeat": self._repeat,
                "interval": self._interval,
            },
        }

    def _save_locked(self) -> None:
        try:
            self._store.save(self._payload_locked())
            self._storage_error = ""
        except OSError as exc:
            self._storage_error = str(exc)

    def _start_thread_locked(self) -> None:
        self._generation += 1
        thread = threading.Thread(target=self._run, daemon=True)
        self._thread = thread
        thread.start()

    def set_alarm(self, hour: int, minute: int, sound_path: str, repeat: bool = True, interval: int = 5) -> dict:
        normalized_path = normalize_sound_path(sound_path)
        hour = int(hour)
        minute = int(minute)
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise AlarmError("Время будильника должно быть в диапазоне 00:00–23:59")
        interval = max(1, int(interval))
        with self._condition:
            self._generation += 1
            try:
                self._player.stop()
            except Exception:
                pass
            self._scheduled = True
            self._playing = False
            self._hour = hour
            self._minute = minute
            self._repeat = bool(repeat)
            self._interval = interval
            self._sound_path = normalized_path
            self._next_trigger = self._next_occurrence(hour, minute)
            self._outcome = ""
            self._info = "будильник установлен"
            self._storage_error = ""
            self._save_locked()
            self._start_thread_locked()
            return self._state_locked()

    def set_sound(self, sound_path: str) -> dict:
        normalized_path = normalize_sound_path(sound_path)
        with self._condition:
            self._sound_path = normalized_path
            self._storage_error = ""
            self._save_locked()
            return self._state_locked()

    def cancel(self) -> dict:
        with self._condition:
            was_active = self._scheduled or self._playing
            self._generation += 1
            try:
                self._player.stop()
            except Exception:
                pass
            self._scheduled = False
            self._playing = False
            self._next_trigger = None
            self._outcome = "cancelled" if was_active else ""
            self._info = "будильник отменён" if was_active else "будильник не установлен"
            self._storage_error = ""
            self._save_locked()
            self._condition.notify_all()
            return self._state_locked()

    def stop_playback(self) -> dict:
        """Глушит текущий звонок, не отменяя расписание.

        Повторяющийся будильник остаётся и звонит завтра в то же время,
        одиночный после состоявшегося звонка больше не активен.
        """
        with self._condition:
            was_playing = self._playing
            self._generation += 1
            try:
                self._player.stop()
            except Exception:
                pass
            self._playing = False
            self._outcome = "stopped" if was_playing else ""
            rescheduled = False
            if was_playing and self._scheduled:
                if self._repeat:
                    self._next_trigger = self._next_occurrence(
                        self._hour,
                        self._minute,
                        after=self._now(),
                    )
                    rescheduled = True
                    self._info = "воспроизведение остановлено; следующий звонок завтра"
                else:
                    self._scheduled = False
                    self._next_trigger = None
                    self._info = "воспроизведение остановлено"
            else:
                self._info = "воспроизведение остановлено" if was_playing else "будильник не играл"
            self._storage_error = ""
            self._save_locked()
            self._condition.notify_all()
            if rescheduled:
                self._start_thread_locked()
            return self._state_locked()

    def _playback_allowed(self, generation: int) -> bool:
        with self._condition:
            return (
                generation == self._generation
                and self._scheduled
                and self._playing
            )

    def _run(self) -> None:
        log = logging.getLogger(__name__)
        with self._condition:
            generation = self._generation
        log.info("Alarm thread started, generation=%d", generation)
        while True:
            with self._condition:
                if generation != self._generation or not self._scheduled:
                    log.info("Alarm thread exiting: generation mismatch or not scheduled")
                    return
                if self._playing:
                    self._condition.wait()
                    continue
                now = self._now()
                trigger = self._next_trigger
                if trigger is None:
                    self._scheduled = False
                    log.warning("Alarm trigger is None, stopping thread")
                    return
                delay = (trigger - now).total_seconds()
                if delay > 0:
                    log.debug("Alarm waiting %.1f seconds until %s", delay, trigger)
                    self._condition.wait(timeout=delay)
                    continue
                self._playing = True
                self._outcome = "fired"
                self._info = "будильник сработал"
                hour = self._hour
                minute = self._minute
                repeat = self._repeat
                interval = self._interval
                sound_path = self._sound_path
                log.info("Alarm firing at %s, sound=%s", now.strftime("%H:%M:%S"), sound_path)
            try:
                while self._playback_allowed(generation):
                    self._player.play_once(
                        sound_path,
                        lambda: self._playback_allowed(generation),
                    )
                    if not repeat or not self._playback_allowed(generation):
                        break
                    with self._condition:
                        self._condition.wait(timeout=interval * 60)
            except Exception as exc:
                with self._condition:
                    if generation != self._generation:
                        return
                    self._playing = False
                    self._scheduled = False
                    self._next_trigger = None
                    self._outcome = "error"
                    self._info = f"ошибка воспроизведения: {exc}"
                    self._storage_error = ""
                    self._save_locked()
                    log.error("Alarm playback error: %s", exc)
                return

            with self._condition:
                if generation != self._generation:
                    return
                self._playing = False
                if repeat:
                    self._next_trigger = self._next_occurrence(
                        hour,
                        minute,
                        after=self._now(),
                    )
                    self._info = "будильник сработал; следующий запуск подготовлен"
                else:
                    self._scheduled = False
                    self._next_trigger = None
                    self._info = "будильник сработал"
                self._outcome = "fired"
                self._storage_error = ""
                self._save_locked()
                log.info("Alarm finished, scheduled=%s, next_trigger=%s", self._scheduled, self._next_trigger)
                if not self._scheduled:
                    return

    def _state_locked(self) -> dict:
        now = self._now()
        trigger = self._next_trigger
        if self._scheduled and trigger is None:
            trigger = self._next_occurrence(self._hour, self._minute)
            self._next_trigger = trigger
        remaining = 0
        if trigger is not None:
            remaining = max(0, int((trigger - now).total_seconds()))
        if self._sound_path:
            try:
                sound_name = Path(self._sound_path).name
            except (OSError, ValueError):
                sound_name = self._sound_path
        else:
            sound_name = ""
        if self._playing:
            status = "playing"
        elif self._scheduled:
            status = "scheduled"
        else:
            status = "idle"
        return {
            "scheduled": self._scheduled,
            "playing": self._playing,
            "status": status,
            "hour": self._hour if self._scheduled else 0,
            "minute": self._minute if self._scheduled else 0,
            "repeat": self._repeat,
            "interval": self._interval,
            "sound_path": self._sound_path,
            "sound_name": sound_name,
            "next_time": trigger.strftime("%H:%M") if trigger else "",
            "next_datetime": trigger.isoformat(timespec="seconds") if trigger else "",
            "remaining_seconds": remaining,
            "outcome": self._outcome,
            "info": self._info,
            "storage_error": self._storage_error,
        }

    def state(self) -> dict:
        with self._condition:
            return self._state_locked()

    def stop_thread(self) -> None:
        with self._condition:
            self._generation += 1
            try:
                self._player.stop()
            except Exception:
                pass
            self._playing = False
            self._condition.notify_all()
            thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.5)
