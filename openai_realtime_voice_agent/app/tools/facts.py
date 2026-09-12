"""Time and HA weather tools, independent of LLM provider and scheduling."""

from datetime import datetime, timezone, timedelta
import httpx
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
    "GetWeather", "查询HA实际暴露的天气。无须猜entity_id，唯一实体可省略；type=catalog获取真实ID及能力。用户所说地点原样传location，家里/这里可用；城市或行政区必须验证覆盖，不得省略地点以绕过校验。current为当前；明天用daily和day=tomorrow。每次天气问题均调用工具，只报告返回的数据、日期、单位和覆盖范围；失败不代表未接入数据，不编造缺失预报。",
    {"type": "object", "properties": {
        "entity_id": {"type": "string", "pattern": "^weather\\.[a-z0-9_]+$"},
        "type": {"type": "string", "enum": ["catalog", "current", "daily", "hourly", "twice_daily"]},
        "location": {"type": "string", "minLength": 1, "maxLength": 100},
        "day": {"type": "string", "enum": ["today", "tomorrow"]},
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
        try:
            return await self._weather(request, execution_id)
        except PermissionError:
            return ToolOutcome(CanonicalToolResult(execution_id, "failed", detail="weather_access_denied"),
                               {"message": "天气访问权限不可用，不代表没有接入天气数据。"})
        except (httpx.HTTPError, TimeoutError):
            return ToolOutcome(CanonicalToolResult(execution_id, "unknown", detail="weather_fetch_failed"),
                               {"message": "此次天气读取失败，不能据此判断未接入数据，不使用旧数据冒充本次结果。"})

    async def _weather(self, request: CanonicalToolRequest, execution_id: str) -> ToolOutcome:
        allowed = await self.backend.exposed_weather()
        states = {i: await self.backend.weather_state(i) for i in allowed}
        flags = {"daily": 1, "hourly": 2, "twice_daily": 4}
        choices = [{"entity_id": i, "name": name, "coverage": "HA配置的天气位置；城市/行政区绑定未验证",
                    "types": ["current"] + [k for k, flag in flags.items()
                              if (states[i].get("attributes", {}).get("supported_features", 0) & flag)]}
                   for i, name in allowed.items()]
        def failure(detail, message, status="failed"):
            return ToolOutcome(CanonicalToolResult(execution_id, status, detail=detail),
                               {"choices": choices, "message": message})
        if not allowed:
            return failure("weather_not_exposed", "Assist未暴露可用天气实体；不能据此断言HA没有天气集成。")
        kind = request.arguments.get("type", "current")
        if kind == "catalog":
            return ToolOutcome(CanonicalToolResult(execution_id, "completed", verified=True), {"choices": choices})
        location = request.arguments.get("location", "").strip()
        if location not in {"", "家", "家里", "这里", "我家", "家中", "本地", "home", "here"}:
            return failure("weather_location_unverified", "现有数据源未建立该地点的可验证绑定，暂不能查询该地点；可查询家里的天气。")
        entity_id = request.arguments.get("entity_id")
        if not entity_id:
            if len(allowed) != 1:
                return failure("weather_target_required", "存在多个天气目标，请按可用名称澄清，不自行选择。")
            entity_id = next(iter(allowed))
        if entity_id not in allowed:
            return failure("weather_not_exposed", "请求实体不在Assist暴露清单，请使用choices中的真实ID；不是完全没有天气数据。")
        state = states[entity_id]
        if state.get("entity_id") != entity_id or state.get("state") in (None, "unknown", "unavailable"):
            return failure("weather_unavailable", "该实体目前不可用，不代表未接入天气数据。", "unknown")
        if request.arguments.get("day") and kind == "current":
            return failure("weather_forecast_required", "日期查询必须使用预报类型，不能拿当前天气回答明天天气。")
        attrs = state.get("attributes") or {}
        fields = ("temperature", "temperature_unit", "humidity", "pressure", "pressure_unit", "wind_speed", "wind_speed_unit", "precipitation_unit")
        data = {"entity_id": entity_id, "name": allowed[entity_id], "type": kind,
                "source": "ha_weather_entity", "choices": choices,
                "coverage": "HA配置的天气位置，未验证城市或行政区名称",
                "last_updated": state.get("last_updated"),
                "measurements": {k: attrs[k] for k in fields if k in attrs}}
        if kind == "current":
            data["condition"] = state["state"]
        else:
            if not (attrs.get("supported_features", 0) & flags[kind]):
                return failure("weather_forecast_unsupported", "此数据源不支持请求的预报类型；请参考choices中的能力，不改用当前天气冒充预报。")
            data["units"] = {k: v for k, v in data.pop("measurements").items() if k.endswith("_unit")}
            forecast = await self.backend.weather_forecast(entity_id, kind)
            if not forecast or not all(isinstance(row, dict) and row.get("datetime") for row in forecast):
                return failure("forecast_missing", "此次预报为空或不完整，不能编造，也不能用当前天气替代。", "unknown")
            zone = await self.backend.home_timezone()
            try:
                tz = ZoneInfo(zone)
                now = self.clock().astimezone(tz)
                data.update(timezone=zone, retrieved_at=now.isoformat())
                if request.arguments.get("day"):
                    date = now.date() + timedelta(days=request.arguments["day"] == "tomorrow")
                    data["requested_date"] = date.isoformat()
                    dated = [(row, datetime.fromisoformat(row["datetime"].replace("Z", "+00:00"))) for row in forecast]
                    if any(instant.tzinfo is None for _, instant in dated):
                        return failure("forecast_time_invalid", "预报缺少时区，不能猜测日期。", "unknown")
                    forecast = [row for row, instant in dated if instant.astimezone(tz).date() == date]
                    if not forecast:
                        return failure("forecast_date_missing", "没有所请求日期的预报，不使用其他日期代替。", "unknown")
            except (ValueError, TypeError, ZoneInfoNotFoundError):
                return failure("forecast_time_invalid", "无法确认预报日期或时区，不能猜测日期。", "unknown")
            data["forecast"] = forecast
        return ToolOutcome(CanonicalToolResult(execution_id, "completed", verified=True), data)
