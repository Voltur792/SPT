import io
from contextlib import nullcontext

from PIL import Image, ImageGrab
import pytest

from src import screen_capture


PRIMARY = {"id": r"\\.\DISPLAY2", "primary": True, "bounds": (0, 0, 2560, 1440)}
SECONDARY = {"id": r"\\.\DISPLAY1", "primary": False, "bounds": (-1920, -100, 0, 980)}


@pytest.fixture
def displays(monkeypatch):
    monkeypatch.setattr(screen_capture.sys, "platform", "win32")
    monkeypatch.setattr(screen_capture, "_physical_coordinates", lambda: nullcontext(None))
    monkeypatch.setattr(screen_capture, "_enumerate", lambda _: [PRIMARY.copy(), SECONDARY.copy()])


@pytest.mark.parametrize("value", ["primary", "all", r"\\.\DISPLAY2", r"\\.\DISPLAY15"])
def test_monitor_selection(value):
    assert screen_capture.validate_monitor(value) == value


@pytest.mark.parametrize("value", ["", "../../screen", "0", "display1"])
def test_invalid_monitor_selection(value):
    with pytest.raises(ValueError):
        screen_capture.validate_monitor(value)


def test_monitor_list_labels_physical_size(displays):
    monitors = screen_capture.list_monitors()
    assert "основной" in monitors[0]["label"]
    assert "2560×1440" in monitors[0]["label"]
    assert monitors[1]["id"] == SECONDARY["id"]
    assert "1920×1080" in monitors[1]["label"]


@pytest.mark.parametrize("selection,bounds,limit", [
    ("primary", PRIMARY["bounds"], 1920),
    (SECONDARY["id"], SECONDARY["bounds"], 1920),
    ("all", None, 3840),
])
def test_capture_uses_physical_bounds_and_all_screens(displays, monkeypatch, selection, bounds, limit):
    calls = []
    def grab(**kwargs):
        calls.append(kwargs)
        return Image.new("RGB", (5000, 2000), "red")
    monkeypatch.setattr(ImageGrab, "grab", grab)
    with Image.open(io.BytesIO(screen_capture.capture_desktop(selection))) as image:
        assert image.format == "JPEG"
        assert max(image.size) == limit
        assert image.size[0] / image.size[1] == pytest.approx(2.5, abs=0.005)
    assert calls == [{"bbox": bounds, "all_screens": True, "include_layered_windows": True}]


def test_disconnected_monitor_never_captures_another_display(displays, monkeypatch):
    monkeypatch.setattr(ImageGrab, "grab", lambda **_: pytest.fail("must not capture another display"))
    with pytest.raises(RuntimeError, match="отключён"):
        screen_capture.capture_desktop(r"\\.\DISPLAY3")
