"""Capability-driven Home Assistant tool builder for the Qwen Voice PE bridge.

WHY THIS EXISTS
---------------
Home Assistant's MCP server only exposes the fixed set of Assist built-in
intents (24 tools).  Capabilities such as ``climate.set_hvac_mode`` (制冷/制热/
除湿), ``climate.set_fan_mode``, ``climate.set_swing_mode``,
``fan.set_preset_mode`` and ``select.select_option`` are NOT among them, so the
model can neither switch the AC mode nor set a fan preset.  This module
discovers what the *exposed* entities actually support (from ``/api/states``
attributes) and generates matching function tools, executed by calling the HA
service over REST directly.

The discovery runs on every add-on start (self-healing: newly added devices get
their tools on the next restart).  Only safe, controllable domains are
generated; dangerous services (system/script/homeassistant/...) are excluded.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Callable, Optional
from urllib import request

from app.control_intent_router import RouterRule

logger = logging.getLogger(__name__)


# Capability templates: domain -> list of {tool, service, param, attr, desc}.
# ``attr`` is the /api/states attribute that lists the supported values; those
# values become the tool's enum so the model can only pick legal ones.
CAPABILITY_TEMPLATES = {
    "climate": [
        {
            "id": "climate_mode",
            "tool": "HassClimateSetHvacMode",
            "service": "climate/set_hvac_mode",
            "param": "hvac_mode",
            "attr": "hvac_modes",
            "core": True,
            "description": "切换空调运行模式（制冷/制热/除湿/送风/自动/关）。",
            "param_desc": "目标运行模式，例如 dry（除湿）、cool（制冷）、heat（制热）、fan_only（送风）、auto（自动）。",
            "router": {
                "domain": "climate",
                "keywords": ("除湿", "制冷", "制热", "送风", "通风", "自动"),
                "value_map": {
                    "除湿": "dry",
                    "制冷": "cool",
                    "制热": "heat",
                    "送风": "fan_only",
                    "通风": "fan_only",
                    "自动": "auto",
                },
            },
        },
        {
            "id": "climate_fan",
            "tool": "HassClimateSetFanMode",
            "service": "climate/set_fan_mode",
            "param": "fan_mode",
            "attr": "fan_modes",
            "core": True,
            "description": "设置空调风速。",
            "param_desc": "目标风速，例如 auto（自动）、low（低）、medium（中）、high（高）。",
            "router": {
                "domain": "climate",
                "keywords": ("风速", "风量"),
                "value_map": {"自动": "auto", "低": "low", "中": "medium", "高": "high"},
            },
        },
        {
            "id": "climate_swing",
            "tool": "HassClimateSetSwingMode",
            "service": "climate/set_swing_mode",
            "param": "swing_mode",
            "attr": "swing_modes",
            "core": True,
            "description": "设置空调摆风方向。",
            "param_desc": "目标摆风方向，例如 off（关）、vertical（上下）、horizontal（左右）、both（双向）。",
            "router": {
                "domain": "climate",
                "keywords": ("摆风", "扫风"),
                "value_map": {"上下": "vertical", "左右": "horizontal", "双向": "both", "关闭": "off"},
            },
        },
    ],
    "fan": [
        {
            "id": "fan_preset",
            "tool": "HassFanSetPreset",
            "service": "fan/set_preset_mode",
            "param": "preset_mode",
            "attr": "preset_modes",
            "core": True,
            "description": "设置风扇/空气净化器的预设模式。",
            "param_desc": "目标预设模式，例如 auto（自动）、manual（手动）、sleep（睡眠）、comfortable（舒适）。",
            "router": {
                "domain": "fan",
                "keywords": ("睡眠模式", "睡眠", "舒适", "手动", "自动"),
                "value_map": {
                    "睡眠模式": "sleep",
                    "睡眠": "sleep",
                    "舒适": "comfortable",
                    "手动": "manual",
                    "自动": "auto",
                },
            },
        },
    ],
    "select": [
        {
            "id": "select_option",
            "tool": "HassSelectOption",
            "service": "select/select_option",
            "param": "option",
            "attr": "options",
            "core": False,
            "description": (
                "设置选择型实体的离散选项（如空调功能模式、风速、摆风或温度显示单位）。"
                "此工具不能设置空调目标温度；数值温度必须使用 HassClimateSetTemperature。"
            ),
            "param_desc": "目标离散选项，必须是该实体支持的选项之一，不能填写数值温度。",
            "router": None,
        },
    ],
}

# Priority order used for truncation when the generated tool count is capped:
# common/forced-routing capabilities first, long-tail ones last.
CAPABILITY_PRIORITY = [
    "climate_mode",
    "climate_fan",
    "climate_swing",
    "fan_preset",
    "select_option",
]

# Domains we never generate tools for.
UNSAFE_DOMAINS = {
    "system",
    "script",
    "automation",
    "homeassistant",
    "input_boolean",
    "scene",
    "group",
    "zone",
    "person",
    "update",
    "camera",
}

MAX_GENERATED_TOOLS = 30


@dataclass
class GeneratedTool:
    id: str
    name: str
    core: bool
    schema: dict
    handler: Callable  # async (params) -> None; must call params.result_callback(str)
    router_rule: Optional[RouterRule] = None


def _fetch_states(base_url: str, token: str, timeout: float = 20.0) -> list:
    req = request.Request(
        f"{base_url}/api/states",
        headers={"Authorization": f"Bearer {token}"},
    )
    with request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _call_ha_service(
    base_url: str,
    token: str,
    service: str,
    entity_id: str,
    payload: dict,
    timeout: float = 15.0,
) -> list:
    body = json.dumps({"entity_id": entity_id, **payload}).encode("utf-8")
    req = request.Request(
        f"{base_url}/api/services/{service}",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    with request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8") or "[]"
        return json.loads(raw)


def build_generated_tools(
    base_url: str,
    token: str,
    *,
    timeout: float = 20.0,
    enabled_ids: Optional[list[str]] = None,
    allowed_names: Optional[set[str]] = None,
) -> list[GeneratedTool]:
    """Discover exposed entity capabilities and build tools for them.

    ``allowed_names`` is the safety boundary derived from MCP GetLiveContext.
    Home Assistant's REST state endpoint contains every entity, including ones
    the user did not expose to Assist, so generated tools must never be built
    from that endpoint without intersecting it with the MCP-visible names.
    """
    if allowed_names is not None and not allowed_names:
        logger.warning(
            "Capability discovery: MCP exposed-entity catalog is empty; "
            "no REST capability tools will be generated"
        )
        return []
    try:
        states = _fetch_states(base_url, token, timeout)
    except Exception:
        logger.exception("Capability discovery: failed to fetch /api/states")
        return []

    entities = []
    for st in states:
        entity_id = st.get("entity_id", "")
        domain = entity_id.split(".", 1)[0] if "." in entity_id else ""
        if domain not in CAPABILITY_TEMPLATES:
            continue
        attrs = st.get("attributes", {}) or {}
        friendly_name = str(attrs.get("friendly_name") or entity_id)
        if allowed_names is not None and friendly_name not in allowed_names:
            continue
        entities.append(
            {
                "entity_id": entity_id,
                "name": friendly_name,
                "domain": domain,
                "attrs": attrs,
            }
        )
    if not entities:
        logger.warning("Capability discovery: no controllable entities found")
        return []

    # Aggregate supported values per domain+tool (deduped).
    domain_tool_values: dict = {}
    per_cap_counts: dict = {}
    for entity in entities:
        for cap in CAPABILITY_TEMPLATES[entity["domain"]]:
            values = entity["attrs"].get(cap["attr"])
            if values:
                domain_tool_values.setdefault(cap["tool"], set()).update(values)
                per_cap_counts[cap["id"]] = per_cap_counts.get(cap["id"], 0) + 1

    enabled = set(enabled_ids) if enabled_ids is not None else None
    logger.info(
        "Capability discovery: %s",
        ", ".join(f"{cid}x{n}" for cid, n in sorted(per_cap_counts.items())) or "none",
    )

    tools: list[GeneratedTool] = []
    for cap in _all_capabilities(enabled):
        tool_name = cap["tool"]
        if tool_name not in domain_tool_values:
            continue
        selectable_names = sorted({
            entity["name"]
            for entity in entities
            if entity["domain"] in CAPABILITY_TEMPLATES
            and cap in CAPABILITY_TEMPLATES[entity["domain"]]
            and entity["attrs"].get(cap["attr"])
        })
        schema = _build_schema(
            cap,
            sorted(domain_tool_values[tool_name]),
            selectable_names=selectable_names,
        )
        handler = _make_handler(base_url, token, cap, entities)
        router_rule = None
        router = cap.get("router")
        if router and cap.get("core"):
            router_rule = RouterRule(
                tool=tool_name,
                param=cap["param"],
                domain=router["domain"],
                keywords=tuple(router["keywords"]),
                value_map=dict(router["value_map"]),
            )
        tools.append(
            GeneratedTool(
                id=cap["id"],
                name=tool_name,
                core=bool(cap.get("core")),
                schema=schema,
                handler=handler,
                router_rule=router_rule,
            )
        )
        if len(tools) >= MAX_GENERATED_TOOLS:
            break

    logger.info(
        "Auto-generated %d capability tools (forced-routing %s): %s",
        len(tools),
        [t.name for t in tools if t.router_rule],
        [t.name for t in tools],
    )
    return tools


def _all_capabilities(enabled_ids: Optional[set] = None):
    """Yield capability templates in priority order, filtered by enabled ids."""
    by_id = {cap["id"]: cap for caps in CAPABILITY_TEMPLATES.values() for cap in caps}
    for cap_id in CAPABILITY_PRIORITY:
        cap = by_id.get(cap_id)
        if cap is None:
            continue
        if enabled_ids is not None and cap_id not in enabled_ids:
            continue
        yield cap


def _build_schema(
    cap: dict, values: list, *, selectable_names: Optional[list[str]] = None
) -> dict:
    properties = {
        "name": {"type": "string", "description": "设备或实体名称，例如 空调。"},
    }
    param_schema: dict = {"type": "string", "description": cap["param_desc"]}
    # Select options are entity-specific: aggregating them across every select
    # entity produces a meaningless grab-bag (wake words + AC modes + drying
    # rack positions ...). Keep select free-form and validate in the handler.
    if values and cap.get("tool") != "HassSelectOption":
        param_schema["enum"] = values
    if cap.get("tool") == "HassSelectOption" and selectable_names:
        properties["name"]["enum"] = selectable_names
        properties["name"]["description"] = (
            "选择型实体的完整名称；必须从枚举中选择，不能使用父设备简称。"
        )
    properties[cap["param"]] = param_schema
    return {
        "type": "function",
        "name": cap["tool"],
        "description": cap["description"],
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": ["name", cap["param"]],
        },
    }


def _resolve_entity(
    entities: list[dict], name: str, *, allow_partial: bool = True
) -> Optional[dict]:
    name = (name or "").strip()
    if not name:
        return None
    for entity in entities:
        if entity["name"] == name:
            return entity
    if allow_partial:
        for entity in entities:
            if name in entity["name"] or entity["name"] in name:
                return entity
    return None


def _make_handler(base_url: str, token: str, cap: dict, entities: list[dict]) -> Callable:
    async def handler(params) -> None:
        arguments = dict(getattr(params, "arguments", {}) or {})
        name = str(arguments.get("name", "")).strip()
        value = arguments.get(cap["param"])
        entity = _resolve_entity(
            entities,
            name,
            allow_partial=cap["tool"] != "HassSelectOption",
        )
        if entity is None:
            await params.result_callback(
                json.dumps(
                    {"success": False, "tool": cap["tool"], "error": f"未找到实体 '{name}'"},
                    ensure_ascii=False,
                )
            )
            return
        supported = entity["attrs"].get(cap["attr"]) or []
        if supported and value not in supported:
            await params.result_callback(
                json.dumps(
                    {
                        "success": False,
                        "tool": cap["tool"],
                        "error": f"实体 {entity['name']} 不支持值 '{value}'，支持：{'、'.join(supported)}",
                    },
                    ensure_ascii=False,
                )
            )
            return
        try:
            changed = _call_ha_service(
                base_url, token, cap["service"], entity["entity_id"], {cap["param"]: value}
            )
            # HA returns [] when the service call did not actually change any
            # entity (e.g. select_option on a fan, or a wrong entity_id).  Treat
            # that as failure so the model cannot report a false success.
            if not changed:
                await params.result_callback(
                    json.dumps(
                        {
                            "success": False,
                            "tool": cap["tool"],
                            "error": (
                                f"HA 服务执行后没有实体状态变化（可能实体名不正确，"
                                f"或 {entity['name']} 不支持此操作）"
                            ),
                        },
                        ensure_ascii=False,
                    )
                )
                return
            await params.result_callback(
                json.dumps(
                    {
                        "success": True,
                        "tool": cap["tool"],
                        "result": f"已将 {entity['name']} 的{cap['param_desc']}设为 {value}",
                    },
                    ensure_ascii=False,
                )
            )
        except Exception as exc:
            await params.result_callback(
                json.dumps(
                    {
                        "success": False,
                        "tool": cap["tool"],
                        "error": f"Home Assistant 服务调用失败：{exc}",
                    },
                    ensure_ascii=False,
                )
            )

    return handler
