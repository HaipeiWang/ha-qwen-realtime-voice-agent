<p align="center">
  <img src="openai_realtime_voice_agent/icon.png" alt="Qwen Omni Realtime Voice Agent for Home Assistant Voice PE" width="160"/>
</p>

<p align="center">
  <strong>English</strong> · <a href="README.zh-CN.md">简体中文</a>
</p>

# Qwen Omni Realtime Voice Agent for Home Assistant Voice PE

A Home Assistant OS Add-on that connects custom Voice PE firmware directly to
Alibaba Cloud Model Studio **Qwen Omni Realtime**. Microphone audio goes to the
native Qwen Realtime WebSocket, Qwen streams speech back to the Voice PE, and
Home Assistant devices are controlled through MCP plus capability tools built
automatically from the entities the user exposed to Assist.

```text
Voice PE custom firmware               Home Assistant OS Add-on
┌────────────────────────┐  PCM / WS  ┌─────────────────────────────┐
│ wake word + microphone │ ─────────▶ │ Qwen Omni Realtime bridge  │
│ speaker + LED state    │ ◀───────── │ paced audio + state machine│
└────────────────────────┘            └──────────────┬──────────────┘
                                                    │ MCP + generated tools
                                                    ▼
                                           Home Assistant entities
```

## Features

- Native speech-to-speech Qwen Realtime connection; no separate Whisper or
  Piper path.
- Voice PE wake word, wake chime, LEDs, center-button interruption, follow-up
  conversation, and streamed speaker audio.
- Home Assistant MCP tools discovered on every Add-on start.
- Additional capability tools generated automatically for supported exposed
  entities: climate mode/fan/swing, fan presets, and select options.
- Deterministic routing for common device commands and result-aware spoken
  confirmation, reducing verbal false successes.
- Paced audio delivery, playback buffering, reconnect handling, and optional
  diagnostic audio recording.
- English and Simplified Chinese Add-on configuration descriptions.

## Requirements

- Home Assistant OS with access to the Add-on Store.
- A Home Assistant Voice PE running the companion custom firmware.
- An Alibaba Cloud Model Studio workspace with access to a supported Qwen Omni
  Realtime model, plus your own API key and Workspace ID.
- The Home Assistant **Model Context Protocol Server** integration.

No API key, Workspace ID, Home Assistant token, Wi-Fi credential, device
address, recording, or user-specific entity data is included in this repository.

## Install the Add-on

1. In Home Assistant open **Settings → Add-ons → Add-on Store → ⋮ →
   Repositories**.
2. Add:

   ```text
   https://github.com/HaipeiWang/ha-qwen-realtime-voice-agent
   ```

3. Install **Qwen Omni Realtime Voice Agent**. The repository intentionally has
   no fixed container image, so HAOS builds the Add-on for its own architecture
   from the included Dockerfile. The first build can take several minutes.
4. Open the Add-on **Configuration** page and enter your own:

   - Qwen API Key
   - Qwen Workspace ID
   - region matching that workspace, normally `cn-beijing`
   - Realtime model and voice available to that workspace

5. Save the configuration. Do not start the Add-on until MCP is enabled as
   described below.

## Enable Home Assistant tools

1. Add **Model Context Protocol Server** from **Settings → Devices & services
   → Add integration**.
2. Under **Settings → Voice assistants → Expose**, expose only the entities the
   Voice PE may control.
3. Keep **MCP server URL** and **Access token** empty in the Add-on for the
   normal HAOS installation. The Add-on receives an internal Supervisor token.
4. Leave **Auto tool generation** enabled. At startup the Add-on:

   - obtains the official MCP tools;
   - reads `GetLiveContext` to determine the Assist-exposed entity boundary;
   - reads those entities' Home Assistant capabilities;
   - builds and registers the extra function tools supported by those devices.

Start the Add-on. A healthy log includes messages similar to:

```text
Home Assistant MCP Client initialized
Entity catalog built: ... exposed entities
Auto-generated ... capability tools
Qwen Realtime Service created
Starting WebSocket server and pipeline
```

## Connect the Voice PE

The device half lives in
[xandervanerven/home-assistant-voice-pe](https://github.com/xandervanerven/home-assistant-voice-pe).
Install its custom firmware through **ESPHome Device Builder** using the DHCP or
static-IP stub supplied by that repository. The first replacement of stock
firmware normally requires USB; later updates can use OTA.

The companion firmware defaults to:

```text
ws://homeassistant.local:8080/
```

If that host name is unavailable on your network, set the firmware
`substitutions.va_url` to the HAOS host name and Add-on WebSocket port. Keep the
Add-on's `websocket_port` and the firmware URL port identical.

After boot, the Voice PE should connect to the Add-on and enter its idle LED
state. Say the configured wake word, speak a request, and verify the Add-on log
shows transcription, a Qwen response, and any tool execution.

See [DOCS.md](openai_realtime_voice_agent/DOCS.md) for the complete setup,
validation, tuning, and troubleshooting guide.

## Project lineage

- Voice PE protocol and firmware integration:
  [xandervanerven/home-assistant-voice-pe](https://github.com/xandervanerven/home-assistant-voice-pe)
- Backend Add-on foundation:
  [fjfricke/ha-openai-realtime](https://github.com/fjfricke/ha-openai-realtime)
- Runtime framework: [Pipecat](https://github.com/pipecat-ai/pipecat)

The firmware and backend repositories have different roles, so this repository
records both sources rather than claiming a GitHub fork relationship that would
hide one side of the implementation.

## License

MIT — see [LICENSE](LICENSE).
