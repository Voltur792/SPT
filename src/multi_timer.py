"""Independent countdowns with stable IDs and legacy primary selection."""
import threading
import uuid


class TimerPool:
    def __init__(self, primary, factory):
        self.primary, self.factory = primary, factory
        self.rows = {"main": {"engine": primary, "name": "Таймер"}}
        self.lock = threading.RLock()

    def add(self, seconds, action, window="", name="", params=None):
        with self.lock:
            if sum(row["engine"].state()["running"] for row in self.rows.values()) >= 50:
                raise ValueError("Одновременно можно запустить до 50 таймеров")
            state = self.primary.state()
            if not state["running"] and not state.get("executing"):
                id, engine = "main", self.primary
            else:
                id, engine = uuid.uuid4().hex, self.factory()
            self.rows[id] = {"engine": engine, "name": str(name or "Таймер").strip()[:100]}
            if not engine.start(seconds, action, window, params):
                raise ValueError("Не удалось запустить таймер")
            completed = [key for key, row in self.rows.items() if key != "main" and not row["engine"].state()["running"] and not row["engine"].state().get("executing")]
            for key in completed[:-50]:
                self.rows.pop(key)
            return self.state(id)

    def selected_id(self):
        with self.lock:
            active = [(key, row["engine"].state()) for key, row in self.rows.items() if row["engine"].state()["running"]]
            return min(active, key=lambda pair: pair[1]["remaining_seconds"])[0] if active else "main"

    def get(self, id=""):
        with self.lock:
            key = id or self.selected_id()
            if key not in self.rows:
                raise ValueError("Таймер не найден")
            return self.rows[key]["engine"]

    def state(self, id=""):
        with self.lock:
            key = id or self.selected_id()
            engine = self.get(key)
            return {**engine.state(), "id": key, "name": self.rows[key]["name"]}

    def all(self):
        with self.lock:
            states = [self.state(key) for key in self.rows]
            return {"timers": [state for state in states if state["running"] or state.get("executing")],
                    "finished": [state for state in states if not state["running"] and not state.get("executing") and state.get("outcome") in ("done", "error")],
                    "selected": self.selected_id()}

    def stop(self):
        with self.lock:
            engines = [row["engine"] for row in self.rows.values()]
        for engine in engines:
            engine.stop_thread()
