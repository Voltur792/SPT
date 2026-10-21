"""Native Qt/Wayland or Cocoa desktop widget; integration calls remain scoped."""
import base64
import os
from pathlib import Path
import threading
from .platform_support import webview_gui


def run_widget(title, snapshot, command, closed):
    gui = webview_gui()  # Set Qt's platform before importing the GUI backend.
    import webview
    stopping = threading.Event()
    lock = threading.RLock()
    state = {}

    class Api:
        def snapshot(self):
            with lock:
                try:
                    value = snapshot()
                    if value.get("closed") or not value.get("settings", {}).get("enabled", True):
                        window.destroy()
                        return {"closed": True}
                    settings = value.get("settings", {})
                    state.update(value)
                    if "on_top" in settings and state.get("_on_top") != settings["on_top"]:
                        try:
                            window.on_top = settings["on_top"]
                            state["_on_top"] = settings["on_top"]
                        except Exception:
                            pass  # A compositor may decline topmost; readings remain valid.
                    path = settings.get("background_path")
                    if path:
                        # The parent supplies only its managed, normalized PNG.
                        value["background"] = "data:image/png;base64," + base64.b64encode(Path(path).read_bytes()).decode("ascii")
                    return value
                except Exception:
                    return {"error": "Нет связи с Astra. Проверьте, что плагин включён."}

        def command(self, name, timer_id=""):
            if name not in ("control", "cancel", "hide"):
                raise ValueError("Неизвестное действие")
            with lock:
                return command(name, str(timer_id), state)

    html = Path(__file__).resolve().parent.parent / "ui/desktop-portable.html"
    window = webview.create_window(title, html=str(html.read_text(encoding="utf-8")), js_api=Api(), width=370, height=250,
                                   min_size=(280, 190), text_select=True, easy_drag=False,
                                   background_color="#25243b")

    def on_closing(*_):
        if not stopping.is_set():
            stopping.set()
            try:
                closed()
            except Exception:
                pass

    window.events.closing += on_closing
    webview.start(gui=gui, private_mode=True)
