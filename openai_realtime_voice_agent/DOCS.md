# Qwen Omni Realtime Voice Agent — Setup and Validation

This Add-on is one half of the system. The Add-on runs the Qwen session and
Home Assistant tools; the companion Voice PE firmware captures and plays audio.

```text
Voice PE ── 16 kHz PCM / local WebSocket ──▶ HAOS Add-on
Voice PE ◀─ 24 kHz PCM / local WebSocket ─── HAOS Add-on
                                                    │
                                         Qwen native Realtime WebSocket
                                                    │ function calls
                                                    ▼
                                     HA MCP + generated capability tools
```

The historical slug `openai_realtime_voice_agent` remains for update
compatibility. The provider protocol and visible settings are Qwen-native.

## 1. Install from the Add-on repository

In Home Assistant open **Settings → Add-ons → Add-on Store → ⋮ →
Repositories**, add:

```text
https://github.com/HaipeiWang/ha-qwen-realtime-voice-agent
```

Install **Qwen Omni Realtime Voice Agent**. HAOS builds the Add-on locally from
the included Dockerfile for `amd64` or `aarch64`. The first build downloads the
Python dependencies and may take several minutes. Use the installation log to
follow progress.

## 2. Configure your Qwen workspace

Open the Add-on **Configuration** page and set:

| Setting | Required | Meaning |
| --- | --- | --- |
| Qwen API Key | yes | A key belonging to your Model Studio workspace. |
| Qwen Workspace ID | yes | Your own workspace identifier, shown in Model Studio. |
| Qwen region | yes | Region that owns the workspace, normally `cn-beijing`. |
| Realtime model | yes | A Qwen Omni Realtime model enabled for the workspace. |
| Voice | yes | A voice supported by the selected model. |

The Add-on constructs the native endpoint at runtime:

```text
wss://<workspace-id>.<region>.maas.aliyuncs.com/api-ws/v1/realtime?model=<model>
```

It does not use the OpenAI-compatible HTTP endpoint. Save and restart after any
provider setting changes. The public repository contains no API key or
Workspace ID.

## 3. Enable Home Assistant MCP

1. Open **Settings → Devices & services → Add integration**.
2. Add **Model Context Protocol Server**.
3. Open **Settings → Voice assistants → Expose** and expose only entities this
   assistant may read or control.
4. In the Add-on Configuration, keep **MCP server URL** and **Access token**
   empty for a normal HAOS installation. `homeassistant_api: true` gives the
   Add-on an internal Supervisor token and the default endpoint is
   `http://supervisor/core/api/mcp`.

Use an external MCP URL or long-lived token only when the Add-on is deliberately
connecting to a different Home Assistant instance.

## 4. Automatic tool discovery and generation

Leave **Auto tool generation** enabled unless diagnosing a device integration.
Each Add-on start performs this sequence:

1. Connect to Home Assistant's MCP server and obtain its function schemas.
2. Call MCP `GetLiveContext` to build the list of entities exposed to Assist.
3. Read Home Assistant capability attributes for those exposed entities.
4. Generate only the extra functions supported by the discovered entities.
5. Register MCP and generated handlers in the Qwen Realtime session.

The default generated capability IDs are:

| ID | Generated behavior |
| --- | --- |
| `climate_mode` | Set HVAC mode such as cool, heat, dry, fan-only, or auto. |
| `climate_fan` | Set a supported climate fan mode. |
| `climate_swing` | Set a supported climate swing mode. |
| `fan_preset` | Set a fan or purifier preset. |
| `select_option` | Set a supported Home Assistant select option. |

The generated schema uses capability values reported by Home Assistant. The
handler validates the selected value again and treats an empty service result
as failure, preventing a spoken success for an action that changed nothing.

The **Enabled capability tools** setting accepts a comma-separated subset of
the IDs above. An empty list enables every supported capability template.

## 5. Connect Home Assistant Voice PE

