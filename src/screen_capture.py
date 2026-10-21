"""Fresh, temporary desktop images for the optional screen commentary."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from contextlib import contextmanager
import io
import re
import sys


def validate_monitor(value: str) -> str:
    if value in ("primary", "all") or re.fullmatch(r"\\\\\.\\DISPLAY\d+", value):
        return value
    raise ValueError("Выберите основной экран, все мониторы или монитор из списка")


@contextmanager
def _physical_coordinates():
    # Change only this worker thread; leave Astra and Tk's DPI handling alone.
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    setter = getattr(user32, "SetThreadDpiAwarenessContext", None)
    previous = None
    if setter:
        setter.argtypes = [ctypes.c_void_p]
        setter.restype = ctypes.c_void_p
        previous = setter(ctypes.c_void_p(-4))
    try:
        yield user32
    finally:
        if previous:
            setter(previous)


def _enumerate(user32):
    class MonitorInfo(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("monitor", wintypes.RECT),
                    ("work", wintypes.RECT), ("flags", wintypes.DWORD),
                    ("device", wintypes.WCHAR * 32)]

    monitors = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HANDLE,
                                     wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)
    user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MonitorInfo)]
    user32.GetMonitorInfoW.restype = wintypes.BOOL
    user32.EnumDisplayMonitors.argtypes = [wintypes.HDC, ctypes.c_void_p, callback_type, wintypes.LPARAM]
    user32.EnumDisplayMonitors.restype = wintypes.BOOL

    @callback_type
    def visit(handle, _dc, _rect, _data):
        info = MonitorInfo()
        info.size = ctypes.sizeof(info)
        if user32.GetMonitorInfoW(handle, ctypes.byref(info)):
            rect = info.monitor
            monitors.append({"id": info.device, "primary": bool(info.flags & 1),
                             "bounds": (rect.left, rect.top, rect.right, rect.bottom)})
        return True

    if not user32.EnumDisplayMonitors(None, None, visit, 0) or not monitors:
        raise RuntimeError("Windows не вернула доступные мониторы")
    return sorted(monitors, key=lambda m: (not m["primary"], m["id"]))


def list_monitors():
    if sys.platform != "win32":
        return [{"id": "primary", "label": "Основной экран (системное разрешение на захват)", "primary": True}]
    with _physical_coordinates() as user32:
        monitors = _enumerate(user32)
    for index, monitor in enumerate(monitors, 1):
        left, top, right, bottom = monitor["bounds"]
        monitor["label"] = f"Монитор {index}{' (основной)' if monitor['primary'] else ''} — {right-left}×{bottom-top}"
    return monitors


def capture_desktop(selection: str) -> bytes:
    """Return a JPEG, preserving physical/negative monitor coordinates."""
    validate_monitor(selection)
    if sys.platform != "win32":
        return _capture_posix(selection)
    from PIL import Image, ImageGrab

    with _physical_coordinates() as user32:
        monitors = _enumerate(user32)
        if selection == "primary":
            selected = next((m for m in monitors if m["primary"]), monitors[0])
        elif selection == "all":
            selected = None
        else:
            selected = next((m for m in monitors if m["id"] == selection), None)
            if selected is None:
                raise RuntimeError("Выбранный монитор отключён. Выберите другой в настройках")
        bounds = selected["bounds"] if selected else None
        with ImageGrab.grab(bbox=bounds, all_screens=True, include_layered_windows=True) as image:
            limit = 3840 if selection == "all" else 1920
            image.thumbnail((limit, limit), Image.Resampling.LANCZOS)
            with image.convert("RGB") as rgb:
                buffer = io.BytesIO()
                rgb.save(buffer, "JPEG", quality=85)
                return buffer.getvalue()


def _capture_posix(selection):
    from PIL import Image, ImageGrab
    import os
    import shutil
    import subprocess
    if selection not in ("primary", "all"):
        raise RuntimeError("Выберите экран заново: идентификаторы мониторов Windows недоступны в этой ОС")
    try:
        if sys.platform == "linux" and (os.environ.get("WAYLAND_DISPLAY") or os.environ.get("XDG_SESSION_TYPE") == "wayland"):
            grim = shutil.which("grim")
            if not grim:
                raise RuntimeError("Для снимков в Wayland нужен grim и поддержка screencopy композитором (Sway/Hyprland). В GNOME/KDE используйте выбор экрана браузером в VC-Discord; таймер работает без снимков.")
            result = subprocess.run([grim, "-t", "png", "-"], capture_output=True, timeout=10, check=True)
            image = Image.open(io.BytesIO(result.stdout))
        else:
            image = ImageGrab.grab(all_screens=selection == "all")
        with image:
            image.thumbnail((3840, 3840) if selection == "all" else (1920, 1920), Image.Resampling.LANCZOS)
            with image.convert("RGB") as rgb:
                buffer = io.BytesIO()
                rgb.save(buffer, "JPEG", quality=85)
                return buffer.getvalue()
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError("Не удалось сделать снимок. Проверьте разрешение на запись экрана в macOS или средство захвата текущего Linux-сеанса.") from exc
