import json
import pytest
from src import posix_system as desktop
from src import platform_support as paths


def test_power_commands_are_native_argument_lists_without_shell(monkeypatch):
    commands = []
    monkeypatch.setattr(desktop, "run", lambda args, **kw: commands.append(args))
    monkeypatch.setattr(desktop, "tool", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(desktop.sys, "platform", "linux")
    desktop.power("sleep")
    desktop.power("shutdown")
    assert commands == [["/usr/bin/systemctl", "suspend"], ["/usr/bin/systemctl", "poweroff"]]
    monkeypatch.setattr(desktop.sys, "platform", "darwin")
    desktop.power("sleep")
    assert commands[-1] == ["/usr/bin/pmset", "sleepnow"]
    with pytest.raises(ValueError):
        desktop.power("sleep; echo arbitrary")


def test_sway_windows_and_close_do_not_kill_process_by_title(monkeypatch):
    monkeypatch.setattr(desktop.sys, "platform", "linux")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setenv("SWAYSOCK", "/run/fake")
    monkeypatch.setattr(desktop, "tool", lambda name: name)
    tree = {"nodes": [{"type":"con", "id":123, "pid":42, "name":"Музыка", "nodes": []}], "floating_nodes": []}
    calls = []
    def run(args):
        calls.append(args)
        return json.dumps(tree) if "get_tree" in args else '[{"success":true}]'
    monkeypatch.setattr(desktop, "run", run)
    assert desktop.windows() == [("sway:123", "Музыка")]
    desktop.window_action("sway:123", close=True)
    assert calls[-1] == ["swaymsg", "-r", "[con_id=123] kill"]
    with pytest.raises(RuntimeError):
        desktop.window_action("sway:123] exec shutdown")


def test_unavailable_wayland_idle_fails_instead_of_counting_false_idle(monkeypatch):
    monkeypatch.setattr(desktop.sys, "platform", "linux")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "GNOME")
    with pytest.raises(RuntimeError, match="Wayland"):
        desktop.idle_seconds()


def test_mac_idle_nanoseconds_and_x11_milliseconds_are_seconds(monkeypatch):
    monkeypatch.setattr(desktop.sys, "platform", "darwin")
    monkeypatch.setattr(desktop, "run", lambda args: '"HIDIdleTime" = 12000000000')
    assert desktop.idle_seconds() == 12
    monkeypatch.setattr(desktop.sys, "platform", "linux")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    monkeypatch.setattr(desktop, "tool", lambda name: name)
    monkeypatch.setattr(desktop, "run", lambda args: "12000")
    assert desktop.idle_seconds() == 12


def test_linux_and_mac_paths_share_integration_contract(tmp_path, monkeypatch):
    monkeypatch.setattr(paths.sys, "platform", "linux")
    monkeypatch.setattr(paths.Path, "home", lambda: tmp_path / "home")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.delenv("SPT_INTEGRATION_DIR", raising=False)
    assert paths.integration_dir() == tmp_path / "data/sleep-pause-timer/integrations"
    monkeypatch.setattr(paths.sys, "platform", "darwin")
    assert paths.user_data() == tmp_path / "home/Library/Application Support"


def test_wayland_gui_preserves_explicit_platform_override(monkeypatch):
    monkeypatch.setattr(paths.sys, "platform", "linux")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.delenv("PYWEBVIEW_GUI", raising=False)
    monkeypatch.delenv("QT_QPA_PLATFORM", raising=False)
    assert paths.webview_gui() == "qt"
    assert paths.os.environ["QT_QPA_PLATFORM"] == "wayland"
    monkeypatch.setenv("QT_QPA_PLATFORM", "xcb")
    assert paths.webview_gui() == "qt"
    assert paths.os.environ["QT_QPA_PLATFORM"] == "xcb"