Install the companion firmware from
[xandervanerven/home-assistant-voice-pe](https://github.com/xandervanerven/home-assistant-voice-pe):

1. Install and open **ESPHome Device Builder**.
2. Adopt the Voice PE and retain the generated API encryption and OTA values in
   ESPHome's private `secrets.yaml`.
3. Replace the adopted device configuration with the DHCP or static-IP stub
   published by the firmware repository.
4. Keep its package reference so ESPHome fetches the complete firmware.
5. Use the default `ws://homeassistant.local:8080/`, or set
   `substitutions.va_url` to the resolvable HAOS host and the configured Add-on
   WebSocket port.
6. Flash the first replacement firmware over USB. Later updates can use OTA.

Never place Wi-Fi credentials, OTA passwords, API encryption keys, provider
keys, or private addresses in this Add-on repository.

## 6. First-run verification

Start the Add-on before booting or restarting the Voice PE. Check for:

```text
Home Assistant MCP Client initialized
Entity catalog built: <count> exposed entities
Capability discovery: ...
Auto-generated <count> capability tools
Qwen Realtime Service created
Starting WebSocket server and pipeline
```

Then verify these scenarios:

1. **Connection:** Voice PE enters the idle LED state and the Add-on reports a
   device WebSocket connection.
2. **Conversation:** wake the device and ask a non-control question; speech is
   returned and the device returns to idle.
3. **Core MCP control:** ask to turn an exposed light on and off; the log shows
   tool receipt, execution, result return, and the entity changes.
4. **Generated tool:** request a capability the device actually supports, such
   as an exposed climate entity's dry mode. The log names the generated tool and
   Home Assistant reports the new state.
5. **Safety boundary:** an entity not exposed to Assist must not appear in the
   entity catalog or generated tools.
6. **Interruption:** press the center button or use the firmware stop behavior
   during playback; audio stops and the state returns to idle.

## 7. Recommended starting settings

| Setting | Default | Guidance |
| --- | ---: | --- |
| Follow-up listening | 8 s | Set to 0 while isolating single-turn problems. |
| Wake/follow-up open delay | 700 ms | Helps prevent wake chime and speaker-tail echo. |
| Playback prebuffer | 150 ms | Increase for Wi-Fi jitter; reducing it lowers latency but raises underrun risk. |
| VAD eagerness | low | Better tolerance for natural pauses. |
| Qwen tool timeout | 15 s | Increase only for unusually slow HA services. |
| Tool debug logs | off | Enable temporarily for protocol diagnosis. |
| Audio recording | off | Enable only for a controlled test and remove recordings afterward. |

## 8. Troubleshooting

**The Add-on does not start**

- Confirm the Qwen API Key and Workspace ID fields are not empty.
- Confirm the region matches the workspace.
- Read the first error in the Add-on log; later reconnect messages are often a
  consequence of that initial error.

**No Home Assistant tools**

- Confirm Model Context Protocol Server is installed.
- Confirm at least one entity is exposed to Assist.
- Keep MCP URL and token empty for local HAOS use.
- Restart the Add-on so discovery runs again.

**MCP works but generated tools are zero**

- This is normal if the exposed entities have none of the supported capability
  attributes.
- Check `Capability discovery` in the log and the Enabled capability tools list.
- Confirm the entity is exposed to Assist; REST visibility alone is not enough.

**Voice PE cannot connect**

- Confirm the Add-on is running and the firmware `va_url` resolves from the
  device's network.
- Confirm both sides use the same WebSocket port.
- Check ESPHome device logs and the Add-on log at the same timestamp.

**Audio breaks up**

- Raise playback prebuffer gradually.
- Check Wi-Fi signal and packet loss between Voice PE and HAOS.
- Use diagnostic recording only long enough to reproduce the problem.

## 9. References

- [Qwen Realtime overview](https://help.aliyun.com/zh/model-studio/realtime)
- [Qwen Realtime client events](https://help.aliyun.com/zh/model-studio/client-events)
- [Qwen Realtime server events](https://help.aliyun.com/zh/model-studio/server-events)
- [Voice PE companion firmware](https://github.com/xandervanerven/home-assistant-voice-pe)
- [Home Assistant MCP Server](https://www.home-assistant.io/integrations/mcp_server/)
