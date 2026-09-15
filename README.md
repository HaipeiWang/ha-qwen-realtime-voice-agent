> **0.11.0-beta.2 hotfix:** repairs alias-related control rejection and makes startup enrichment resilient. See [hotfix notes](openai_realtime_voice_agent/HOTFIX_20260916.md) for validation and remaining issues.

<p align="center">
  <img src="openai_realtime_voice_agent/icon.png" alt="Natural Home Assistant Realtime Harness" width="160"/>
</p>

<p align="center">
  <strong>English</strong> · <a href="README.zh-CN.md">简体中文</a>
</p>

# Natural Home Assistant Realtime Harness

This release turns the former Qwen-specific application into a provider-neutral
realtime harness. **Qwen is the provider shipped and tested in this preview.**
The shared runtime no longer consumes Qwen protocol messages or session state;
additional providers, including GLM, can implement the same adapter contract in a
later release without duplicating Home Assistant control, turn, or audio logic.

## What it can do

- Turn a Home Assistant Voice PE into a native **Qwen Realtime** voice
  assistant without a separate Whisper or Piper pipeline.
- Understand natural spoken requests, stream spoken answers, show conversation
  state through the Voice PE LEDs, and support center-button interruption and
  follow-up conversation.
- Discover Home Assistant MCP tools automatically and control only entities
  exposed to Assist, including lights, switches, covers, climate devices and
  other supported domains.
- Generate extra tools from real entity capabilities for climate mode, fan
  speed, swing mode, fan presets and select options.
- Route common device commands deterministically and confirm actions only after
  Home Assistant returns the execution result.
- Prefer an exact, unique entity name when an inherited or incorrect HA area
  conflicts with that name—for example, a living-room light attached through a
  Bluetooth proxy assigned to another room.
- Pace and buffer streamed audio, recover provider connections, and optionally
  record diagnostic audio for troubleshooting.

## What's new in 0.11.0-beta.1

- Introduces a provider-neutral Core with canonical realtime events, audio chunks,
  tool definitions, results, turn identity, lifecycle and cancellation boundaries.
- Moves Qwen WebSocket events, session configuration and model/voice rules into a
  dedicated Qwen adapter. A fake provider validates the shared contract without a
  cloud connection. GLM is planned but is not included in this release.
- Centralizes Home Assistant tool registration, execution records, parameter
  arbitration, exact-name routing and result-based spoken confirmations.
- Adds actual-time and Assist-exposed weather fact tools, bounded tool execution,
  clarification carry-over, and rejection of unsupported future-action promises.
- Adapts 24 kHz streamed reply audio to Voice PE with monotonic packet deadlines.
  Normal processing overhead no longer slows every packet, while delayed writes
  rebase the clock instead of producing a catch-up burst.

## Provider boundary

```text
Voice PE 16 kHz PCM
        │
        ▼
shared realtime Core ── Home Assistant tools / router / confirmations
        │
        ├── Qwen adapter (available and tested)
        └── GLM adapter  (interface reserved; implementation pending)
        │
        ▼
paced 24 kHz PCM → Voice PE playback buffer
```

The Add-on configuration remains Qwen-native in this preview because Qwen is the
only selectable provider. Provider credentials and protocol objects are confined
to the adapter and composition layer; the shared Core and Home Assistant tool layer
do not import Qwen protocol state.

- Repairs allow-lists saved with spaces, commas or newlines and maps legacy tool
  names to the namespaced names returned by current Home Assistant Core.
- Uses one safe tool policy for discovery, deterministic routing and Qwen
  instructions, with startup validation for every configured tool.
- Restores power control (including Apple TV), climate target temperature, fan
  speed and stop-moving commands without device-specific tool patches.
- Verifies asynchronous device state before speaking a result and protects
  paced reply audio from late VAD events until playback has fully drained.

## How it works

The Add-on connects the companion Voice PE firmware directly to Alibaba Cloud
Model Studio Qwen Realtime. Microphone audio goes to the native Qwen Realtime
WebSocket, speech streams back to the Voice PE, and device actions use MCP plus
capability tools built from entities exposed to Assist.

