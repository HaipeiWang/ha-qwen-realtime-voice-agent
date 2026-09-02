# Qwen Realtime Voice Agent

This Home Assistant OS Add-on is the backend for the custom Voice PE firmware
from [HaipeiWang/home-assistant-voice-pe-qwen](https://github.com/HaipeiWang/home-assistant-voice-pe-qwen).
It streams Voice PE audio to Qwen Omni Realtime, streams Qwen audio back to the
speaker, and controls Home Assistant through MCP and automatically generated
capability tools.

## Before starting

Configure your own Qwen API Key and Workspace ID on the **Configuration** page.
The repository contains no provider credentials or Home Assistant credentials.

Add Home Assistant's **Model Context Protocol Server** integration and expose
only the entities this assistant may control. In the normal HAOS installation,
leave the MCP URL and token fields empty so the Add-on uses its Supervisor
permission.

With **Auto tool generation** enabled, every Add-on start:

1. loads the official Home Assistant MCP tools;
2. obtains the entity boundary from MCP `GetLiveContext`;
3. discovers supported capabilities for those exposed entities;
4. registers Qwen tools for climate modes, fan and swing modes, fan presets,
   select options, and the core MCP operations.

## Voice PE connection

The Add-on listens on WebSocket port `8080` by default. The companion firmware
uses `ws://homeassistant.local:8080/` unless its `va_url` substitution is
overridden. Both ports must match. Install the device firmware through ESPHome
Device Builder; the first stock-firmware replacement normally requires USB.

For complete installation, configuration, log checks, and troubleshooting, see
[DOCS.md](DOCS.md).

The historical slug `openai_realtime_voice_agent` is retained only to preserve
upgrade compatibility. The provider connection and visible configuration are
Qwen-native.
