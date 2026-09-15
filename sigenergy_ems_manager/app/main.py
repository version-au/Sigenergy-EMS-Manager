import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ha_client import HAClient
from scheduler import Scheduler
import storage

OPTIONS_PATH = Path("/data/options.json")


def load_addon_options() -> dict:
    defaults = {
        "poll_interval_seconds": 30,
        "log_level": "info",
        # "W" if your Sigenergy number entities are natively in Watts (the
        # UI always works in kW and converts); set to "kW" if your entities
        # are already natively in kW so no conversion should happen.
        "power_entities_unit": "W",
    }
    if OPTIONS_PATH.exists():
        try:
            with open(OPTIONS_PATH) as f:
                defaults.update(json.load(f))
        except (json.JSONDecodeError, OSError):
            pass
    return defaults


options = load_addon_options()

logging.basicConfig(
    level=getattr(logging, str(options.get("log_level", "info")).upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("sigen_ems.main")

ha_client = HAClient()
_power_scale = 1000.0 if str(options.get("power_entities_unit", "W")).upper() == "W" else 1.0
scheduler = Scheduler(
    ha_client,
    poll_interval=int(options.get("poll_interval_seconds", 30)),
    power_entity_scale=_power_scale,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler.start()
    yield
    await scheduler.stop()
    await ha_client.close()


app = FastAPI(title="Sigenergy EMS Manager", lifespan=lifespan)

STATIC_DIR = Path(__file__).parent / "static"


class EntitiesPayload(BaseModel):
    ems_mode_select: str = ""
    ems_mode_enable_switch: str = ""
    discharge_limit_number: str = ""
    charge_limit_number: str = ""
    soc_sensor: str = ""
    import_limit_number: str = ""
    export_limit_number: str = ""


class SettingsPayload(BaseModel):
    battery_capacity_kwh: float | None = None


class Window(BaseModel):
    id: str
    name: str
    enabled: bool = True
    days: list[str] = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    start: str
    end: str
    ems_mode: str | None = None
    charge_power_kw: float | None = None
    discharge_power_kw: float | None = None
    soc_stop_percent: float | None = None
    import_limit_kw: float | None = None
    export_limit_kw: float | None = None
    discharge_ramp_enabled: bool = False
    charge_ramp_enabled: bool = False
    charge_target_percent: float | None = None
    notify_enabled: bool = False
    notify_service: str | None = None
    notify_title: str | None = None
    notify_message: str | None = None


class WindowsPayload(BaseModel):
    windows: list[Window]


@app.get("/api/config")
async def get_config():
    return storage.load_config()


@app.post("/api/entities")
async def set_entities(payload: EntitiesPayload):
    cfg = storage.load_config()
    cfg["entities"] = payload.model_dump()
    storage.save_config(cfg)
    return cfg


@app.post("/api/settings")
async def set_settings(payload: SettingsPayload):
    cfg = storage.load_config()
    cfg["settings"] = payload.model_dump()
    storage.save_config(cfg)
    return cfg


@app.post("/api/windows")
async def set_windows(payload: WindowsPayload):
    cfg = storage.load_config()
    cfg["windows"] = [w.model_dump() for w in payload.windows]
    storage.save_config(cfg)
    return cfg


@app.get("/api/status")
async def get_status():
    cfg = storage.load_config()
    return cfg.get("status", {})


@app.get("/api/ha/entities")
async def search_entities(q: str = ""):
    """Cheap entity picker: lists sensor/select/number/switch entities whose
    entity_id contains the query string, so the UI can offer suggestions
    without you having to type exact entity IDs from memory."""
    states = await ha_client.get_all_states()
    if states is None:
        raise HTTPException(status_code=502, detail="Could not reach Home Assistant")
    q_lower = q.lower()
    matches = [
        {"entity_id": s["entity_id"], "state": s["state"]}
        for s in states
        if s["entity_id"].split(".")[0] in ("sensor", "select", "number", "switch")
        and q_lower in s["entity_id"].lower()
    ]
    return matches[:50]


app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")


@app.middleware("http")
async def no_cache_static(request, call_next):
    """Static assets (index.html/app.js/style.css) were being cached
    indefinitely by browsers inside the ingress iframe, so UI updates after
    an add-on update didn't show up without a hard refresh. Force
    revalidation on every request for anything that isn't already an API
    call, without touching the API responses themselves."""
    response = await call_next(request)
    if not request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8099, log_level=str(options.get("log_level", "info")))
