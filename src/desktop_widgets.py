"""Optional independent Windows desktop windows, managed by the plugin."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from .alarm import default_state_path

DEFAULT = {"enabled": False, "rounded": True, "transparency": 10, "on_top": False, "x": None, "y": None}


class DesktopWidgets:
    def __init__(self):
        self.path = default_state_path().with_name("desktop-widgets.json")
        self.lock = threading.RLock()
        self.processes = {}
        self.errors = {}
        try:
            stored = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            stored = {}
        self.settings = {}
        for kind in ("timer", "alarm"):
            values = stored.get(kind, {}) if isinstance(stored, dict) else {}
            values = values if isinstance(values, dict) else {}
            settings = dict(DEFAULT)
            for key in ("enabled", "rounded", "on_top"):
                if type(values.get(key)) is bool:
                    settings[key] = values[key]
            value = values.get("transparency")
            if type(value) in (int, float) and 0 <= value <= 80:
                settings["transparency"] = round(value)
            for key in ("x", "y"):
                value = values.get(key)
                if type(value) is int and -100000 <= value <= 100000:
                    settings[key] = value
            self.settings[kind] = settings

    def state(self):
        with self.lock:
            return {kind: {**self.settings[kind], "running": kind in self.processes and self.processes[kind].poll() is None,
                           "error": self.errors.get(kind, "")} for kind in self.settings}

    def set(self, kind, settings):
        if kind not in self.settings or not isinstance(settings, dict):
            raise ValueError("Неизвестный виджет")
        with self.lock:
            updated = dict(self.settings[kind])
            for key in ("enabled", "rounded", "on_top"):
                if key in settings:
                    if type(settings[key]) is not bool:
                        raise ValueError("Некорректный переключатель виджета")
                    updated[key] = settings[key]
            if "transparency" in settings:
                value = int(settings["transparency"])
                if not 0 <= value <= 80:
                    raise ValueError("Прозрачность должна быть от 0 до 80%")
                updated["transparency"] = value
            for key in ("x", "y"):
                if key in settings:
                    value = settings[key]
                    if value is not None and (type(value) is not int or not -100000 <= value <= 100000):
                        raise ValueError("Некорректная позиция виджета")
                    updated[key] = value
            previous = self.settings[kind]
            self.settings[kind] = updated
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                temp = self.path.with_suffix(".tmp")
                temp.write_text(json.dumps(self.settings, ensure_ascii=False, indent=2), encoding="utf-8")
                temp.replace(self.path)
            except OSError:
                self.settings[kind] = previous
                raise
            if not updated["enabled"]:
                self.stop(kind)
            return self.state()

    def ensure(self):
        with self.lock:
            for kind, settings in self.settings.items():
                process = self.processes.get(kind)
                if not settings["enabled"] or (process and process.poll() is None):
                    continue
                if process:
                    self.errors[kind] = "Окно виджета завершилось. Выключите и включите виджет, чтобы повторить."
                    continue
                try:
                    self.processes[kind] = subprocess.Popen([sys.executable, "-m", "src.desktop_widgets", kind],
                        cwd=str(self.path_to_project()), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
                    self.errors.pop(kind, None)
                except OSError as exc:
                    self.errors[kind] = str(exc)

    @staticmethod
    def path_to_project():
        from pathlib import Path
        return Path(__file__).resolve().parent.parent

    def stop(self, kind):
        process = self.processes.pop(kind, None)
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
        self.errors.pop(kind, None)

    def close(self):
        for kind in list(self.processes):
            self.stop(kind)


def run_window(kind):
    import ctypes
    import queue
    import tkinter as tk
    from .integrations import integration_call
    if sys.platform == "win32":
        shell32 = ctypes.windll.shell32
        shell32.SetCurrentProcessExplicitAppUserModelID.argtypes = [ctypes.c_wchar_p]
        shell32.SetCurrentProcessExplicitAppUserModelID.restype = ctypes.c_long
        shell32.SetCurrentProcessExplicitAppUserModelID("Voltur.SleepPauseTimer.Widget." + kind)
    root = tk.Tk()
    root.widget_icon = tk.PhotoImage(file=str(Path(__file__).resolve().parent.parent / "icon.png"))
    root.title("Таймер паузы · " + ("Таймер" if kind == "timer" else "Будильник"))
    root.overrideredirect(True)
    root.configure(bg="#25243b")
    root.geometry("320x164")
    root.update_idletasks()
    # Borderless mode recreates the Windows wrapper; apply its icon afterwards.
    root.iconphoto(True, root.widget_icon)
    title = tk.Label(root, text="Таймер" if kind == "timer" else "Будильник", bg="#25243b", fg="#bbb8d3", font=("Segoe UI", 11))
    title.pack(fill="x", padx=12, pady=(8, 0))
    metric = tk.Label(root, text="—", bg="#25243b", fg="#ffffff", font=("Segoe UI", 26, "bold"))
    metric.pack(fill="x")
    meta = tk.Label(root, text="Подключение…", bg="#25243b", fg="#bbb8d3", font=("Segoe UI", 9))
    meta.pack(fill="x")
    buttons = tk.Frame(root, bg="#25243b")
    buttons.pack(pady=8)
    state = {}
    messages = queue.Queue()
    requests = queue.Queue()
    stopping = threading.Event()

    def worker():
        while not stopping.is_set():
            try:
                try:
                    method, params = requests.get_nowait()
                    integration_call("timer", method, **params)
                except queue.Empty:
                    pass
                result = integration_call("timer", "desktop_snapshot", kind=kind)
                messages.put(result)
            except Exception as exc:
                messages.put({"error": str(exc)})
            stopping.wait(.5)

    def control():
        if kind == "timer":
            requests.put(("resume" if state.get("paused") else "pause", {"timer_id": state.get("id", "")}))
        else:
            requests.put(("alarm_stop", {}))

    def close():
        requests.put(("desktop_set", {"kind": kind, "settings": {"enabled": False}}))

    pause = tk.Button(buttons, text="Пауза", command=control, bg="#46425f", fg="white", relief="flat", takefocus=True)
    pause.pack(side="left", padx=4)
    cancel = tk.Button(buttons, text="Отменить", command=lambda: requests.put(("cancel" if kind == "timer" else "alarm_cancel", {"timer_id": state.get("id", "")} if kind == "timer" else {})), bg="#46425f", fg="white", relief="flat")
    cancel.pack(side="left", padx=4)
    tk.Button(buttons, text="×", command=close, bg="#46425f", fg="white", relief="flat").pack(side="left", padx=4)
    drag = {}

    def begin(event):
        drag.update(x=event.x_root-root.winfo_x(), y=event.y_root-root.winfo_y())

    def move(event):
        position(event.x_root-drag['x'], event.y_root-drag['y'])

    def position(x, y):
        user32 = ctypes.windll.user32
        user32.GetParent.argtypes = [ctypes.c_void_p]
        user32.GetParent.restype = ctypes.c_void_p
        user32.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
        hwnd = user32.GetParent(root.winfo_id()) or root.winfo_id()
        user32.SetWindowPos(hwnd, None, int(x), int(y), 0, 0, 0x15)

    def end(event):
        requests.put(("desktop_set", {"kind": kind, "settings": {"x": root.winfo_x(), "y": root.winfo_y()}}))

    for node in (title, metric):
        node.bind("<ButtonPress-1>", begin)
        node.bind("<B1-Motion>", move)
        node.bind("<ButtonRelease-1>", end)
    appearance = None

    def tick():
        nonlocal state, appearance
        latest = None
        while not messages.empty():
            latest = messages.get_nowait()
        if latest:
            if latest.get("error"):
                meta.config(text="Нет связи с Astra")
            else:
                state = latest["state"]
                settings = latest["settings"]
                if not settings["enabled"]:
                    stopping.set()
                    root.destroy()
                    return
                metric.config(text=state.get("remaining", "—") if kind == "timer" and state.get("running") else
                              "Звонит" if kind == "alarm" and state.get("playing") else state.get("next_time", "—") if kind == "alarm" and state.get("scheduled") else "—")
                title.config(text=state.get("name", "Таймер") if kind == "timer" else "Будильник")
                meta.config(text=("На паузе" if state.get("paused") else "Идёт отсчёт") if kind == "timer" and state.get("running") else
                            "Таймер не запущен" if kind == "timer" else "Ближайший будильник" if state.get("scheduled") else "Будильник не установлен")
                pause.config(text=("Продолжить" if state.get("paused") else "Пауза") if kind == "timer" else "Остановить звонок")
                current = (settings["rounded"], settings["transparency"], settings["on_top"])
                if current != appearance:
                    root.attributes("-alpha", 1-settings["transparency"]/100)
                    root.attributes("-topmost", settings["on_top"])
                    root.update_idletasks()
                    user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
                    user32.GetParent.argtypes = [ctypes.c_void_p]
                    user32.GetParent.restype = ctypes.c_void_p
                    user32.SetWindowRgn.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_bool]
                    gdi32.CreateRoundRectRgn.argtypes = [ctypes.c_int]*6
                    gdi32.CreateRoundRectRgn.restype = ctypes.c_void_p
                    hwnd = user32.GetParent(root.winfo_id()) or root.winfo_id()
                    region = gdi32.CreateRoundRectRgn(0, 0, root.winfo_width()+1, root.winfo_height()+1, 24, 24) if settings["rounded"] else None
                    if not user32.SetWindowRgn(hwnd, region, True) and region:
                        gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
                        gdi32.DeleteObject(region)
                    if appearance is None:
                        x, y = settings.get("x"), settings.get("y")
                        left, top = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)
                        width, height = user32.GetSystemMetrics(78), user32.GetSystemMetrics(79)
                        if x is None or y is None or not (left <= x <= left+width-80 and top <= y <= top+height-40):
                            x, y = max(0, root.winfo_screenwidth()-344), 36 if kind == 'timer' else 216
                        position(x, y)
                    appearance = current
        root.after(200, tick)

    threading.Thread(target=worker, daemon=True).start()
    root.after(200, tick)
    root.mainloop()
    stopping.set()


if __name__ == "__main__":
    run_window(sys.argv[1])
