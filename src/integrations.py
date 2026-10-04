"""Authenticated local calls to cooperating Astra plugins."""
from __future__ import annotations

import asyncio
import inspect
import json
import os
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, ProxyHandler, build_opener


def registry_dir():
    base = os.environ.get("SPT_INTEGRATION_DIR")
    return Path(base) if base else Path(os.environ.get("APPDATA") or Path.home()) / "sleep-pause-timer" / "integrations"


class IntegrationServer:
    """Publish only explicitly allowed methods, never account credentials."""

    def __init__(self, name, methods):
        self.name, self.methods = name, methods
        self.server = None
        self.token = secrets.token_urlsafe(32)

    def start(self, loop):
        if self.server:
            return
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                if self.path != "/api" or not secrets.compare_digest(self.headers.get("Authorization", ""), "Bearer " + owner.token):
                    self.send_error(403)
                    return
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                    if not 0 < size <= 65536:
                        self.send_error(413)
                        return
                    request = json.loads(self.rfile.read(size))
                    method = owner.methods.get(request.get("method"))
                    if method is None:
                        self.send_error(404)
                        return
                    params = request.get("params") or {}
                    if not isinstance(params, dict):
                        raise ValueError("Некорректные параметры")

                    async def invoke():
                        if inspect.iscoroutinefunction(method):
                            return await method(**params)
                        return await asyncio.to_thread(method, **params)

                    future = asyncio.run_coroutine_threadsafe(invoke(), loop)
                    try:
                        result = future.result(timeout=90)
                    except TimeoutError:
                        future.cancel()
                        raise RuntimeError("Плагин не ответил за 90 секунд")
                except Exception as exc:
                    result = {"error": str(exc)}
                data = json.dumps(result, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                try:
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        path = registry_dir() / (self.name + ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"port": self.server.server_port, "token": self.token}), encoding="utf-8")
        tmp.replace(path)
        threading.Thread(target=self.server.serve_forever, daemon=True, name=self.name + "-integration").start()

    def close(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
        path = registry_dir() / (self.name + ".json")
        try:
            if json.loads(path.read_text(encoding="utf-8")).get("token") == self.token:
                path.unlink()
        except (OSError, ValueError):
            pass


def integration_call(name, method, **params):
    try:
        location = json.loads((registry_dir() / (name + ".json")).read_text(encoding="utf-8"))
        port = int(location["port"])
        if not 1 <= port <= 65535:
            raise ValueError("Некорректный порт")
        request = Request(f"http://127.0.0.1:{port}/api", data=json.dumps({"method": method, "params": params}).encode(),
                          headers={"Authorization": "Bearer " + location["token"], "Content-Type": "application/json"})
        with build_opener(ProxyHandler({})).open(request, timeout=95) as response:
            result = json.load(response)
        if isinstance(result, dict) and result.get("error"):
            raise RuntimeError(str(result["error"]))
        return result
    except (OSError, ValueError, KeyError, URLError) as exc:
        label = {"music": "Музыка", "home": "Умный дом"}.get(name, name)
        raise RuntimeError(f"Нет связи с плагином «{label}». Обновите и перезапустите его в Astra.") from exc
