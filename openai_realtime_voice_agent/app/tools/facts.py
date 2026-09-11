"""Time and HA weather tools, independent of LLM provider and scheduling."""

from datetime import datetime, timezone
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.tools.registry import ToolOutcome, ToolRegistry
from app.tools.schema import CanonicalTool, CanonicalToolRequest, CanonicalToolResult


class FactsBackend(Protocol):
    async def home_timezone(self) -> str: ...
    async def exposed_weather(self) -> dict[str, str]: ...
    async def weather_state(self, entity_id: str) -> dict: ...
    async def weather_forecast(self, entity_id: str, kind: str) -> list[dict]: ...


TIME_TOOL = CanonicalTool(
    "GetCurrentTime", "获取当前日期和时间。默认使用 HA 配置的时区；其他地区请传 IANA 时区（如 America/New_York）。地点不明确先询问，不猜测时区。",
    {"type": "object", "properties": {"timezone": {"type": "string", "minLength": 1}}, "additionalProperties": False},
    read_only=True,
)
WEATHER_TOOL = CanonicalTool(
    "GetWeather", "读取 HA 已暴露的天气实体。未指定实体时，多个地点必须先澄清。current 为当前天气；daily/hourly/twice_daily 为预报。只报告返回的数据与单位，不编造缺失预报。",
    {"type": "object", "properties": {
        "entity_id": {"type": "string", "pattern": "^weather\\.[a-z0-9_]+$"},
        "type": {"type": "string", "enum": ["current", "daily", "hourly", "twice_daily"]},
    }, "additionalProperties": False}, read_only=True,
)


class FactTools:
    def __init__(self, backend: FactsBackend, clock=None):
        self.backend = backend
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def register(self, registry: ToolRegistry) -> None:
        registry.register(TIME_TOOL, self.current_time)
        registry.register(WEATHER_TOOL, self.weather)

    async def current_time(self, request: CanonicalToolRequest, execution_id: str) -> ToolOutcome:
        zone = request.arguments.get("timezone")
        source = "requested_iana_timezone" if zone else "ha_config.time_zone"
        if not zone:
            zone = await self.backend.home_timezone()
        try:
            tz = ZoneInfo(zone)
        except (ZoneInfoNotFoundError, ValueError, TypeError):
            return ToolOutcome(CanonicalToolResult(execution_id, "failed", detail="invalid_or_missing_timezone"))
        instant = self.clock()
        if instant.tzinfo is None:
            return ToolOutcome(CanonicalToolResult(execution_id, "unknown", detail="clock_timezone_missing"))
        return ToolOutcome(CanonicalToolResult(execution_id, "completed", verified=True), {
            "datetime": instant.astimezone(tz).isoformat(), "timezone": zone,
            "timezone_source": source, "clock_source": "host_system_clock",
            "utc": instant.astimezone(timezone.utc).isoformat(),
        })

    async def weather(self, request: CanonicalToolRequest, execution_id: str) -> ToolOutcome:
        allowed = await self.backend.exposed_weather()
        entity_id = request.arguments.get("entity_id")
        if not entity_id:
            if len(allowed) != 1:
                return ToolOutcome(CanonicalToolResult(execution_id, "failed", detail="weather_target_required"), {"choices": allowed})
            entity_id = next(iter(allowed))
        if entity_id not in allowed:
            return ToolOutcome(CanonicalToolResult(execution_id, "failed", detail="weather_not_exposed"))
        state = await self.backend.weather_state(entity_id)
        if state.get("entity_id") != entity_id or state.get("state") in (None, "unknown", "unavailable"):
            return ToolOutcome(CanonicalToolResult(execution_id, "unknown", detail="weather_unavailable"))
        kind = request.arguments.get("type", "current")
        attrs = state.get("attributes") or {}
        fields = ("temperature", "temperature_unit", "humidity", "pressure", "pressure_unit", "wind_speed", "wind_speed_unit", "precipitation_unit")
        data = {"entity_id": entity_id, "name": allowed[entity_id], "type": kind,
                "last_updated": state.get("last_updated"),
                "measurements": {k: attrs[k] for k in fields if k in attrs}}
        if kind == "current":
            data["condition"] = state["state"]
        else:
            data["units"] = {k: v for k, v in data.pop("measurements").items() if k.endswith("_unit")}
            forecast = await self.backend.weather_forecast(entity_id, kind)
            if not forecast or not all(isinstance(row, dict) and row.get("datetime") for row in forecast):
                return ToolOutcome(CanonicalToolResult(execution_id, "unknown", detail="forecast_missing"))
            data["forecast"] = forecast
        return ToolOutcome(CanonicalToolResult(execution_id, "completed", verified=True), data)
