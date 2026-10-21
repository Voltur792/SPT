import pytest
from src import platform_support as platform_paths


def test_platform_data_uses_absolute_xdg_only(tmp_path, monkeypatch):
    monkeypatch.setattr(platform_paths.sys, "platform", "linux")
    monkeypatch.setattr(platform_paths.Path, "home", lambda: tmp_path)
    monkeypatch.setenv("XDG_DATA_HOME", "relative/path")
    assert platform_paths.user_data() == tmp_path / ".local/share"
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    assert platform_paths.user_data() == tmp_path / "data"


def test_platform_registry_keeps_legacy_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(platform_paths.sys, "platform", "linux")
    monkeypatch.setattr(platform_paths.Path, "home", lambda: tmp_path)
    monkeypatch.delenv("SPT_INTEGRATION_DIR", raising=False)
    legacy = tmp_path / "sleep-pause-timer/integrations"
    legacy.mkdir(parents=True)
    assert platform_paths.integration_dir() == legacy
