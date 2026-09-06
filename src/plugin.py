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
import json
import threading
import time

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
    ctypes.windll.kernel32.ExitWindowsEx(EWX_SHUTDOWN_FORCE, 0xFFFFFFFF)


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
    """Таймер паузы: инструменты для Астры + виджет отсчёта на главном экране."""

    def __init__(self):
        super().__init__()
        self.engine = TimerEngine()

    # ---------- настройки ----------

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
        action = str(self.config.get("default_action", "") or "").strip().lower()
        return action if action in ACTIONS else "playpause"

    def _default_window(self) -> str:
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

    # ---------- UI-вклад ----------

    async def get_ui_contributions(self) -> list[UiContribution]:
        # Строим вручную, а не через @ui_slot: нужен transparent=True
        # (иначе Astra зальёт iframe непрозрачным фоном — «чёрный фон виджета»).
        return [
            UiContribution(
                id="timer-widget",
                slot="home.widgets",
                url="widget.html",
                height=104,
                transparent=True,
                pointer_events=True,
            )
        ]

    # ---------- завершение ----------

    async def on_shutdown(self):
        self.engine.stop_thread()


if __name__ == "__main__":
    SleepPauseTimer().run()