```text
Voice PE custom firmware               Home Assistant OS Add-on
┌────────────────────────┐  PCM / WS  ┌─────────────────────────────┐
│ wake word + microphone │ ─────────▶ │ provider-neutral Core       │
│ speaker + LED state    │ ◀───────── │ Qwen adapter + paced audio  │
└────────────────────────┘            └──────────────┬──────────────┘
                                                    │ MCP + generated tools
                                                    ▼
                                           Home Assistant entities
```

## Requirements

- Home Assistant OS with access to the Add-on Store.
- A Home Assistant Voice PE running the companion custom firmware.
- An Alibaba Cloud Model Studio workspace with access to a supported Qwen
  Realtime model, plus your own API key and Workspace ID.
- The Home Assistant **Model Context Protocol Server** integration.

No API key, Workspace ID, Home Assistant token, Wi-Fi credential, device
address, recording, or user-specific entity data is included in this repository.

## Realtime model selector

The Add-on Configuration page exposes these native WebSocket models:

| Model | Intended test | Home Assistant tools |
| --- | --- | --- |
| `qwen-audio-3.0-realtime-flash` | Low-cost speech assistant (default) | Yes |
| `qwen-audio-3.0-realtime-plus` | Higher-quality speech assistant | Yes |
| `qwen3.5-omni-flash-realtime` | Fast multimodal voice assistant | Yes |
| `qwen3.5-omni-plus-realtime` | Highest-quality multimodal assistant | Yes |

Voice choices are model-family safe: Qwen-Audio uses its dedicated `longan*`
selector (or a Qwen-Audio cloned voice ID), while Qwen3.5 Omni uses a separate
selector containing only Omni voices. The backend validates legacy/YAML edits
and falls back with an explicit error if a voice does not match the model.

## Install the Add-on

1. In Home Assistant open **Settings → Add-ons → Add-on Store → ⋮ →
   Repositories**.
2. Add:

   ```text
   https://github.com/HaipeiWang/ha-qwen-realtime-voice-agent
   ```

3. Install **Natural Realtime Harness (Qwen Preview)**. The repository intentionally has
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
   - reads the compatible `GetLiveContext` tool to determine the Assist-exposed
     entity boundary;
   - reads those entities' Home Assistant capabilities;
   - builds and registers the extra function tools supported by those devices.

Start the Add-on. A healthy log includes messages similar to:

```text
Home Assistant MCP Client initialized
Entity catalog built through ...GetLiveContext: ... exposed entities
Auto-generated ... capability tools
Qwen Realtime Service created
Starting WebSocket server and pipeline
```

## Connect the Voice PE

The device half lives in
[HaipeiWang/home-assistant-voice-pe-qwen](https://github.com/HaipeiWang/home-assistant-voice-pe-qwen).
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
state. Before using the wake word, open **Settings → Devices & services** and
complete configuration of the discovered ESPHome device with the firmware's API
encryption key. The companion firmware does not start wake-word detection until
that authenticated Home Assistant API connection exists. Then say the configured
wake word, speak a request, and verify the Add-on log shows transcription, a Qwen
response, and any tool execution.

See [DOCS.md](openai_realtime_voice_agent/DOCS.md) for the complete setup,
validation, tuning, and troubleshooting guide.

## Project lineage

- Voice PE protocol and firmware integration:
  [HaipeiWang/home-assistant-voice-pe-qwen](https://github.com/HaipeiWang/home-assistant-voice-pe-qwen),
  forked from [xandervanerven/home-assistant-voice-pe](https://github.com/xandervanerven/home-assistant-voice-pe)
- Backend Add-on foundation:
  [fjfricke/ha-openai-realtime](https://github.com/fjfricke/ha-openai-realtime)
- Runtime framework: [Pipecat](https://github.com/pipecat-ai/pipecat)

The firmware and backend repositories have different roles, so this repository
records both sources rather than claiming a GitHub fork relationship that would
hide one side of the implementation.

## License

MIT — see [LICENSE](LICENSE).
