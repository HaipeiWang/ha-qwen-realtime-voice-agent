# Natural Home Assistant Realtime Harness

Provider-neutral realtime Core and Home Assistant tool harness. The current
preview ships the Qwen adapter; GLM is not implemented yet.

## What this Add-on does

- Connects the companion Voice PE firmware directly to Qwen Realtime for native
  speech input and streamed speech output—without separate Whisper or Piper.
- Automatically discovers Home Assistant MCP tools and builds additional tools
  from the capabilities of entities exposed to Assist.
- Controls lights, switches, climate devices and other supported entities, then
  confirms the action from the real Home Assistant execution result.
- Keeps common device routing deterministic and prefers an exact, unique entity
  name if its inherited Home Assistant area is incorrect.
- Manages wake/follow-up sessions, paced playback, interruption, LEDs and
  connection recovery together with the companion firmware.

The companion firmware is published at
[HaipeiWang/home-assistant-voice-pe-qwen](https://github.com/HaipeiWang/home-assistant-voice-pe-qwen).

## New in 0.11.0-beta.1

- Runs provider-independent turn, tool, confirmation and audio logic in the shared
  Core; Qwen protocol details now live in one adapter.
- Provides canonical provider events and a small adapter contract. Qwen is included
  and tested; GLM remains planned.
- Uses monotonic 24 kHz output pacing so processing overhead does not accumulate on
  every audio packet, with safe clock rebasing after delayed sends.

## Before starting

Configure your own Qwen API Key, Workspace ID, matching region, and an enabled
Realtime model/voice on the **Configuration** page. All are required for the
native regional Qwen WebSocket; the repository contains no provider credentials
or Home Assistant credentials.

Add Home Assistant's **Model Context Protocol Server** integration and expose
only the entities this assistant may control. In the normal HAOS installation,
leave the MCP URL and token fields empty so the Add-on uses its Supervisor
permission.

With **Auto tool generation** enabled, every Add-on start:

1. loads the official Home Assistant MCP tools;
2. obtains the entity boundary from the compatible MCP `GetLiveContext` tool;
3. discovers supported capabilities for those exposed entities;
4. registers Qwen tools for climate modes, fan and swing modes, fan presets,
   select options, and the core MCP operations.

## Voice PE connection

The Add-on listens on WebSocket port `8080` by default. The companion firmware
uses `ws://homeassistant.local:8080/` unless its `va_url` substitution is
overridden. Both ports must match. Install the device firmware through ESPHome
Device Builder; the first stock-firmware replacement normally requires USB.
After flashing, complete the discovered ESPHome integration with the same API
encryption key used by the firmware. Wake-word detection intentionally starts only
after that authenticated Home Assistant API connection is established.

For complete installation, configuration, log checks, and troubleshooting, see
[DOCS.md](DOCS.md).

The historical slug `openai_realtime_voice_agent` is retained only to preserve
upgrade compatibility. The provider connection and visible configuration are
Qwen-native.
