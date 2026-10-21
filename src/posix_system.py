"""Desktop actions. Commands are fixed argument lists, never shell expressions."""
import ctypes
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys


def wayland():
    return bool(os.environ.get("WAYLAND_DISPLAY") or os.environ.get("XDG_SESSION_TYPE") == "wayland")


def run(args, *, input=None):
    try:
        return subprocess.run(args, input=input, capture_output=True, text=True, encoding="utf-8", timeout=12, check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"Система не разрешила действие ({Path(args[0]).name}). Проверьте зависимости и разрешения.") from exc


def tool(name):
    path = shutil.which(name)
    if not path:
        raise RuntimeError(f"Для этого действия установите {name}")
    return path


def media_pause():
    if sys.platform == "darwin":
        run(["/usr/bin/osascript", "-e", 'if application "Spotify" is running then\n tell application "Spotify" to playpause\nelse if application "Music" is running then\n tell application "Music" to playpause\nelse\n error "Нет активного музыкального приложения"\nend if'])
    else:
        run([tool("playerctl"), "play-pause"])


def power(action):
    if action not in ("shutdown", "sleep"):
        raise ValueError("Неизвестное действие питания")
    if sys.platform == "darwin":
        if action == "sleep":
            run(["/usr/bin/pmset", "sleepnow"])
        else:
            run(["/usr/bin/osascript", "-e", 'tell application "System Events" to shut down'])
    else:
        run([tool("systemctl"), "poweroff" if action == "shutdown" else "suspend"])


def _sway_leaves(node):
    result = []
    if node.get("pid") and node.get("name") and node.get("type") == "con":
        result.append((f"sway:{int(node['id'])}", node["name"]))
    for child in node.get("nodes", []) + node.get("floating_nodes", []):
        result.extend(_sway_leaves(child))
    return result


def windows():
    if sys.platform == "darwin":
        script = '''const s=Application("System Events"); JSON.stringify(s.applicationProcesses.whose({backgroundOnly:false})().flatMap(p=>{try{return p.windows().map((w,i)=>[String(p.unixId())+":"+String(i),p.name()+" · "+w.name()]);}catch(e){return []}}));'''
        return [tuple(row) for row in json.loads(run(["/usr/bin/osascript", "-l", "JavaScript", "-e", script]))]
    if wayland():
        if os.environ.get("SWAYSOCK"):
            return _sway_leaves(json.loads(run([tool("swaymsg"), "-t", "get_tree", "-r"])))
        if os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
            return [("hypr:" + row["address"], row.get("title", "")) for row in json.loads(run([tool("hyprctl"), "clients", "-j"])) if row.get("mapped")]
        raise RuntimeError("Список и активация чужих окон в этом Wayland-сеансе недоступны. Поддерживаются Sway/Hyprland; таймер и управление музыкой работают отдельно.")
    rows = []
    for line in run([tool("wmctrl"), "-l"]).splitlines():
        parts = line.split(None, 3)
        if len(parts) == 4:
            rows.append((parts[0], parts[3]))
    return rows


def window_action(ident, close=False):
    ident = str(ident)
    if sys.platform == "darwin":
        if not re.fullmatch(r"\d+:\d+", ident):
            raise ValueError("Некорректный идентификатор окна")
        script = '''function run(a){const s=Application("System Events"),p=s.applicationProcesses.whose({unixId:Number(a[0])})()[0]; if(!p)throw Error("Окно закрыто"); const w=p.windows()[Number(a[1])]; if(!w)throw Error("Окно закрыто"); if(a[2]==="close") w.buttons.whose({subrole:"AXCloseButton"})()[0].click(); else {p.frontmost=true;w.actions.byName("AXRaise").perform();}}'''
        pid, index = ident.split(":")
        run(["/usr/bin/osascript", "-l", "JavaScript", "-e", script, pid, index, "close" if close else "focus"])
    elif ident.startswith("sway:") and re.fullmatch(r"sway:\d+", ident):
        result = json.loads(run([tool("swaymsg"), "-r", f"[con_id={ident[5:]}] {'kill' if close else 'focus'}"]))
        if not result or not all(row.get("success") for row in result):
            raise RuntimeError("Композитор не выполнил действие с окном")
    elif ident.startswith("hypr:") and re.fullmatch(r"hypr:0x[0-9a-fA-F]+", ident):
        result = run([tool("hyprctl"), "dispatch", "closewindow" if close else "focuswindow", "address:" + ident[5:]])
        if result != "ok":
            raise RuntimeError("Композитор не выполнил действие с окном")
    elif not wayland() and re.fullmatch(r"0x[0-9a-fA-F]+", ident):
        run([tool("wmctrl"), "-ic" if close else "-ia", ident])
    else:
        raise RuntimeError("Окно больше не доступно в текущем сеансе")


