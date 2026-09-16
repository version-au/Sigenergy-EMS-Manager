import json
import logging
import re
from pathlib import Path
from threading import RLock

log = logging.getLogger("sigen_ems.storage")

DATA_DIR = Path("/data")
CONFIG_PATH = DATA_DIR / "config.json"

# RLock (not Lock): load_config() calls save_config() while still holding
# the lock on first run (when config.json doesn't exist yet). A plain Lock
# would deadlock the whole asyncio event loop the first time that happens.
_lock = RLock()

DEFAULT_CONFIG = {
    "entities": {
        "ems_mode_select": "",
        "ems_mode_enable_switch": "",
        "discharge_limit_number": "",
        "charge_limit_number": "",
        "soc_sensor": "",
        "import_limit_number": "",
        "export_limit_number": "",
        "consumption_sensor": "",
    },
    "settings": {
        "battery_capacity_kwh": None,
    },
    "windows": [],
    "status": {},
}

# A window saved by a buggy frontend (pre-0.5.1) that failed to generate an
# id for a brand-new schedule ended up with the literal string "undefined"
# stored as its id. These count as "missing" for the purposes of repair.
_BAD_IDS = {"", "undefined", "null", "none", "None"}


def _slugify(name: str, existing_ids: set) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-") or "window"
    candidate = base
    n = 1
    while candidate in existing_ids:
        candidate = f"{base}-{n}"
        n += 1
    existing_ids.add(candidate)
    return candidate


def _repair_window_ids(windows: list) -> bool:
    """Regenerate any missing/"undefined" window id from its name. Returns
    True if anything changed, so the caller knows to persist the fix."""
    existing_ids = {w.get("id") for w in windows if w.get("id") not in _BAD_IDS}
    changed = False
    for w in windows:
        if w.get("id") in _BAD_IDS:
            old_id = w.get("id")
            w["id"] = _slugify(w.get("name", ""), existing_ids)
            log.warning(
                "Repaired window with missing/invalid id (%r) -> %r ('%s')",
                old_id, w["id"], w.get("name", ""),
            )
            changed = True
    return changed


def _migrate_export_limit_field(windows: list) -> bool:
    """Pre-0.6 windows had a single flat "export_limit_kw" field. That's
    now split into min_export_limit_kw / max_export_limit_kw, so carry any
    old value over as the max (a sensible ceiling) if not already set."""
    changed = False
    for w in windows:
        if "export_limit_kw" in w:
            old_value = w.pop("export_limit_kw")
            if old_value is not None and w.get("max_export_limit_kw") is None:
                w["max_export_limit_kw"] = old_value
            changed = True
    return changed


def load_config() -> dict:
    with _lock:
        if not CONFIG_PATH.exists():
            save_config(DEFAULT_CONFIG)
            return json.loads(json.dumps(DEFAULT_CONFIG))
        try:
            with open(CONFIG_PATH, "r") as f:
                data = json.load(f)
            # backfill any keys added in later versions
            for key, val in DEFAULT_CONFIG.items():
                data.setdefault(key, val)
            for key, val in DEFAULT_CONFIG["entities"].items():
                data["entities"].setdefault(key, val)
            for key, val in DEFAULT_CONFIG["settings"].items():
                data["settings"].setdefault(key, val)
            needs_save = _repair_window_ids(data["windows"])
            needs_save = _migrate_export_limit_field(data["windows"]) or needs_save
            if needs_save:
                save_config(data)
            return data
        except (json.JSONDecodeError, OSError) as exc:
            log.error("Failed to read config, falling back to defaults: %s", exc)
            return json.loads(json.dumps(DEFAULT_CONFIG))


def save_config(data: dict) -> None:
    with _lock:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp_path = CONFIG_PATH.with_suffix(".tmp")
        with open(tmp_path, "w") as f:
            json.dump(data, f, indent=2)
        tmp_path.replace(CONFIG_PATH)
