"""Validated custom completion actions and music alarm playback."""
from __future__ import annotations

import base64
import ctypes
import json
import threading
import time

from .alarm import WmpPlayer
from .integrations import integration_call

CUSTOM_ACTIONS = {"close_app": "закрыть приложение", "hotkey": "нажать сочетание клавиш",
                  "astra_command": "запустить автоматизацию Astra", "music": "включить музыку",
                  "music_pause": "приостановить музыку Astra"}
KEYS = {"ctrl": 0x11, "control": 0x11, "alt": 0x12, "shift": 0x10, "win": 0x5B,
        "enter": 0x0D, "tab": 9, "esc": 0x1B, "escape": 0x1B, "space": 0x20,
        "backspace": 8, "delete": 0x2E, "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27}
KEYS.update({chr(n).lower(): n for n in range(65, 91)})
KEYS.update({str(n): 48+n for n in range(10)})
KEYS.update({f"f{n}": 0x6F+n for n in range(1, 25)})


def parse_hotkey(value):
    parts = str(value).lower().replace(" ", "").split("+")
    if not 1 <= len(parts) <= 5 or any(p not in KEYS for p in parts) or len(set(parts)) != len(parts):
        raise ValueError("Укажите сочетание, например Ctrl+Alt+P, Win+D или Alt+F4")
    return [KEYS[p] for p in parts]


def validate_action(action, params):
    if not isinstance(params, dict):
        raise ValueError("Некорректные параметры действия")
    params = dict(params)
    for key in ("window", "keys", "command_key"):
        if key in params:
            if not isinstance(params[key], str):
                raise ValueError("Параметр действия должен быть текстом")
            params[key] = params[key].strip()
    if action == "hotkey":
        parse_hotkey(params.get("keys", ""))
    if action == "close_app" and not str(params.get("window", "")).strip():
        raise ValueError("Выберите приложение или укажите заголовок окна")
    if action == "astra_command" and not str(params.get("command_key", "")).strip():
        raise ValueError("Укажите метку автоматизации Astra")
    if action == "music":
        params = decode_music(music_sound(params))
    return params


def music_sound(track):
    if track.get("service") not in ("yandex", "vk") or not str(track.get("track_id", "")).strip():
        raise ValueError("Выберите трек в плагине «Музыка»")
    clean = {k: track[k] for k in ("service", "track_id", "title", "artist", "extra") if k in track}
    return "music://" + base64.urlsafe_b64encode(json.dumps(clean, ensure_ascii=False).encode()).decode()


def decode_music(value):
    try:
        track = json.loads(base64.urlsafe_b64decode(value[8:]))
        music_sound(track)
        return track
    except Exception as exc:
        raise ValueError("Некорректный трек музыки") from exc


def perform_native(action, params, windows):
    user32 = ctypes.windll.user32
    if action == "close_app":
        target = windows.resolve_target(params["window"])
        matches = windows._find_windows(target, target.casefold())
        if len(matches) != 1:
            raise RuntimeError("Окно не найдено или найдено несколько окон. Укажите точный заголовок.")
        user32.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
        if not user32.PostMessageW(matches[0], 0x0010, 0, 0):
            raise RuntimeError("Windows не разрешила закрыть окно")
        return "Приложению отправлен запрос закрытия"
    keys = parse_hotkey(params["keys"])
    if params.get("window"):
        ok, _ = windows.activate(params["window"])
        if not ok:
            raise RuntimeError("Не удалось активировать выбранное окно")
    pressed = []
    try:
        for key in keys:
            user32.keybd_event(key, 0, 0, 0)
            pressed.append(key)
        time.sleep(.03)
    finally:
        for key in reversed(pressed):
            user32.keybd_event(key, 0, 2, 0)
    return "Сочетание клавиш отправлено"


class AlarmPlayer:
    def __init__(self):
        self.local = WmpPlayer()
        self.stop_event = threading.Event()
        self.revision = None
        self.lock = threading.RLock()

    def play_once(self, path, should_continue=None):
        if not path.startswith("music://"):
            return self.local.play_once(path, should_continue)
        with self.lock:
            session = threading.Event()
            self.stop_event = session
        result = integration_call("music", "play", **decode_music(path))
        revision = result.get("revision")
        if revision is None:
            raise RuntimeError("Музыкальный плагин не подтвердил запуск")
        with self.lock:
            cancelled = session.is_set() or self.stop_event is not session
            if not cancelled:
                self.revision = revision
        if cancelled:
            self._stop_music(revision)
            return True
        try:
            while not session.wait(.5):
                if should_continue and not should_continue():
                    break
                state = integration_call("music", "current")
                if state.get("revision") != revision or state.get("status") in ("ended", "stopped", "paused"):
                    break
                if state.get("status") in ("blocked", "failed"):
                    raise RuntimeError(state.get("playback_error") or "Не удалось воспроизвести музыку")
        finally:
            with self.lock:
                if self.stop_event is session:
                    session.set()
                    self.revision = None
            self._stop_music(revision)
        return True

    def stop(self):
        with self.lock:
            self.stop_event.set()
            revision, self.revision = self.revision, None
        self.local.stop()
        if revision is not None:
            self._stop_music(revision)

    @staticmethod
    def _stop_music(revision):
        # UI handlers and alarm locks must never wait for an unavailable player.
        # The music endpoint checks ownership before stopping this revision.
        def stop_owned():
            try:
                integration_call("music", "stop", revision=revision)
            except RuntimeError:
                pass
        threading.Thread(target=stop_owned, name="spt-music-stop", daemon=True).start()
