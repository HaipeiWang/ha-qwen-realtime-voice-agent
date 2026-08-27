"""Smoke checks for capability generation and the Assist exposure boundary."""

from unittest.mock import patch

from app.ha_tool_builder import build_generated_tools


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
]


def main() -> None:
    with patch("app.ha_tool_builder._fetch_states", return_value=STATES):
        tools = build_generated_tools(
            "http://home-assistant",
            "unused",
            allowed_names={"Exposed AC"},
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

    with patch("app.ha_tool_builder._fetch_states", return_value=STATES):
        assert build_generated_tools(
            "http://home-assistant",
            "unused",
            allowed_names=set(),
        ) == []

    print("ha_tool_builder smoke checks passed")


if __name__ == "__main__":
    main()
