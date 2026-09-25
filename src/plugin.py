"""Sleep Pause Timer — плагин для Astra.

Портирование автономной программы SleepPause (sleep_pause.py) на SDK плагинов
Astra: обратный отсчёт, по завершении которого нажимается медиа-клавиша
play/pause (опционально с предварительной активацией выбранного окна), либо
компьютер выключается, либо уходит в спящий режим. Виджет на главном экране
Astra показывает отсчёт (слот home.widgets).

Зависимостей нет: клавиши и окна — чистый ctypes (в отличие от оригинала,
где использовались pyautogui/pygetwindow/win32gui).
"""

from __future__ import annotations

import ctypes
import base64
import json
import os
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog

from .alarm import AlarmEngine, AlarmError
from .calendar_alarms import CalendarAlarmEngine
from .idle import IdleMonitor
from astra_plugin_sdk import (
    BadArguments,
    Plugin,
    UiContribution,
    Unavailable,
    ui_call,
    tool,
)

# ----------------- Константы (из оригинальной программы) -----------------

ACTIONS = ("playpause", "shutdown", "sleep")
ACTION_LABELS = {
    "playpause": "нажать play/pause",
    "shutdown": "выключить ПК",
    "sleep": "спящий режим",
}

VK_MEDIA_PLAY_PAUSE = 0xB3
KEYEVENTF_KEYUP = 0x0002

# ExitWindowsEx: EWX_SHUTDOWN | EWX_FORCE — как в оригинале
EWX_SHUTDOWN_FORCE = 0x00000001 | 0x00000004

# SetWindowPos / ShowWindow / сообщения (методы активации из оригинала)
SW_RESTORE = 9
SW_SHOW = 5
SW_SHOWDEFAULT = 10
SW_SHOWNOACTIVATE = 4
WM_ACTIVATE = 0x0006
WA_ACTIVE = 1
HWND_TOP = 0
HWND_TOPMOST = -1
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_SHOWWINDOW = 0x0040
SWP_NOACTIVATE = 0x0010


