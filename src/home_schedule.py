"""Durable local-time device and scenario schedules."""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, date
from .alarm import default_state_path
from .integrations import integration_call


def next_occurrence(item, after):
    hour, minute = map(int, item["time"].split(":"))
    if item.get("date"):
        at = datetime.combine(date.fromisoformat(item["date"]), datetime.min.time()).replace(hour=hour, minute=minute)
        return at if at > after else None
    for offset in range(8):
        at = (after + timedelta(days=offset)).replace(hour=hour, minute=minute, second=0, microsecond=0)
        if at > after and at.weekday() in item["days"]:
            return at
    return None


class HomeSchedule:
    def __init__(self, path=None):
        self.path = path or default_state_path().with_name("home-schedule.json")
        self.error = ""
        self.load_error = ""
        try:
            rows = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(rows, list):
                raise ValueError("Некорректный формат расписания")
            self.rows = rows
            for row in self.rows:
                self.validate(row, restoring=True)
        except FileNotFoundError:
            self.rows = []
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.rows = []
            self.load_error = f"Не удалось прочитать расписание: {exc}"
        if not self.load_error:
            try:
                self._clear_completed_once()
            except OSError as exc:
                self.error = f"Не удалось очистить выполненные расписания: {exc}"

    def _clear_completed_once(self):
        previous = self.rows
        remaining = [row for row in previous if not (
            row.get("date") and not row.get("enabled")
            and row.get("result") == "Выполнено"
            and row.get("last_slot") == f'{row["date"]}T{row["time"]}'
        )]
        if len(remaining) == len(previous):
            return
        self.rows = remaining
        try:
            self.save()
        except OSError:
            self.rows = previous
            raise

    def validate(self, item, restoring=False):
        if not isinstance(item, dict):
            raise ValueError("Некорректная запись расписания")
        if restoring and (not isinstance(item.get("id"), str) or not item["id"]):
            raise ValueError("В расписании отсутствует идентификатор")
        if "enabled" in item and type(item["enabled"]) is not bool:
            raise ValueError("Некорректное состояние расписания")
        if not isinstance(item.get("time"), str):
            raise ValueError("Укажите время в формате ЧЧ:ММ")
        hour, minute = map(int, item["time"].split(":"))
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise ValueError("Время должно быть от 00:00 до 23:59")
        item["time"] = f"{hour:02d}:{minute:02d}"
        if item.get("kind") not in ("scenario", "device") or not item.get("target_id"):
            raise ValueError("Выберите устройство или сценарий")
        if item["kind"] == "device":
            if not str(item.get("capability_type", "")).startswith("devices.capabilities.") or not item.get("instance") or "value" not in item:
                raise ValueError("Выберите действие устройства")
            if item["capability_type"] in ("devices.capabilities.on_off", "devices.capabilities.toggle") and type(item["value"]) is not bool:
                raise ValueError("Для переключателя выберите включить или выключить")
            if item["capability_type"] == "devices.capabilities.range":
                import math
                if type(item["value"]) not in (int, float) or not math.isfinite(item["value"]):
                    raise ValueError("Укажите числовое значение устройства")
        if item.get("date"):
            date.fromisoformat(item["date"])
            if not restoring and item.get("enabled", True) and next_occurrence(item, datetime.now()) is None:
                raise ValueError("Выберите дату и время в будущем")
        else:
            days = item.get("days", [])
            if not isinstance(days, list) or not days or any(type(n) is not int or n not in range(7) for n in days):
                raise ValueError("Выберите хотя бы один день недели")

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.rows, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)
        self.load_error = ""

    def upsert(self, item):
        allowed = ("id", "name", "kind", "target_id", "time", "date", "days", "enabled", "capability_type", "instance", "value")
        row = {k: item[k] for k in allowed if k in item}
        self.validate(row)
        existing = next((x for x in self.rows if x["id"] == row.get("id")), None)
        row["id"] = existing["id"] if existing else uuid.uuid4().hex
        row.setdefault("enabled", True)
        row["last_slot"] = existing.get("last_slot", "") if existing else ""
        if existing and "result" in existing:
            row["result"] = existing["result"]
        previous = self.rows
        self.rows = [x for x in self.rows if x["id"] != row["id"]] + [row]
        try:
            self.save()
        except OSError:
            self.rows = previous
            raise
        return self.state()

    def remove(self, id):
        previous = self.rows
        self.rows = [x for x in self.rows if x["id"] != id]
        try:
            self.save()
        except OSError:
            self.rows = previous
            raise
        return self.state()

    def state(self):
        now = datetime.now()
        rows = []
        for row in self.rows:
            at = next_occurrence(row, now) if row.get("enabled") else None
            rows.append({**row, "next": at.isoformat() if at else None})
        # `error` is reserved by CallFromUi: even an empty value makes the SDK
        # discard all payload fields. Keep storage diagnostics inside the payload.
        return {"entries": rows, "storage_error": self.error or self.load_error}

    async def tick(self, now=None):
        self._clear_completed_once()
        now = now or datetime.now()
        slot = now.strftime("%Y-%m-%dT%H:%M")
        for row in list(self.rows):
            if not any(row is current for current in self.rows):
                continue
            if not row.get("enabled") or row.get("last_slot") == slot or row["time"] != now.strftime("%H:%M"):
                continue
            if row.get("date"):
                if row["date"] != now.date().isoformat():
                    continue
            elif now.weekday() not in row["days"]:
                continue
            previous_slot, previous_enabled = row.get("last_slot", ""), row["enabled"]
            row["last_slot"] = slot
            if row.get("date"):
                row["enabled"] = False
            # Persist the claim before a physical action; never replay on restart.
            try:
                self.save()
            except OSError:
                row["last_slot"], row["enabled"] = previous_slot, previous_enabled
                raise
            try:
                if row["kind"] == "scenario":
                    await asyncio.to_thread(integration_call, "home", "scenario", scenario_id=row["target_id"])
                else:
                    await asyncio.to_thread(integration_call, "home", "device", device_id=row["target_id"],
                                            capability_type=row["capability_type"], capability_instance=row["instance"], value=row["value"])
                row["result"] = "Выполнено"
            except Exception as exc:
                row["result"] = "Ошибка: " + str(exc)
            self.save()
            self._clear_completed_once()

    async def run(self):
        while True:
            try:
                await self.tick()
                self.error = ""
            except Exception as exc:
                self.error = str(exc)
            await asyncio.sleep(1)
