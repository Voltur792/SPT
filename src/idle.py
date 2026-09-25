"""Windows user-idle monitor used by the Astra plugin."""
from __future__ import annotations

import ctypes
import threading
import time


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]


def _tick_delta_ms(current_ticks: int, last_input_ticks: int) -> int:
    return (int(current_ticks) - int(last_input_ticks)) & 0xFFFFFFFF


class IdleMonitor:
    POLL_INTERVAL = 0.2

    def __init__(self, action_callback):
        self._action_callback = action_callback
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._minutes = 0
        self._action = "playpause"
        self._window = ""
        self._triggered = False
        self._error = ""
        self._last_activity_at: float | None = None
        self._last_cursor_position: tuple[int, int] | None = None

    def start(self, minutes: int, action: str, window: str = "") -> dict:
        try:
            minutes = int(minutes)
        except (TypeError, ValueError) as exc:
            raise ValueError("Укажите число минут") from exc
        if not 1 <= minutes <= 1440:
            raise ValueError("Период бездействия должен быть от 1 до 1440 минут")
        if action not in ("playpause", "shutdown", "sleep"):
            raise ValueError("Неизвестное действие")
        if action != "playpause":
            window = ""
        try:
            cursor_position = self._cursor_position()
        except Exception:
            cursor_position = None
        with self._lock:
            if self._thread and self._thread.is_alive():
                self._stop.set()
                old_thread = self._thread
            else:
                old_thread = None
        if old_thread:
            old_thread.join(timeout=1.0)
        with self._lock:
            self._stop = threading.Event()
            self._minutes, self._action, self._window = minutes, action, window
            self._triggered = False
            self._error = ""
            # Starting or resuming begins a fresh interval now. This also makes
            # a remote Telegram request useful even if Windows was already idle.
            self._last_activity_at = time.monotonic()
            self._last_cursor_position = cursor_position
            self._thread = threading.Thread(target=self._run, name="spt-idle-monitor", daemon=True)
            self._thread.start()
            return self.state()

    def stop(self) -> dict:
        with self._lock:
            self._stop.set()
            self._triggered = False
            self._thread = None
            return self.state()

    def state(self) -> dict:
        with self._lock:
            thread = self._thread
            running = bool(thread and thread.is_alive() and not self._stop.is_set())
            minutes, action, window = self._minutes, self._action, self._window
            triggered, error = self._triggered, self._error
            last_activity_at = self._last_activity_at
        if running and last_activity_at is not None:
            idle_seconds = max(0, int(time.monotonic() - last_activity_at))
        else:
            try:
                idle_seconds = self._idle_seconds()
            except Exception as exc:
                idle_seconds = 0
                error = error or str(exc)
        remaining = max(0, minutes * 60 - idle_seconds) if running else 0
        return {
            "running": running,
            "minutes": minutes,
            "action": action,
            "window": window,
            "triggered": triggered,
            "warning": running and not triggered and 0 < remaining <= 300,
            "remaining_seconds": remaining,
            "idle_seconds": idle_seconds,
            "error": error,
        }

    @staticmethod
    def _idle_seconds() -> int:
        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
        info = LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(LASTINPUTINFO)
        if not user32.GetLastInputInfo(ctypes.byref(info)):
            raise OSError("Windows не вернула время последнего ввода")
        # Both values are uint32 and wrap together; unsigned delta stays correct.
        return _tick_delta_ms(kernel32.GetTickCount(), info.dwTime) // 1000

    @staticmethod
    def _cursor_position() -> tuple[int, int]:
        """Return cursor screen coordinates, including negative multi-monitor space."""
        class POINT(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

        point = POINT()
        if not ctypes.windll.user32.GetCursorPos(ctypes.byref(point)):
            raise OSError("Windows не вернула положение курсора")
        return int(point.x), int(point.y)

    def _update_activity(self, cursor_position, system_idle_seconds, now: float) -> int:
        """Merge cursor movement with Windows keyboard/system activity."""
        with self._lock:
            if cursor_position is not None:
                if (self._last_cursor_position is not None
                        and cursor_position != self._last_cursor_position):
                    self._last_activity_at = now
                self._last_cursor_position = cursor_position
            if system_idle_seconds is not None:
                input_activity_at = now - max(0.0, float(system_idle_seconds))
                if self._last_activity_at is None or input_activity_at > self._last_activity_at:
                    self._last_activity_at = input_activity_at
            if self._last_activity_at is None:
                self._last_activity_at = now
            return max(0, int(now - self._last_activity_at))

    def _run(self) -> None:
        cooldown_until = 0.0
        while not self._stop.wait(self.POLL_INTERVAL):
            now = time.monotonic()
            try:
                system_idle_seconds = self._idle_seconds()
                input_error = ""
            except Exception as exc:
                system_idle_seconds = None
                input_error = str(exc)
            try:
                cursor_position = self._cursor_position()
                cursor_error = ""
            except Exception as exc:
                cursor_position = None
                cursor_error = str(exc)
            if system_idle_seconds is None and cursor_position is None:
                with self._lock:
                    self._error = input_error or cursor_error
                continue
            idle_seconds = self._update_activity(cursor_position, system_idle_seconds, now)
            callback = None
            with self._lock:
                self._error = input_error or cursor_error
                if self._triggered and now >= cooldown_until and idle_seconds < 2:
                    # The user returned after our synthetic media key/action.
                    self._triggered = False
                if idle_seconds >= self._minutes * 60 and not self._triggered:
                    self._triggered = True
                    cooldown_until = now + 15
                    callback = (self._action, self._window)
            if callback:
                try:
                    self._action_callback(*callback)
                except Exception as exc:
                    with self._lock:
                        self._error = str(exc)
