"""Read-only compatibility probe for a real Home Assistant MCP endpoint.

Run inside an Add-on container so ``SUPERVISOR_TOKEN`` is scoped to that
container. The probe lists schemas and calls only GetLiveContext; it never
dispatches a control tool or changes Home Assistant state.
"""

import asyncio
import os
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_PROJECT_ROOT))

from app.control_intent_router import build_catalog_from_ha
from app.mcp_service import HomeAssistantMCPService
from app.tool_names import canonical_tool_name, tool_allowed


MCP_URL = "http://supervisor/core/api/mcp"
LEGACY_ALLOWLIST = {
    "GetLiveContext",
    "HassTurnOn",
    "HassTurnOff",
    "HassLightSet",
    "HassClimateSetTemperature",
}


async def main() -> None:
    token = os.environ["SUPERVISOR_TOKEN"]
    service = HomeAssistantMCPService(MCP_URL, token)
    client = await service.initialize()
    schema = await client.get_tools_schema()
    wire_names = [tool.name for tool in schema.standard_tools]
    exposed = [
        name for name in wire_names if tool_allowed(name, LEGACY_ALLOWLIST)
    ]
    canonical_exposed = {canonical_tool_name(name) for name in exposed}
    missing = LEGACY_ALLOWLIST - canonical_exposed
    assert not missing, f"legacy allow-list failed to expose: {sorted(missing)}"

    catalog = await asyncio.to_thread(
        build_catalog_from_ha,
        MCP_URL,
        token,
        retries=1,
    )
    assert catalog.entities, "GetLiveContext returned an empty entity catalog"

    target = catalog.by_name("客厅吊灯")
    if target is not None and target.area != "客厅":
        args, details = catalog.normalize_control_arguments(
            "intent__HassTurnOn",
            {"name": "客厅吊灯", "area": "客厅", "domain": ["light"]},
        )
        assert "area" not in args
        assert details and "dropped_conflicting_area" in details["changes"]
        area_check = "passed"
    else:
        area_check = "not-applicable"

    print(
        "HAOS MCP offline probe passed: "
        f"listed={len(wire_names)} exposed={len(exposed)} "
        f"entities={len(catalog.entities)} area_conflict={area_check}"
    )


if __name__ == "__main__":
    asyncio.run(main())
