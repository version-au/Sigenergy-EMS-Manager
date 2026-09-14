import asyncio
import logging
from datetime import datetime, time as dtime
from typing import Optional

from ha_client import HAClient
import storage

log = logging.getLogger("sigen_ems.scheduler")

DAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _parse_hhmm(value: str) -> dtime:
    h, m = value.split(":")
    return dtime(hour=int(h), minute=int(m))


def _window_is_active(window: dict, now: datetime) -> bool:
    if not window.get("enabled", True):
        return False
    today = DAY_KEYS[now.weekday()]
    if today not in window.get("days", DAY_KEYS):
        return False
    start = _parse_hhmm(window["start"])
    end = _parse_hhmm(window["end"])
    now_t = now.time()
    if start <= end:
        return start <= now_t < end
    # window crosses midnight
    return now_t >= start or now_t < end


def _active_windows(windows: list[dict], now: datetime) -> list[dict]:
    return [w for w in windows if _window_is_active(w, now)]


class Scheduler:
    def __init__(
        self, ha: HAClient, poll_interval: int = 30, power_entity_scale: float = 1000.0
    ) -> None:
        self.ha = ha
        self.poll_interval = poll_interval
        # The UI works in kW. Sigenergy's Modbus number entities for power
        # limits are natively in W, so we multiply by this scale (1000) when
        # writing, and divide by it when reading, to keep the UI in kW. Set
        # this to 1 (via the add-on's power_entities_unit option) if your
        # entities are already natively in kW.
        self.power_entity_scale = power_entity_scale
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        # Tracks which window ids were active on the previous tick, so
        # notifications fire once on the transition into "active" rather
        # than repeatedly on every poll while a window stays active.
        self._previously_active: set[str] = set()

    def start(self) -> None:
        if self._task is None:
            self._stop.clear()
            self._task = asyncio.create_task(self._run())
            log.info("Scheduler started (poll every %ss)", self.poll_interval)

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await self._task
            self._task = None

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self._tick()
            except Exception:
                log.exception("Scheduler tick failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll_interval)
            except asyncio.TimeoutError:
                pass

    async def _tick(self) -> None:
        cfg = storage.load_config()
        entities = cfg["entities"]
        now = datetime.now()
        active = _active_windows(cfg.get("windows", []), now)
        active_ids = {w["id"] for w in active}

        status = {
            "last_run": now.isoformat(timespec="seconds"),
            "active_window_ids": [w["id"] for w in active],
            "active_window_names": [w.get("name") or w["id"] for w in active],
            "actions": [],
            "errors": [],
        }

        newly_active = active_ids - self._previously_active
        if newly_active:
            for w in active:
                if w["id"] in newly_active and w.get("notify_enabled"):
                    await self._send_notification(w, status)

        if not active:
            self._previously_active = active_ids
            cfg["status"] = status
            storage.save_config(cfg)
            return

        # Later windows in the list win on conflicting fields.
        merged: dict = {}
        for w in active:
            for key in (
                "ems_mode",
                "charge_power_kw",
                "discharge_power_kw",
                "soc_stop_percent",
                "import_limit_kw",
                "export_limit_kw",
            ):
                if w.get(key) is not None:
                    merged[key] = w[key]

        # SoC-based discharge cutoff: if configured and battery has reached
        # the floor, force discharge power to 0 regardless of the window.
        if merged.get("soc_stop_percent") is not None and entities.get("soc_sensor"):
            soc_state = await self.ha.get_state(entities["soc_sensor"])
            try:
                soc_value = float(soc_state["state"]) if soc_state else None
            except (TypeError, ValueError):
                soc_value = None
            if soc_value is not None and soc_value <= merged["soc_stop_percent"]:
                merged["discharge_power_kw"] = 0
                status["actions"].append(
                    f"SoC {soc_value}% <= cutoff {merged['soc_stop_percent']}% - discharge held at 0kW"
                )

        await self._apply_ems_mode(entities, merged, status)
        await self._apply_number(
            entities.get("charge_limit_number"),
            merged.get("charge_power_kw"),
            status,
            "charge limit",
            unit="kW",
            scale_to_entity=self.power_entity_scale,
        )
        await self._apply_number(
            entities.get("discharge_limit_number"),
            merged.get("discharge_power_kw"),
            status,
            "discharge limit",
            unit="kW",
            scale_to_entity=self.power_entity_scale,
        )
        await self._apply_number(
            entities.get("import_limit_number"),
            merged.get("import_limit_kw"),
            status,
            "import limit",
            unit="kW",
            scale_to_entity=self.power_entity_scale,
        )
        await self._apply_number(
            entities.get("export_limit_number"),
            merged.get("export_limit_kw"),
            status,
            "export limit",
            unit="kW",
            scale_to_entity=self.power_entity_scale,
        )

        cfg["status"] = status
        storage.save_config(cfg)
        self._previously_active = active_ids

    async def _send_notification(self, window: dict, status: dict) -> None:
        service_full = (window.get("notify_service") or "").strip()
        if not service_full or "." not in service_full:
            return
        domain, service = service_full.split(".", 1)
        message = window.get("notify_message") or f"{window.get('name', window['id'])} schedule is now active"
        payload = {"message": message}
        if window.get("notify_title"):
            payload["title"] = window["notify_title"]
        ok = await self.ha.call_raw_service(domain, service, payload)
        (status["actions"] if ok else status["errors"]).append(
            f"Notification ({service_full}) for '{window.get('name', window['id'])}': {'sent' if ok else 'failed'}"
        )

    async def _apply_ems_mode(self, entities: dict, merged: dict, status: dict) -> None:
        entity_id = entities.get("ems_mode_select")
        desired = merged.get("ems_mode")
        if not entity_id or not desired:
            return
        current = await self.ha.get_state(entity_id)
        current_option = current["state"] if current else None
        if current_option == desired:
            return
        ok = await self.ha.select_option(entity_id, desired)
        (status["actions"] if ok else status["errors"]).append(
            f"EMS mode: {current_option!r} -> {desired!r}"
        )

    async def _apply_number(
        self,
        entity_id: Optional[str],
        desired: Optional[float],
        status: dict,
        label: str,
        unit: str = "",
        scale_to_entity: float = 1.0,
    ) -> None:
        """desired is in UI units (e.g. kW); scale_to_entity converts UI
        units to the entity's native units (e.g. 1000 for kW -> W)."""
        if not entity_id or desired is None:
            return
        current = await self.ha.get_state(entity_id)
        try:
            current_native = float(current["state"]) if current else None
        except (TypeError, ValueError):
            current_native = None
        desired_native = desired * scale_to_entity
        # Compare in native units, but tolerate float rounding.
        if current_native is not None and abs(current_native - desired_native) < 1e-6:
            return
        ok = await self.ha.set_number(entity_id, desired_native)
        current_display = (
            current_native / scale_to_entity if current_native is not None else None
        )
        (status["actions"] if ok else status["errors"]).append(
            f"{label}: {current_display}{unit} -> {desired}{unit}"
        )