def foreground_title():
    if sys.platform == "darwin":
        return run(["/usr/bin/osascript", "-e", 'tell application "System Events" to get name of first application process whose frontmost is true'])
    if wayland():
        raise RuntimeError("Заголовок активного окна Wayland не предоставляется")
    return run([tool("xdotool"), "getactivewindow", "getwindowname"])


def hotkey(value):
    keys = value.lower().replace(" ", "").split("+")
    # parse_hotkey in actions.py validates this before entering the backend.
    if sys.platform == "darwin":
        special = {"enter":36,"tab":48,"esc":53,"escape":53,"space":49,"backspace":51,"delete":117,
                   "left":123,"right":124,"down":125,"up":126}
        modifiers = {"ctrl":"control down","control":"control down","alt":"option down","shift":"shift down","win":"command down"}
        final, mods = keys[-1], keys[:-1]
        if any(k not in modifiers for k in mods):
            raise ValueError("Модификаторы должны стоять перед основной клавишей")
        using = " using {" + ", ".join(modifiers[k] for k in mods) + "}" if mods else ""
        if final in special:
            command = f"key code {special[final]}"
        elif re.fullmatch(r"[a-z0-9]", final):
            command = "keystroke " + json.dumps(final)
        else:
            raise RuntimeError("Это сочетание клавиш пока не поддерживается в macOS")
        run(["/usr/bin/osascript", "-e", 'tell application "System Events" to ' + command + using])
        return
    aliases = {"control":"ctrl","win":"super","enter":"Return","esc":"Escape","escape":"Escape","backspace":"BackSpace","delete":"Delete",
               "up":"Up","down":"Down","left":"Left","right":"Right","tab":"Tab","space":"space"}
    final = aliases.get(keys[-1], keys[-1].upper() if keys[-1].startswith("f") else keys[-1])
    if wayland():
        args = [tool("wtype")]
        for key in keys[:-1]:
            if key not in ("ctrl", "control", "alt", "shift", "win"):
                raise ValueError("Некорректный модификатор")
            args += ["-M", aliases.get(key, key)]
        args += ["-P", final, "-p", final]
        for key in reversed(keys[:-1]):
            args += ["-m", aliases.get(key, key)]
        run(args)
    else:
        run([tool("xdotool"), "key", "--clearmodifiers", "+".join(aliases.get(k, k) for k in keys[:-1]) + ("+" if len(keys)>1 else "") + final])


def idle_seconds():
    if sys.platform == "darwin":
        output = run(["/usr/sbin/ioreg", "-c", "IOHIDSystem"])
        match = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', output)
        if not match:
            raise RuntimeError("macOS не вернула время бездействия")
        return int(match[1]) // 1_000_000_000
    if wayland():
        # KDE's GetSessionIdleTime explicitly returns NotSupported on Wayland.
        # Never substitute the idle time of XWayland: it misses native input.
        raise RuntimeError("Отслеживание бездействия в этом Wayland-сеансе не настроено. Обычные таймеры доступны.")
    return int(run([tool("xprintidle")])) // 1000


def cursor_position():
    if sys.platform == "darwin" or wayland():
        raise RuntimeError("Положение курсора не используется; активность определяется временем последнего ввода")
    text = run([tool("xdotool"), "getmouselocation", "--shell"])
    values = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
    return int(values["X"]), int(values["Y"])
