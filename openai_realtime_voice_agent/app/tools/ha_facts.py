"""HA read-only facts transport with an injected authoritative exposure source."""

import asyncio
import json
from collections.abc import Awaitable, Callable
from urllib.parse import quote

import httpx
from websockets.asyncio.client import connect


async def exposed_entity_ids(base_url: str, token: str) -> set[str]:
    """Read explicit Assist exposure; authorization/protocol failures fail closed."""
    url = base_url.rstrip("/") + "/api/websocket"
    url = url.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
    async with asyncio.timeout(10):
        async with connect(url, open_timeout=5, close_timeout=1) as ws:
            if json.loads(await ws.recv()).get("type") != "auth_required":
                raise PermissionError("HA authentication protocol unavailable")
            await ws.send(json.dumps({"type": "auth", "access_token": token}))
            if json.loads(await ws.recv()).get("type") != "auth_ok":
                raise PermissionError("HA authentication failed")
            await ws.send(json.dumps({"id": 1, "type": "homeassistant/expose_entity/list"}))
            response = json.loads(await ws.recv())
            if response.get("id") != 1 or response.get("success") is not True:
                raise PermissionError("HA exposure list unavailable")
            entries = response["result"]["exposed_entities"]
            return {entity_id for entity_id, assistants in entries.items()
                    if isinstance(assistants, dict) and assistants.get("conversation") is True}


class HAFactsBackend:
    def __init__(self, client: httpx.AsyncClient,
                 exposed_entities: Callable[[], Awaitable[set[str]]]):
        # The application owns authentication/base_url and client lifetime.
        self.client = client
        self.exposed_entities = exposed_entities

    async def home_timezone(self) -> str:
        response = await self.client.get("api/config")
        response.raise_for_status()
        return response.json().get("time_zone", "")

    async def exposed_weather(self) -> dict[str, str]:
        ids = await self.exposed_entities()
        # Fetch only explicitly exposed weather states, never an all-state fallback.
        candidates = sorted(i for i in ids if i.startswith("weather."))
        states = await asyncio.gather(*(self.weather_state(i) for i in candidates))
        return {i: (s.get("attributes") or {}).get("friendly_name", i)
                for i, s in zip(candidates, states) if s.get("entity_id") == i}

    async def weather_state(self, entity_id: str) -> dict:
        if not entity_id.startswith("weather.") or entity_id not in await self.exposed_entities():
            raise PermissionError("Weather entity not exposed")
        response = await self.client.get("api/states/" + quote(entity_id, safe=""))
        response.raise_for_status()
        return response.json()

    async def weather_forecast(self, entity_id: str, kind: str) -> list[dict]:
        if not entity_id.startswith("weather.") or entity_id not in await self.exposed_entities():
            raise PermissionError("Weather entity not exposed")
        if kind not in {"daily", "hourly", "twice_daily"}:
            raise ValueError("Unsupported forecast type")
        response = await self.client.post(
            "api/services/weather/get_forecasts", params={"return_response": ""},
            json={"entity_id": entity_id, "type": kind},
        )
        response.raise_for_status()
        return (response.json().get("service_response", {}).get(entity_id) or {}).get("forecast", [])