def _fmt_hms(total_seconds: int) -> str:
    """«5:00», «1:04:59» — для сообщений пользователю."""
    h, rem = divmod(int(total_seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


# ----------------- Окна: перечисление и активация (ctypes) -----------------

WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)


def _visible_windows() -> list[tuple[int, str]]:
    """Видимые окна верхнего уровня с непустыми заголовками."""
    user32 = ctypes.windll.user32
    found: list[tuple[int, str]] = []

    def on_window(hwnd, _lparam):
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value
            if title:
                found.append((hwnd, title))
        except Exception:
            pass
        return True

    try:
        user32.EnumWindows(WNDENUMPROC(on_window), None)
    except Exception:
        pass
    return found


def press_media_play_pause() -> None:
    """Медиа-клавиша play/pause через keybd_event (замена pyautogui)."""
    user32 = ctypes.windll.user32
    user32.keybd_event(VK_MEDIA_PLAY_PAUSE, 0, 0, 0)
    time.sleep(0.02)
    user32.keybd_event(VK_MEDIA_PLAY_PAUSE, 0, KEYEVENTF_KEYUP, 0)


def shutdown_pc() -> None:
    """Выключение ПК — как в оригинале (ExitWindowsEx)."""
    ctypes.windll.user32.ExitWindowsEx(EWX_SHUTDOWN_FORCE, 0xFFFFFFFF)


def sleep_pc() -> None:
    """Спящий режим.

    Исправление относительно оригинала: SetSuspendState живёт в PowrProf.dll,
    а не в kernel32 (вызов ctypes.windll.kernel32.SetSuspendState в оригинале
    падал с AttributeError и сон не срабатывал).
    """
    ctypes.windll.powrprof.SetSuspendState(0, 0, 0)


class WindowManager:
    """Поиск и активация окон. Порт логики SleepPauseApp._activate_window."""

    def __init__(self):
        self._lock = threading.Lock()
        self._last_listed: list[str] = []

    # ---------- список окон (для инструмента list_windows и «#N») ----------

    def list_windows(self, limit: int = 50) -> list[str]:
        titles = [
            title
            for _, title in _visible_windows()
            if not self._is_self_title(title)
        ]
        with self._lock:
            self._last_listed = titles.copy()
        return [f"#{i} — {t}" for i, t in enumerate(titles[:limit], start=1)]

    @staticmethod
    def _is_self_title(title: str) -> bool:
        needle = (title or "").casefold()
        return "sleep pause" in needle or "таймер паузы" in needle

    # ---------- разрешение цели: «#N» или подстрока заголовка ----------

    def resolve_target(self, target: str) -> str:
        """«#N» → заголовок из последнего списка list_windows; иначе как есть."""
        if not target:
            return ""
        target = target.strip()
        if target.startswith("#"):
            try:
                idx = int(target[1:])
            except ValueError:
                return target
            with self._lock:
                listed = self._last_listed
            if 1 <= idx <= len(listed):
                return listed[idx - 1]
        return target

    # ---------- активация окна ----------

    def activate(self, target: str) -> tuple[bool, str]:
        """Активировать окно по подстроке/точному заголовку/«#N».

        Возвращает (успех, имя метода или диагностика). Последовательность
        методов повторяет оригинальную программу (A/B/C/D/G; PowerShell-метод
        F не портирован — ctypes-варианты покрывают те же случаи).
        """
        resolved = self.resolve_target(target)
        if not resolved:
            return False, "пустая цель"
        needle = resolved.strip().casefold()
        if self._is_self_title(resolved):
            return False, "пропуск собственного окна"

        is_chromium = any(b in needle for b in ("yandex", "chrome", "яндекс", "msedge"))
        hwnds = self._find_windows(resolved, needle)
        if not hwnds:
            sample = [t for _, t in _visible_windows()][:30]
            return False, "окно не найдено; примеры заголовков:\n" + "\n".join(sample)

        for hwnd in hwnds:
            ok, method = self._try_activate(hwnd, is_chromium)
            if ok:
                return True, method
        return False, f"все методы не сработали для {len(hwnds)} ок(на)"

    def _find_windows(self, resolved: str, needle: str) -> list[int]:
        windows = _visible_windows()
        exact = [h for h, t in windows if t.strip().casefold() == resolved.strip().casefold()]
        if exact:
            return exact
        return [h for h, t in windows if needle in (t or "").casefold()]

    def _try_activate(self, hwnd, is_chromium: bool) -> tuple[bool, str]:
        user32 = ctypes.windll.user32

        # Метод A: «как при демонстрации экрана» — танец с HWND_TOPMOST
        try:
            if user32.IsIconic(hwnd):
                user32.ShowWindow(hwnd, SW_RESTORE)
                time.sleep(0.05)
            user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                                SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
            time.sleep(0.05)
            user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
            time.sleep(0.05)
            user32.SetWindowPos(hwnd, HWND_TOP, 0, 0, 0, 0,
                                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
            time.sleep(0.05)
            user32.SetForegroundWindow(hwnd)
            time.sleep(0.1)
            if user32.GetForegroundWindow() == hwnd:
                return True, "SetWindowPos demo-style"
            user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                                SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW | SWP_NOACTIVATE)
            time.sleep(0.1)
            user32.SetForegroundWindow(hwnd)
            time.sleep(0.1)
            if user32.GetForegroundWindow() == hwnd:
                return True, "SetWindowPos TOPMOST+foreground"
        except Exception:
            pass

        # Метод B: классический
        try:
            user32.ShowWindow(hwnd, SW_RESTORE)
            user32.ShowWindow(hwnd, SW_SHOW)
            user32.SetForegroundWindow(hwnd)
            time.sleep(0.1)
            if user32.GetForegroundWindow() == hwnd:
                return True, "ShowWindow+SetForegroundWindow"
        except Exception:
            pass

        # Метод C: базовый ctypes
        try:
            user32.ShowWindow(hwnd, SW_SHOWDEFAULT)
            user32.SetForegroundWindow(hwnd)
            time.sleep(0.1)
            if user32.GetForegroundWindow() == hwnd:
                return True, "ShowDefault+SetForegroundWindow"
        except Exception:
            pass

        # Метод D: Chromium-браузеры
        if is_chromium:
            try:
                user32.SendMessageW(hwnd, WM_ACTIVATE, WA_ACTIVE, 0)
                time.sleep(0.05)
                user32.SetForegroundWindow(hwnd)
                time.sleep(0.1)
                if user32.GetForegroundWindow() == hwnd:
                    return True, "SendMessage WM_ACTIVATE"
            except Exception:
                pass

        # Метод G: несколько повторов
        try:
            for _ in range(3):
                user32.ShowWindow(hwnd, SW_RESTORE)
                time.sleep(0.05)
                user32.BringWindowToTop(hwnd)
                time.sleep(0.05)
                user32.SetForegroundWindow(hwnd)
                time.sleep(0.1)
                if user32.GetForegroundWindow() == hwnd:
                    return True, "multiple attempts"
        except Exception:
            pass

        return False, "методы не сработали"


# ----------------- Движок таймера (поток, как в оригинале) -----------------


class TimerEngine:
    """Обратный отсчёт без GUI — логика SleepPauseApp._timer_thread.

    Тик 0.2 с (в оригинале 1 с), остаток считается по монотонным дельтам,
    поэтому пауза не копит дрейф. Действие выполняется в том же потоке.
    """

    TICK = 0.2

    def __init__(self):
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._total = 0
        self._remaining = 0.0
        self._paused = False
        self._active = False
        self._action = ""
        self._window = ""
        self._outcome = ""  # "", "done", "cancelled"
        self._last_info = ""
        self.windows = WindowManager()

    # ---------- управление ----------

    def start(self, total_seconds: int, action: str, window: str) -> bool:
        with self._lock:
            if self._active:
                return False
            self._total = int(total_seconds)
            self._remaining = float(total_seconds)
            self._paused = False
            self._active = True
            self._action = action
            self._window = window
            self._outcome = ""
            self._last_info = ""
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
            return True

    def pause(self) -> str:
        with self._lock:
            if not self._active:
                return "idle"
            if self._paused:
                return "already-paused"
            self._paused = True
            return "paused"

    def resume(self) -> str:
        with self._lock:
            if not self._active:
                return "idle"
            if not self._paused:
                return "not-paused"
            self._paused = False
            return "resumed"

    def cancel(self) -> str:
        with self._lock:
            if not self._active:
                return "idle"
            self._active = False
            self._remaining = 0.0
            self._outcome = "cancelled"
            return "cancelled"

    def state(self) -> dict:
        with self._lock:
            remaining = max(0, int(round(self._remaining)))
            return {
                "running": self._active,
                "paused": self._active and self._paused,
                "remaining_seconds": remaining,
                "remaining": _fmt_hms(remaining),
                "total_seconds": self._total,
                "action": self._action,
                "window": self._window,
                "outcome": self._outcome,
                "info": self._last_info,
            }

    def stop_thread(self) -> None:
        self.cancel()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.5)

    # ---------- поток отсчёта ----------

    def _run(self):
        last = time.monotonic()
        while True:
            time.sleep(self.TICK)
            now = time.monotonic()
            with self._lock:
                if not self._active:
                    return  # отменён
                if not self._paused:
                    self._remaining -= now - last
                remaining = self._remaining
                paused = self._paused
            last = now
            if remaining <= 0 and not paused:
                break

        with self._lock:
            self._remaining = 0.0
            action, window = self._action, self._window
            self._outcome = "done"
            self._active = False
        self._execute(action, window)

    # ---------- действие по завершении ----------

    def _execute(self, action: str, window: str):
        try:
            if action == "shutdown":
                self._last_info = "выключение ПК"
                print("[sleep-pause-timer] shutdown requested", flush=True)
                time.sleep(1.0)
                shutdown_pc()
                return

            if action == "sleep":
                self._last_info = "спящий режим"
                print("[sleep-pause-timer] sleep requested", flush=True)
                time.sleep(1.0)
                sleep_pc()
                return

            # playpause — действие по умолчанию
            self._last_info = "play/pause"
            if window:
                ok, info = self.windows.activate(window)
                first_line = info.splitlines()[0] if info else ""
                self._last_info = (
                    "окно: активировано; " if ok else "окно: не активировано; "
                ) + first_line
                print(f"[sleep-pause-timer] window activate: ok={ok} {info!r}", flush=True)
                # Клавишу отправляем в любом случае — её получит то окно,
                # которое активно в данный момент.

            press_media_play_pause()
            self._last_info += "; клавиша отправлена"
            print("[sleep-pause-timer] media key sent", flush=True)
        except Exception as exc:  # не роняем поток ни при чём
            self._last_info = f"ошибка: {exc}"
            print(f"[sleep-pause-timer] action error: {exc!r}", flush=True)


