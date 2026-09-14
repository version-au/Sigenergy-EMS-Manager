"""
Thin async wrapper around the Home Assistant Core REST API, reached through
the Supervisor proxy that add-ons get for free when `homeassistant_api: true`
is set in config.yaml.
"""
import os
import logging
from typing import Any, Optional

import httpx

log = logging.getLogger("sigen_ems.ha_client")

SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")
BASE_URL = "http://supervisor/core/api"


class HAClient:
    def __init__(self) -> None:
        headers = {"Content-Type": "application/json"}
        if SUPERVISOR_TOKEN:
            headers["Authorization"] = f"Bearer {SUPERVISOR_TOKEN}"
        else:
            # Sending "Bearer " with an empty token is an illegal header
            # value and crashes httpx outright, which is far more confusing
            # than a clean 401 from Home Assistant. Omit the header instead
            # and log loudly - if you see this, the add-on's environment
            # isn't passing SUPERVISOR_TOKEN through (e.g. a base image that
            # strips env vars from its CMD process; homeassistant_api: true
            # must also be set in config.yaml).
            log.error(
                "SUPERVISOR_TOKEN is empty - calls to Home Assistant will fail. "
                "Check config.yaml has 'homeassistant_api: true' and that the "
                "base image doesn't strip container env vars from the app process."
            )
        self._client = httpx.AsyncClient(
            base_url=BASE_URL,
            headers=headers,
            timeout=10.0,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def get_state(self, entity_id: str) -> Optional[dict]:
        if not entity_id:
            return None
        try:
            resp = await self._client.get(f"/states/{entity_id}")
            if resp.status_code == 404:
                log.warning("Entity not found: %s", entity_id)
                return None
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as exc:
            log.error("Failed to get state for %s: %s", entity_id, exc)
            return None

    async def get_states(self, entity_ids: list[str]) -> dict[str, Optional[dict]]:
        results = {}
        for eid in entity_ids:
            if eid:
                results[eid] = await self.get_state(eid)
        return results

    async def get_all_states(self) -> Optional[list[dict]]:
        try:
            resp = await self._client.get("/states")
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as exc:
            log.error("Failed to list states: %s", exc)
            return None

    async def call_service(
        self, domain: str, service: str, entity_id: str, extra: Optional[dict] = None
    ) -> bool:
        payload: dict[str, Any] = {"entity_id": entity_id}
        if extra:
            payload.update(extra)
        try:
            resp = await self._client.post(
                f"/services/{domain}/{service}", json=payload
            )
            resp.raise_for_status()
            log.info(
                "Called %s.%s on %s with %s", domain, service, entity_id, extra or {}
            )
            return True
        except httpx.HTTPError as exc:
            log.error(
                "Failed to call %s.%s on %s: %s", domain, service, entity_id, exc
            )
            return False

    async def call_raw_service(self, domain: str, service: str, payload: dict) -> bool:
        """Like call_service but without forcing an entity_id - needed for
        services like notify.* that take message/title/target instead."""
        try:
            resp = await self._client.post(f"/services/{domain}/{service}", json=payload)
            resp.raise_for_status()
            log.info("Called %s.%s with %s", domain, service, payload)
            return True
        except httpx.HTTPError as exc:
            log.error("Failed to call %s.%s: %s", domain, service, exc)
            return False

    async def select_option(self, entity_id: str, option: str) -> bool:
        return await self.call_service(
            "select", "select_option", entity_id, {"option": option}
        )

    async def set_number(self, entity_id: str, value: float) -> bool:
        return await self.call_service(
            "number", "set_value", entity_id, {"value": value}
        )

    async def turn_on(self, entity_id: str) -> bool:
        return await self.call_service("switch", "turn_on", entity_id)

    async def turn_off(self, entity_id: str) -> bool:
        return await self.call_service("switch", "turn_off", entity_id)
