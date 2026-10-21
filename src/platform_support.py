"""Per-user paths and native webview setup, with no GUI imports at startup."""
import os
import sys
from pathlib import Path


def xdg(name, fallback):
    value = Path(os.environ.get(name) or fallback)
    return value if value.is_absolute() else Path(fallback)


def user_data():
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData/Roaming")
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support"
    return xdg("XDG_DATA_HOME", Path.home() / ".local/share")


def astra_config():
    if os.environ.get("ASTRA_CONFIG_DIR"):
        return Path(os.environ["ASTRA_CONFIG_DIR"])
    if sys.platform == "win32":
        return user_data() / "astra/astra/config"
    if sys.platform == "darwin":
        return user_data() / "com.astra.astra"
    return xdg("XDG_CONFIG_HOME", Path.home() / ".config") / "astra"


def integration_dir():
    if os.environ.get("SPT_INTEGRATION_DIR"):
        return Path(os.environ["SPT_INTEGRATION_DIR"])
    legacy = Path.home() / "sleep-pause-timer/integrations"
    if sys.platform != "win32" and legacy.is_dir():
        return legacy
    return user_data() / "sleep-pause-timer/integrations"


def webview_gui():
    requested = os.environ.get("PYWEBVIEW_GUI")
    if requested:
        return requested
    if sys.platform == "win32":
        return "edgechromium"
    if sys.platform == "darwin":
        return "cocoa"
    # Qt uses its native Wayland platform plugin, not XWayland. Respect an
    # explicit QT_QPA_PLATFORM override rather than changing the user's session.
    if os.environ.get("WAYLAND_DISPLAY") or os.environ.get("XDG_SESSION_TYPE") == "wayland":
        os.environ.setdefault("QT_QPA_PLATFORM", "wayland")
    os.environ.setdefault("PYWEBVIEW_GUI", "qt")
    return "qt"
