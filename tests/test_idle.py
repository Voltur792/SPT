from src.idle import IdleMonitor
import threading


def test_idle_worker_passes_custom_action_parameters(monkeypatch):
    called = threading.Event()
    calls = []
    def callback(*args):
        calls.append(args)
        called.set()
    monitor = IdleMonitor(callback)
    monkeypatch.setattr(monitor, "_idle_seconds", lambda: 60)
    monkeypatch.setattr(monitor, "_cursor_position", lambda: (1, 1))
    monkeypatch.setattr(monitor, "_update_activity", lambda *args: 60)
    try:
        monitor.start(1, "hotkey", action_params={"keys": "Win+D"})
        assert called.wait(1)
        assert calls == [("hotkey", "", {"keys": "Win+D"})]
    finally:
        monitor.stop()


def test_cursor_movement_on_negative_monitor_coordinates_resets_idle():
    monitor = IdleMonitor(lambda *_: None)
    monitor._last_activity_at = 95.0
    monitor._last_cursor_position = (100, 40)

    # System input time is stale, but moving onto a monitor left of the primary
    # display changes the virtual-screen coordinate and starts the interval over.
    assert monitor._update_activity((-1920, 40), 20, 100.0) == 0
    assert monitor._update_activity((-1910, 40), 19, 101.0) == 0
    assert monitor._update_activity((-1910, 40), 10, 111.0) == 10
    assert monitor._update_activity((-1910, 40), 0, 112.0) == 0
    assert monitor._update_activity((-1910, 40), 10, 122.0) == 10


def test_keyboard_activity_from_windows_also_resets_idle():
    monitor = IdleMonitor(lambda *_: None)
    monitor._last_activity_at = 50.0
    monitor._last_cursor_position = (200, 100)

    assert monitor._update_activity((200, 100), 0, 60.0) == 0
    assert monitor._update_activity((200, 100), 5, 65.0) == 5
