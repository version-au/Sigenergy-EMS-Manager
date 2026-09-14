import json
import logging
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
    },
    "windows": [],
    "status": {},
}


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
