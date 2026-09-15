import asyncio
import logging
from datetime import datetime, time as dtime, timedelta
from typing import Optional

from ha_client import HAClient
import storage

log = logging.getLogger("sigen_ems.scheduler")

DAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

# Must match the option text of the Sigenergy Local Modbus EMS mode select
# entity exactly - these are used both for the UI dropdown and to decide
# when discharge/charge ramping applies.
DISCHARGE_MODES = {"Command Discharging (PV First)", "Command Discharging (ESS First)"}
CHARGE_MODES = {"Command Charging (Grid First)", "Command Charging (PV First)"}

# Floor under remaining window time when computing a ramp rate, so a window
# that's about to close doesn't produce a divide-by-near-zero power spike.
MIN_RAMP_HOURS = 1 / 60


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


def _window_end_datetime(window: dict, now: datetime) -> datetime:
    """The next occurrence of this window's end time, for computing how
    much time is left in the schedule right now."""
    start_t = _parse_hhmm(window["start"])
    end_t = _parse_hhmm(window["end"])
    end_dt = datetime.combine(now.date(), end_t)
    if start_t <= end_t:
        return end_dt
    # crosses midnight: end lands "tomorrow" once we're past the start time
    if now.time() >= start_t:
        return end_dt + timedelta(days=1)
    return end_dt


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
        settings = cfg.get("settings", {})
        capacity_kwh = settings.get("battery_capacity_kwh")
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

        # The window that actually set the winning ems_mode - ramp settings
        # and the window's own start/end time are sourced from it.
        mode_window = next(
            (w for w in reversed(active) if w.get("ems_mode") == merged.get("ems_mode")),
            None,
        )

        needs_soc = merged.get("soc_stop_percent") is not None or (
            mode_window
            and (mode_window.get("discharge_ramp_enabled") or mode_window.get("charge_ramp_enabled"))
        )
        soc_value = None
        if needs_soc and entities.get("soc_sensor"):
            soc_state = await self.ha.get_state(entities["soc_sensor"])
            try:
                soc_value = float(soc_state["state"]) if soc_state else None
            except (TypeError, ValueError):
                soc_value = None

        discharge_ramp_active = bool(
            mode_window
            and merged.get("ems_mode") in DISCHARGE_MODES
            and mode_window.get("discharge_ramp_enabled")
        )

        consumed_kw = None
        if discharge_ramp_active and entities.get("consumption_sensor"):
            consumption_state = await self.ha.get_state(entities["consumption_sensor"])
            try:
                consumed_kw = (
                    float(consumption_state["state"]) / self.power_entity_scale
                    if consumption_state
                    else None
                )
            except (TypeError, ValueError):
                consumed_kw = None

        # SoC-based discharge cutoff: if configured and battery has reached
        # the floor, force discharge power to 0 regardless of the window.
        # Skipped when the discharge ramp is active for this window - the
        # ramp handles its own cutoff (zeroing the export limit only,
        # leaving the configured discharge power untouched).
        if (
            merged.get("soc_stop_percent") is not None
            and soc_value is not None
            and not discharge_ramp_active
        ):
            if soc_value <= merged["soc_stop_percent"]:
                merged["discharge_power_kw"] = 0
                status["actions"].append(
                    f"SoC {soc_value}% <= cutoff {merged['soc_stop_percent']}% - discharge held at 0kW"
                )

        if mode_window and soc_value is not None and capacity_kwh:
            mode = merged.get("ems_mode")
            if (
                mode in DISCHARGE_MODES
                and mode_window.get("discharge_ramp_enabled")
                and merged.get("soc_stop_percent") is not None
            ):
                self._apply_discharge_ramp(
                    mode_window, merged, soc_value, capacity_kwh, now, status, consumed_kw
                )
            elif (
                mode in CHARGE_MODES
                and mode_window.get("charge_ramp_enabled")
                and mode_window.get("charge_target_percent") is not None
            ):
                self._apply_charge_ramp(mode_window, merged, soc_value, capacity_kwh, now, status)

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

    def _apply_discharge_ramp(
        self,
        window: dict,
        merged: dict,
        soc_value: float,
        capacity_kwh: float,
        now: datetime,
        status: dict,
        consumed_kw: Optional[float] = None,
    ) -> None:
        """Instead of exporting at a flat (often high) power for the whole
        window then hard-stopping at the SoC floor, pace the export limit
        so it lands on the SoC target right as the window ends. Only the
        export limit is touched - the schedule's discharge power setting
        (if any) is left as configured.

        If a home consumption reading is available, it's subtracted from
        the target total discharge rate to get the export limit, since the
        battery's total output covers house load first and only the
        surplus is actually exported. The target total discharge rate is
        capped at the schedule's discharge power ceiling *before* that
        subtraction, so (export limit + consumption) never asks the
        battery for more than it's configured to put out.
        """
        soc_target = merged["soc_stop_percent"]
        if soc_value <= soc_target:
            merged["export_limit_kw"] = 0
            return
        end_dt = _window_end_datetime(window, now)
        remaining_hours = max((end_dt - now).total_seconds() / 3600, MIN_RAMP_HOURS)
        energy_kwh = (soc_value - soc_target) / 100 * capacity_kwh
        required_total_kw = energy_kwh / remaining_hours
        ceiling = merged.get("discharge_power_kw")
        if ceiling is not None:
            required_total_kw = min(required_total_kw, ceiling)
        required_total_kw = max(required_total_kw, 0)

        if consumed_kw is not None and consumed_kw > 0:
            export_kw = max(required_total_kw - consumed_kw, 0)
            merged["export_limit_kw"] = round(export_kw, 3)
            status["actions"].append(
                f"Discharge ramp: {soc_value:.1f}% -> {soc_target}% over {remaining_hours:.2f}h -> "
                f"target output {required_total_kw:.2f}kW - consumption {consumed_kw:.2f}kW "
                f"-> export limit {export_kw:.2f}kW"
            )
        else:
            merged["export_limit_kw"] = round(required_total_kw, 3)
            status["actions"].append(
                f"Discharge ramp: {soc_value:.1f}% -> {soc_target}% over {remaining_hours:.2f}h "
                f"-> export limit {required_total_kw:.2f}kW"
            )

    def _apply_charge_ramp(
        self,
        window: dict,
        merged: dict,
        soc_value: float,
        capacity_kwh: float,
        now: datetime,
        status: dict,
    ) -> None:
        """Same idea as the discharge ramp, but for charging up to a target
        SoC by the end of the window instead of a flat import limit. Only
        the import limit is touched - the schedule's charge power setting
        (if any) is left as configured."""
        soc_target = window["charge_target_percent"]
        if soc_value >= soc_target:
            merged["import_limit_kw"] = 0
            return
        end_dt = _window_end_datetime(window, now)
        remaining_hours = max((end_dt - now).total_seconds() / 3600, MIN_RAMP_HOURS)
        energy_kwh = (soc_target - soc_value) / 100 * capacity_kwh
        required_kw = energy_kwh / remaining_hours
        ceiling = merged.get("charge_power_kw")
        if ceiling is not None:
            required_kw = min(required_kw, ceiling)
        required_kw = max(required_kw, 0)
        merged["import_limit_kw"] = round(required_kw, 3)
        status["actions"].append(
            f"Charge ramp: {soc_value:.1f}% -> {soc_target}% over {remaining_hours:.2f}h -> import limit {required_kw:.2f}kW"
        )

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