# ----------------- Плагин -----------------


class SleepPauseTimer(Plugin):
    """Таймер и будильник: инструменты для Астры + виджеты на главном экране."""

    def __init__(self, alarm_engine=None):
        super().__init__()
        self.engine = TimerEngine()
        self.alarm = alarm_engine or AlarmEngine()
        self.calendar = CalendarAlarmEngine()
        self.calendar.set_sound_provider(lambda: self.alarm.state().get("sound_path", ""))
        self.idle = IdleMonitor(self.engine._execute)
        self.local_settings = self._load_local_settings()

    @staticmethod
    def _local_settings_path() -> Path:
        base = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA") or str(Path.home())
        return Path(base) / "sleep-pause-timer" / "settings.json"

    def _load_local_settings(self) -> dict:
        try:
            data = json.loads(self._local_settings_path().read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError, TypeError):
            return {}

    def _save_local_settings(self, settings: dict) -> dict:
        action = str(settings.get("default_action", "playpause"))
        if action not in ACTIONS:
            raise ValueError("Неизвестное действие по умолчанию")
        try:
            minutes = max(1, min(1440, int(settings.get("idle_minutes", 30))))
        except (TypeError, ValueError) as exc:
            raise ValueError("Период бездействия должен быть числом") from exc
        idle_action = str(settings.get("idle_action", action))
        if idle_action not in ACTIONS:
            raise ValueError("Неизвестное действие при бездействии")
        saved = {
            "default_action": action,
            "default_window": str(settings.get("default_window", "") or "").strip(),
            "idle_minutes": minutes,
            "idle_action": idle_action,
        }
        path = self._local_settings_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(saved, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
        self.local_settings = saved
        return saved

    def _alarm_error(self, exc: AlarmError) -> BadArguments:
        return BadArguments(str(exc))

    @tool(
        "Set a one-shot or daily Windows alarm that plays a local audio file. "
        "Use for «поставь будильник», «разбуди меня в 7:30», «wake me at …». "
        "Parameters: time (HH:MM format), sound_path (optional, uses saved melody if omitted), "
        "repeat (true = daily, false = one time), interval (minutes between repeats, 1-60). "
        "ALWAYS call this tool to actually set the alarm; never only say it is set."
    )
    async def set_alarm(
        self,
        time: str,
        sound_path: str = "",
        repeat: bool = True,
        interval: int = 5,
        days_of_week: list[int] | None = None,
    ) -> dict:
        await self._ensure_config()
        try:
            if ":" not in time:
                raise AlarmError("Время должно быть в формате HH:MM")
            hour_str, minute_str = time.split(":")[:2]
            hour = int(hour_str)
            minute = int(minute_str)
            if not 0 <= hour <= 23 or not 0 <= minute <= 59:
                raise AlarmError("Время должно быть в диапазоне 00:00–23:59")
            interval = max(1, min(60, int(interval)))

            if not sound_path:
                sound_path = str(
                    self.config.get("alarm_sound_path", "") or ""
                ) or self.alarm.state()["sound_path"]
            state = self.alarm.set_alarm(hour, minute, sound_path, repeat, interval, days_of_week)
        except AlarmError as exc:
            raise self._alarm_error(exc) from exc
        except ValueError as exc:
            raise self._alarm_error(AlarmError(f"Неверное время: {exc}")) from exc
        await self.log_info(
            "set_alarm: "
            f"{state['next_time']}, repeat={state['repeat']}, interval={state.get('interval', 5)}, sound={state['sound_path']!r}"
        )
        return {
            "message": (
                f"Будильник установлен на {state['next_time']}"
                + (" ежедневно" if state["repeat"] else " один раз")
                + (f", повтор каждые {state['interval']} мин" if state.get('repeat') else "")
                + f"; звук: {state['sound_name']}"
            ),
            "state": state,
        }

    @tool(
        "Set or replace the local audio file used by the alarm. Use when the "
        "user wants to choose a different local music file. Required: "
        "sound_path, an absolute path to an existing local audio file."
    )
    async def set_alarm_sound(self, sound_path: str) -> dict:
        try:
            state = self.alarm.set_sound(sound_path)
        except AlarmError as exc:
            raise self._alarm_error(exc) from exc
        return {
            "message": f"Звук будильника: {state['sound_name']}",
            "state": state,
        }

    @tool(
        "Cancel the configured alarm without playing it. Use when the user asks "
        "to turn off, cancel, or stop the alarm."
    )
    async def cancel_alarm(self) -> dict:
        state = self.alarm.cancel()
        await self.log_info(f"cancel_alarm -> {state['status']}")
        return {
            "message": "Будильник отменён" if state["status"] == "idle" else "Будильник остановлен",
            "state": state,
        }

    @tool(
        "Report the configured alarm: next time, whether it is active or playing, "
        "repeat mode, interval between repeats, and the selected local audio file."
    )
    async def alarm_status(self) -> dict:
        state = self.alarm.state()
        if state["playing"]:
            message = "Будильник играет"
        elif state["scheduled"]:
            message = f"Будильник установлен на {state['next_time']}"
            if state["repeat"]:
                message += f" ежедневно (каждые {state.get('interval', 5)} мин)"
        else:
            message = "Будильник не установлен"
        if state["sound_path"]:
            message += f"; звук: {state['sound_name']}"
        state["message"] = message
        return state

    @tool(
        "Stop the alarm sound now. Use when the user asks to stop, silence, or "
        "dismiss the currently playing alarm."
    )
    async def stop_alarm_sound(self) -> dict:
        state = self.alarm.stop_playback()
        was_playing = state["outcome"] == "stopped"
        await self.log_info(f"stop_alarm_sound -> {'stopped' if was_playing else 'idle'}")
        return {
            "message": "Звук будильника остановлен" if was_playing else "Будильник не играл",
            "state": state,
        }

    @tool(
        "Open a native Windows file picker and return the selected local audio "
        "file path. Use when the user asks to choose a local music file for the "
        "alarm. The user must select an existing audio file."
    )
    @ui_call
    async def choose_alarm_sound(self) -> dict:
        root = tk.Tk()
        root.withdraw()
        try:
            path = filedialog.askopenfilename(
                parent=root,
                title="Выберите музыку для будильника",
                filetypes=[
                    ("Аудиофайлы", "*.mp3;*.wav;*.wma;*.m4a;*.aac;*.flac;*.ogg;*.mid;*.midi"),
                    ("Все файлы", "*.*"),
                ],
            )
        finally:
            root.destroy()
        if not path:
            return {"cancelled": True, "sound_path": ""}
        return {"cancelled": False, "sound_path": path}

    async def _ensure_config(self):
        # Сервicer SDK ведёт self.config сам; подстраховка на случай, если
        # первый вызов придёт раньше первичной синхронизации конфигурации.
        if not self.config:
            try:
                raw = await self.host.get_config()
                if raw:
                    self.config = json.loads(raw)
            except Exception:
                pass

    def _default_action(self) -> str:
        action = str(self.local_settings.get("default_action") or self.config.get("default_action", "") or "").strip().lower()
        return action if action in ACTIONS else "playpause"

    def _default_window(self) -> str:
        if "default_window" in self.local_settings:
            return str(self.local_settings.get("default_window") or "")
        return str(self.config.get("default_window", "") or "")

    # ---------- инструменты ----------

    @tool(
        "Start a countdown timer that shows a live countdown widget on the Home "
        "screen. Use this INSTEAD of the built-in timer/reminder for ANY "
        "'set a timer' / countdown request — 'поставь таймер', 'timer for N "
        "minutes', 'напомни через N минут', 'через N минут' — the user watches "
        "it and can pause, resume or cancel it right from the widget. When it "
        "reaches zero it presses the media play/pause key (default), shuts the "
        "PC down, or puts it to sleep. Pass the duration via "
        "hours/minutes/seconds (at least one must be > 0). action: playpause "
        "(default), shutdown or sleep. window: a window title substring or "
        "'#N' from list_windows to activate before pressing the key "
        "(playpause only). ALWAYS call this tool to actually set a timer — "
        "never just say the timer is set."
    )
    async def start_timer(
        self,
        hours: int = 0,
        minutes: int = 0,
        seconds: int = 0,
        action: str = "",
        window: str = "",
    ) -> dict:
        await self._ensure_config()
        total = int(hours) * 3600 + int(minutes) * 60 + int(seconds)
        if total <= 0:
            raise BadArguments(
                "Длительность должна быть больше нуля — укажите часы, минуты или секунды"
            )
        act = (action or "").strip().lower() or self._default_action()
        if act not in ACTIONS:
            raise BadArguments(
                f"Неизвестное действие {act!r}. Доступны: playpause, shutdown, sleep"
            )
        win = (window or "").strip() or self._default_window()
        if win and act != "playpause":
            win = ""  # активация окна имеет смысл только перед нажатием клавиши

        if not self.engine.start(total, act, win):
            raise Unavailable("Таймер уже запущен — сначала отмените его (cancel_timer)")

        await self.log_info(f"start_timer: {_fmt_hms(total)}, action={act}, window={win!r}")
        message = f"Таймер на {_fmt_hms(total)} запущен; по завершении: {ACTION_LABELS[act]}"
        if win:
            message += f"; окно: {win}"
        return {"message": message, "state": self.engine.state()}

    @tool(
        "Pause the running countdown. Use when the user asks to pause the timer."
    )
    async def pause_timer(self) -> str:
        result = self.engine.pause()
        await self.log_info(f"pause_timer -> {result}")
        if result == "idle":
            return "Таймер не запущен"
        if result == "already-paused":
            return "Таймер уже на паузе"
        return f"Пауза. Осталось {self.engine.state()['remaining']}"

    @tool(
        "Resume the paused countdown. Use when the user asks to continue the timer."
    )
    async def resume_timer(self) -> str:
        result = self.engine.resume()
        if result == "idle":
            return "Таймер не запущен"
        if result == "not-paused":
            return f"Таймер идёт, осталось {self.engine.state()['remaining']}"
        return f"Продолжаем. Осталось {self.engine.state()['remaining']}"

    @tool(
        "Cancel the countdown without performing its action. Use when the user "
        "asks to stop or cancel the timer."
    )
    async def cancel_timer(self) -> str:
        result = self.engine.cancel()
        await self.log_info(f"cancel_timer -> {result}")
        return "Таймер отменён" if result == "cancelled" else "Таймер не запущен"

    @tool(
        "Report the state of the timer started with start_timer: whether it "
        "runs, is paused, how much is left and what will happen at zero. Use "
        "for 'сколько осталось' / 'how much is left on the timer' and similar."
    )
    async def timer_status(self) -> dict:
        state = self.engine.state()
        if not state["running"]:
            message = "Таймер не запущен"
        elif state["paused"]:
            message = f"Пауза, осталось {state['remaining']}"
        else:
            message = f"Идёт отсчёт, осталось {state['remaining']}"
        if state["running"] or state["outcome"] == "done":
            message += f"; по завершении: {ACTION_LABELS.get(state['action'], state['action'])}"
        state["message"] = message
        return state

    @tool(
        "List visible window titles as '#N — title'. Use it before start_timer "
        "to pick a window: pass its '#N' (or a title substring) as the window "
        "argument so the window is activated before the play/pause key is sent."
    )
    async def list_windows(self) -> dict:
        titles = self.engine.windows.list_windows()
        return {
            "windows": titles,
            "count": len(titles),
            "hint": "Передайте '#N' или подстроку заголовка в start_timer(window=...)",
        }

    @tool("Schedule a one-time local alarm on a calendar date. Use ISO date YYYY-MM-DD and local time HH:MM.")
    async def set_monthly_alarm(self, date: str, time: str) -> dict:
        try:
            if not self.alarm.state().get("sound_path"):
                raise AlarmError("Сначала выберите мелодию во вкладке «Будильник»")
            alarm_date = __import__("datetime").date.fromisoformat(date)
            parts = time.split(":")
            if len(parts) != 2:
                raise ValueError("time must be HH:MM")
            state = self.calendar.set_alarm(alarm_date, int(parts[0]), int(parts[1]))
        except (ValueError, TypeError, AlarmError) as exc:
            raise BadArguments(str(exc)) from exc
        return {"message": f"Будильник добавлен на {date} в {time}", "state": state}

    @tool("Cancel the one-time calendar alarm for a date (YYYY-MM-DD).")
    async def cancel_monthly_alarm(self, date: str) -> dict:
        try:
            day = __import__("datetime").date.fromisoformat(date)
            state = self.calendar.set_alarm(day, None, None)
        except (ValueError, TypeError, AlarmError) as exc:
            raise BadArguments(str(exc)) from exc
        return {"message": f"Будильник на {date} отменён", "state": state}

    @tool("List upcoming one-time calendar alarms and the next scheduled date.")
    async def calendar_alarm_status(self) -> dict:
        return self.calendar.state()

    @tool("Stop the currently playing one-time calendar alarm sound without changing other calendar entries.")
    async def stop_calendar_alarm_sound(self) -> dict:
        was_playing = bool(self.calendar.state().get("playing"))
        self.calendar.stop_playback()
        return {"message": "Календарный звонок остановлен" if was_playing else "Календарный звонок не звучит",
                "state": self.calendar.state()}

    @tool("Set the Windows inactivity threshold and start or restart monitoring. A request such as «Установи отслеживание бездействия на 40 минут» means call this tool with `minutes=40`. The required integer `minutes` is the number of minutes without input before the action runs. Optional `action` and `window` select what happens when the threshold is reached.")
    async def start_idle_monitor(self, minutes: int, action: str = "", window: str = "") -> dict:
        try:
            minutes = int(minutes)
        except (TypeError, ValueError) as exc:
            raise BadArguments("Укажите порог бездействия в минутах") from exc
        if not 1 <= minutes <= 1440:
            raise BadArguments("Порог бездействия должен быть от 1 до 1440 минут")
        act = (action or "").strip().lower() or str(self.local_settings.get("idle_action") or self._default_action())
        target = (window or "").strip() or self._default_window()
        if target and act != "playpause":
            target = ""
        try:
            self.local_settings = self._save_local_settings({
                **self.local_settings,
                "default_action": self.local_settings.get("default_action") or self._default_action(),
                "default_window": self.local_settings.get("default_window", self._default_window()),
                "idle_minutes": minutes,
                "idle_action": act,
            })
            state = self.idle.start(minutes, act, target)
        except ValueError as exc:
            raise BadArguments(str(exc)) from exc
        except OSError as exc:
            raise RuntimeError(f"Не удалось сохранить настройки отслеживания: {exc}") from exc
        return {"message": f"Отслеживание бездействия запущено: {minutes} мин", "state": state}

    @tool("Resume or restart Windows inactivity tracking from now. Keep the current duration and action, or use the saved inactivity settings if tracking is stopped. Use this when the user asks to resume/reset inactivity tracking in an Astra assistant conversation; Telegram can use it when its integration exposes plugin tools to the assistant.")
    async def resume_idle_monitor(self) -> dict:
        current = self.idle.state()
        try:
            configured_minutes = int(current.get("minutes") or 0)
        except (TypeError, ValueError):
            configured_minutes = 0
        has_monitor_configuration = configured_minutes > 0
        if has_monitor_configuration:
            minutes = configured_minutes
            action = str(current.get("action") or "").strip().lower()
            window = str(current.get("window") or "")
        else:
            try:
                minutes = int(self.local_settings.get("idle_minutes", 30))
            except (TypeError, ValueError):
                minutes = 30
            action = str(self.local_settings.get("idle_action") or self._default_action()).strip().lower()
            window = self._default_window()
        minutes = max(1, min(1440, minutes))
        if action not in ACTIONS:
            action = self._default_action()
        if action != "playpause":
            window = ""
        try:
            state = self.idle.start(minutes, action, window)
        except ValueError as exc:
            raise BadArguments(str(exc)) from exc
        return {"message": f"Отслеживание бездействия возобновлено. Новый отсчёт: {minutes} мин", "state": state}

    @tool("Stop tracking Windows inactivity.")
    async def stop_idle_monitor(self) -> dict:
        return self.idle.stop()

    @tool("Report Windows inactivity tracking status and time until the configured action.")
    async def idle_status(self) -> dict:
        return self.idle.state()

    # ---------- вызовы из виджета (CallFromUi) ----------

    @ui_call
    async def state(self):
        return self.engine.state()

    @ui_call
    async def pause(self):
        self.engine.pause()
        return self.engine.state()

    @ui_call
    async def resume(self):
        self.engine.resume()
        return self.engine.state()

    @ui_call
    async def cancel(self):
        self.engine.cancel()
        return self.engine.state()

    @ui_call
    async def alarm_state(self):
        daily = self.alarm.state()
        monthly = self.calendar.state()
        daily = dict(daily)
        daily.update({
            "calendar_count": monthly.get("count", 0),
            "calendar_next": monthly.get("next"),
            "calendar_remaining_seconds": monthly.get("remaining_seconds", 0),
            "calendar_playing": monthly.get("playing"),
        })
        if monthly.get("playing"):
            daily.update({
                "scheduled": False,
                "playing": True,
                "source": "calendar",
                "next_time": monthly["playing"],
                "sound_name": "Календарный будильник",
            })
        elif monthly.get("next") and (
            not daily.get("scheduled")
            or monthly.get("remaining_seconds", 0) < daily.get("remaining_seconds", 0)
        ):
            upcoming = monthly["next"]
            daily.update({
                "scheduled": True,
                "playing": False,
                "source": "calendar",
                "next_time": f"{upcoming['date']} {upcoming['hour']:02d}:{upcoming['minute']:02d}",
                "remaining_seconds": monthly.get("remaining_seconds", 0),
                "repeat": False,
                "sound_name": "Календарный будильник",
            })
        else:
            daily["source"] = "daily"
        return daily

    @ui_call
    async def alarm_cancel(self):
        monthly = self.calendar.state()
        if monthly.get("playing"):
            self.calendar.stop_playback()
            return await self.alarm_state()
        current = await self.alarm_state()
        if current.get("source") == "calendar" and monthly.get("next"):
            upcoming = monthly["next"]
            self.calendar.set_alarm(
                __import__("datetime").date.fromisoformat(upcoming["date"]), None, None
            )
            return await self.alarm_state()
        self.alarm.cancel()
        return await self.alarm_state()

    @ui_call
    async def alarm_stop(self):
        monthly = self.calendar.state()
        if monthly.get("playing"):
            self.calendar.stop_playback()
            return await self.alarm_state()
        self.alarm.stop_playback()
        return await self.alarm_state()

    @ui_call
    async def alarm_set(self, hour: int, minute: int, repeat: bool = True, interval: int = 5,
                        sound_path: str = "", days_of_week: list[int] | None = None):
        try:
            if not str(sound_path or "").strip():
                sound_path = self.alarm.state()["sound_path"]
            state = self.alarm.set_alarm(
                int(hour), int(minute), sound_path, bool(repeat), int(interval), days_of_week
            )
        except (AlarmError, TypeError, ValueError) as exc:
            return {"error": str(exc)}
        return state

    @ui_call
    async def calendar_state(self):
        state = self.calendar.state()
        alarms = [dict(item, source="calendar") for item in state.get("alarms", [])]
        daily = self.alarm.state()
        next_datetime = daily.get("next_datetime", "")
        if daily.get("scheduled") and not daily.get("repeat") and next_datetime:
            try:
                at = __import__("datetime").datetime.fromisoformat(next_datetime)
                alarms.append({
                    "date": at.date().isoformat(), "hour": at.hour,
                    "minute": at.minute, "source": "alarm",
                })
            except (TypeError, ValueError):
                pass
        alarms.sort(key=lambda item: (item["date"], item["hour"], item["minute"], item["source"]))
        state["alarms"] = alarms
        state["count"] = len(alarms)
        state["next"] = alarms[0] if alarms else None
        state["remaining_seconds"] = daily.get("remaining_seconds", 0) if (
            daily.get("scheduled") and not daily.get("repeat")
            and alarms and alarms[0].get("source") == "alarm"
        ) else self.calendar.state().get("remaining_seconds", 0)
        if daily.get("playing") and not daily.get("repeat"):
            state["playing"] = "alarm"
        return state

    @ui_call
    async def calendar_cancel(
        self, date: str, source: str = "calendar",
        hour: int | None = None, minute: int | None = None,
    ):
        try:
            day = __import__("datetime").date.fromisoformat(date)
            if source == "alarm":
                daily = self.alarm.state()
                stamp = str(daily.get("next_datetime", ""))
                if daily.get("scheduled") and not daily.get("repeat") and stamp.startswith(date):
                    at = __import__("datetime").datetime.fromisoformat(stamp)
                    if hour is None or minute is None or (at.hour, at.minute) == (int(hour), int(minute)):
                        self.alarm.cancel()
            elif hour is not None and minute is not None:
                self.calendar.cancel_alarm(day, int(hour), int(minute))
            else:
                self.calendar.set_alarm(day, None, None)
            return await self.calendar_state()
        except (AlarmError, ValueError, TypeError) as exc:
            return {"error": str(exc)}

    @ui_call
    async def calendar_set(self, date: str, hour: int | None, minute: int | None):
        try:
            if hour is not None and minute is not None and not self.alarm.state().get("sound_path"):
                return {"error": "Сначала выберите мелодию во вкладке «Будильник»"}
            day = __import__("datetime").date.fromisoformat(date)
            return self.calendar.set_alarm(day, hour, minute)
        except (AlarmError, ValueError, TypeError) as exc:
            return {"error": str(exc)}

    @ui_call
    async def calendar_stop(self):
        self.calendar.stop_playback()
        return self.calendar.state()

    @ui_call
    async def settings_state(self):
        return {"settings": self.local_settings, "idle": self.idle.state(),
                "alarm": self.alarm.state(), "calendar": self.calendar.state()}

    @ui_call
    async def settings_save(self, default_action: str, default_window: str = "",
                            idle_minutes: int = 30, idle_action: str = "playpause"):
        try:
            return {"settings": self._save_local_settings({
                "default_action": default_action, "default_window": default_window,
                "idle_minutes": idle_minutes, "idle_action": idle_action,
            })}
        except (OSError, ValueError, TypeError) as exc:
            return {"error": str(exc)}

    @ui_call
    async def idle_start(self, minutes: int, action: str, window: str = ""):
        try:
            self.local_settings = self._save_local_settings({
                **self.local_settings,
                "idle_minutes": minutes,
                "idle_action": action,
            })
            return self.idle.start(minutes, action, window)
        except (ValueError, OSError) as exc:
            return {"error": str(exc)}

    @ui_call
    async def idle_stop(self):
        return self.idle.stop()

    @ui_call
    async def windows_list(self):
        return {"windows": self.engine.windows.list_windows()}

    @ui_call
    async def timer_start_local(self, hours: int, minutes: int, seconds: int,
                                action: str, window: str = ""):
        total = int(hours) * 3600 + int(minutes) * 60 + int(seconds)
        if total <= 0:
            return {"error": "Длительность должна быть больше нуля"}
        if action not in ACTIONS:
            return {"error": "Неизвестное действие"}
        if not self.engine.start(total, action, window if action == "playpause" else ""):
            return {"error": "Таймер уже запущен"}
        return self.engine.state()

    @ui_call
    async def alarm_sound_choose_local(self):
        return await self.choose_alarm_sound()

    @ui_call
    async def alarm_sound_save(self, sound_path: str):
        try:
            return self.alarm.set_sound(sound_path)
        except AlarmError as exc:
            return {"error": str(exc)}

    # ---------- UI-вклад ----------

    async def get_ui_contributions(self) -> list[UiContribution]:
        # Строим вручную, а не через @ui_slot: нужен transparent=True
        # (иначе Astra зальёт iframe непрозрачным фоном — «чёрный фон виджета»).
        icon_path = Path(__file__).resolve().parent.parent / "icon.png"
        try:
            icon_data = base64.b64encode(icon_path.read_bytes()).decode("ascii")
            page_icon = (
                '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">'
                f'<image width="512" height="512" href="data:image/png;base64,{icon_data}"/>'
                '</svg>'
            )
        except OSError:
            page_icon = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="12" cy="13" r="8"/><path d="M12 9v4l3 2M9 2h6M12 2v3"/></svg>'
        return [
            UiContribution(
                id="timer-widget",
                slot="home.widgets",
                url="widget.html",
                height=120,
                transparent=True,
                pointer_events=True,
            ),
            UiContribution(
                id="alarm-widget",
                slot="home.widgets",
                url="alarm-widget.html",
                height=120,
                transparent=True,
                pointer_events=True,
            ),
            UiContribution(
                id="timer-settings-page",
                slot="page.custom",
                url="settings-v2.html",
                label="Таймер паузы",
                icon_svg=page_icon,
                transparent=True,
                pointer_events=True,
            ),
        ]

    # ---------- завершение ----------

    async def on_shutdown(self):
        self.engine.stop_thread()
        self.alarm.stop_thread()
        self.calendar.stop_thread()
        self.idle.stop()

    async def on_config_changed(self, config: dict):
        self.config = config
        sound_path = config.get("alarm_sound_path", "")
        if sound_path:
            try:
                self.alarm.set_sound(sound_path)
            except AlarmError:
                pass


if __name__ == "__main__":
    SleepPauseTimer().run()
