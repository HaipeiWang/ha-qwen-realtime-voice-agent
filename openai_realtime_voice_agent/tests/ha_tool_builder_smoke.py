"""Smoke checks for capability generation and the Assist exposure boundary."""

from unittest.mock import patch

from app.ha_tool_builder import (
    CAPABILITY_TEMPLATES,
    _build_schema,
    _resolve_entity,
    build_generated_tools,
)


STATES = [
    {
        "entity_id": "climate.exposed",
        "attributes": {
            "friendly_name": "Exposed AC",
            "hvac_modes": ["off", "cool", "dry"],
            "fan_modes": ["auto", "high"],
        },
    },
    {
        "entity_id": "climate.private",
        "attributes": {
            "friendly_name": "Private AC",
            "hvac_modes": ["off", "heat"],
            "fan_modes": ["silent"],
        },
    },
    {
        "entity_id": "select.ac_temperature_display",
        "attributes": {
            "friendly_name": "空调 温度显示",
            "options": ["室内温度", "目标温度"],
        },
    },
]


def main() -> None:
    with patch("app.ha_tool_builder._fetch_states", return_value=STATES):
        tools = build_generated_tools(
            "http://home-assistant",
            "unused",
            allowed_names={"Exposed AC", "空调 温度显示"},
        )

    schemas = {tool.name: tool.schema for tool in tools}
    hvac_values = schemas["HassClimateSetHvacMode"]["parameters"]["properties"][
        "hvac_mode"
    ]["enum"]
    fan_values = schemas["HassClimateSetFanMode"]["parameters"]["properties"][
        "fan_mode"
    ]["enum"]
    assert hvac_values == ["cool", "dry", "off"]
    assert "heat" not in hvac_values
    assert fan_values == ["auto", "high"]
    assert "silent" not in fan_values

    select_schema = schemas["HassSelectOption"]["parameters"]["properties"]
    assert select_schema["name"]["enum"] == ["空调 温度显示"]
    assert "enum" not in select_schema["option"]

    select_cap = next(
        cap
        for cap in CAPABILITY_TEMPLATES["select"]
        if cap["tool"] == "HassSelectOption"
    )
    standalone_schema = _build_schema(
        select_cap,
        ["室内温度", "目标温度"],
        selectable_names=["空调 温度显示"],
    )
    assert standalone_schema["parameters"]["properties"]["name"]["enum"] == [
        "空调 温度显示"
    ]

    selectable_entities = [
        {"name": "空调 温度显示"},
        {"name": "空调 功能模式"},
    ]
    assert _resolve_entity(
        selectable_entities, "空调", allow_partial=False
    ) is None
    assert _resolve_entity(
        selectable_entities, "空调 温度显示", allow_partial=False
    ) == selectable_entities[0]

    with patch("app.ha_tool_builder._fetch_states", return_value=STATES):
        assert build_generated_tools(
            "http://home-assistant",
            "unused",
            allowed_names=set(),
        ) == []

    print("ha_tool_builder smoke checks passed")


if __name__ == "__main__":
    main()
