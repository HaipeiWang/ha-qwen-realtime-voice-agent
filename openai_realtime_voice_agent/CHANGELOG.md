# Changelog

All notable changes to this add-on. Newest first.

## 0.10.0-beta.1

### Added

- Conversation-scoped Qwen connections: only an explicit Voice PE `wake` event
  creates a provider session, while ordinary PCM can never reconnect implicitly.
- One lifecycle lock and provider generation checks across open, close, audio,
  response boundaries, and asynchronous Home Assistant tool results.
- Explicit Voice PE `conversation_end` handling and a dedicated ordered
  `QwenResponseDoneFrame` delivered through the same pipeline as reply audio.

### Fixed

- `flush` now clears only uncommitted microphone audio and never closes Qwen.
- Qwen closes only after `response.done`, the paced player has drained, and the
  follow-up conversation window has ended. Session close resets playback, VAD,
  reply boundaries, TTS and pending-tool state.
- Late tool results are discarded when their generation no longer matches, so
  they cannot leak into a later conversation.
- Removed the direct response-boundary callback that could overtake queued audio
  and discard most of a reply. The ordered frame preserves complete playback.
- Current firmware wake gating no longer loses the user's first word; the legacy
  Add-on wake-audio guard now defaults to zero.
- Successful forced Qwen transport cleanup after its 1.5-second close deadline is
  logged at INFO instead of being presented as a user-visible failure.

### Companion firmware

- Use [HaipeiWang/home-assistant-voice-pe-qwen](https://github.com/HaipeiWang/home-assistant-voice-pe-qwen)
  `1.3.0-beta.1` for the matching `conversation_end` protocol, wake-chime boundary,
  warm playback chain, and echo-resistant stop threshold.

## 0.9.3

### Changed

- Split the mixed voice selector into family-safe Qwen-Audio and Qwen3.5 Omni
  selectors. The Audio selector exposes only documented longan system voices
  plus cloned voice IDs; the Omni selector exposes only documented Omni voices.
- The runtime now validates the selected voice against the active model family.
  Legacy YAML/API mismatches emit an explicit error and fall back to
  `longanqian` for Audio or `Tina` for Omni instead of sending an invalid
  session configuration to Qwen.

## 0.9.2

### Added

- The Configuration page's Realtime model selector now offers Qwen-Audio 3.0
  Flash/Plus and Qwen 3.5 Omni Flash/Plus. New installations default to
  `qwen-audio-3.0-realtime-flash`.

### Changed

- Qwen-Audio models now use their documented smart-turn, PCM session profile
  and system voices, while Omni models retain their existing manual-response
  profile. A saved Omni Tina/Ethan voice maps safely to Audio's longanqian.

## 0.9.1

### Fixed

- Treat every Voice PE `wake` message as a hard input-generation boundary:
  clear the uncommitted Qwen audio buffer, cancel a pending transcript gate,
  and discard the prior turn's candidate transcript without resetting normal
  conversation memory.
- Reject microphone PCM during a configurable post-wake guard and reject orphan
  Qwen VAD/transcript events unless the current wake generation first received
  a valid `speech_started`. This prevents chime tails and delayed packets from
  answering a previous request.

## 0.9.0

### Automatic Home Assistant capability tools

- Starts from the official Home Assistant MCP tool set and builds additional
  Qwen function tools for capabilities discovered at Add-on startup, including
  climate mode, fan mode, swing mode, fan presets, and select options.
- Generated tools are limited to entities returned by MCP `GetLiveContext`, so
  the user's **Expose to Assist** choices remain the control boundary even
  though capability details are read from Home Assistant's REST API.
- Adds deterministic routing for common Chinese climate, fan, cover, light,
  switch, and sub-function commands. Failed service calls are returned as
  failures instead of being spoken as successful actions.
- Removes the development Workspace ID from all source, defaults, and probe
  scripts. Every installation must provide its own Qwen API key and Workspace
  ID in the Add-on Configuration page.
- Adds current `repository.yaml` metadata and complete installation guidance so
  the repository can be added directly to the Home Assistant Add-on Store.

## 0.8.0

### Qwen Omni Realtime configuration and documentation

- **The Add-on is now presented honestly as “Qwen Omni Realtime Voice Agent”.**
  The historical slug `openai_realtime_voice_agent` is deliberately retained so
  existing installations, the Voice PE firmware, and saved settings continue to
  work without a reinstall.
- **The Configuration tab now exposes the actual Qwen connection settings:**
  Qwen API key, Workspace ID, region, native Realtime model, voice, playback
  speed, Home Assistant MCP URL, and the tool allowlist. Chinese labels are
  included for a Chinese Home Assistant UI.
- **Changing the Qwen API key in the Configuration tab now takes effect.** The
  configured value takes precedence; a legacy `/data/qwen_api_key` file and the
  former OpenAI-key option are fallback migration paths only.
- **Workspace ID and region are no longer hard-coded.** The native Realtime
  WebSocket endpoint is constructed from the two values in the Configuration
  tab.
- **README and DOCS now describe the deployed architecture:** native Qwen
  Realtime WebSocket speech-to-speech, Home Assistant MCP function calls,
  Voice PE's 16 kHz PCM link, Qwen semantic VAD, and the Qwen limitation that
  native web search cannot be enabled alongside function tools.

## 0.6.0

> ⚠️ **This update has two parts — please update both:**
> 1. **This add-on** (the update you're installing now).
> 2. **The Voice PE firmware** — open **ESPHome Device Builder** and click **Update** (or **Install**) on your device.
>
> The device and the add-on use one shared protocol; updating only one half can cause odd behaviour.

A reliability and voice-control polish release.

**Stop word**

- **Saying "stop" now usually works on the first try.** The spoken "stop" could
  previously be answered by the assistant a moment later, so you sometimes had to
  repeat it; that follow-on reply is now cancelled, so a single "stop" is
  typically enough.
- **Saying "stop" during a web search returns the device to rest promptly** — the
  light ring no longer keeps showing the "replying" animation for several seconds.
- **Fewer accidental stops** on the assistant's own speech.
- The light ring briefly flashes **red** to confirm your "stop" was registered. *(firmware)*

**Reliability**

- **No more unresponsive sessions.** A silently dropped connection to OpenAI is
  now detected and repaired within seconds, instead of leaving the assistant deaf
  until a restart.
- **The roughly hourly reconnect now happens proactively during a quiet moment**,
  so it practically never interrupts a conversation.
- **Smart-home commands are no longer cancelled** if you keep talking while they run.
- The light can no longer get **stuck on "thinking"**, and long web searches get
  all the time they need.

**No more "answers out of nowhere"**

- The assistant no longer occasionally replies — or repeats its previous answer —
  right after the wake word when you said nothing.
- A sentence that got cut off is no longer answered minutes later on your next wake.

**Settings**

- New **"Wake mic delay"** setting: a short pause after the wake chime before the
  mic opens, so the chime can't be mistaken for speech (default 700 ms).
- The **"Follow-up mic delay"** default is now **700 ms**. Existing installs keep
  their saved value — raise yours if the assistant ever answers right after its
  own reply.

## 0.5.0

A big stable release: everything built and tested on the dev channel over the
past days. **Also update the Voice PE firmware** (v1.1.0 — one click in ESPHome
Builder) to get the full effect of the "stop" improvements; the two halves
work best together.

- **"Stop" now works through the whole reply AND the after-reply listening
  window.** The device detects the word more reliably, and the bridge treats
  it as authoritative: in-flight audio is discarded and an answer OpenAI had
  already started for the stop word itself is cancelled on arrival — no more
  "Okay, I'll be quiet" replies to your "stop".
- **Fixed: an answer could cut off mid-sentence, after which the assistant
  went deaf** until the next reconnect. Harmless protocol races (e.g. your
  sentence being split into two turns by a pause) no longer kill the session.
- **Fixed an audio race that could inject noise/hiss into replies** (firmware,
  paired with this release).
- **Mute behaves properly now** (firmware): the ring goes dark with red
  markers by the microphones, and muting also ends an open listening window
  immediately — both from Home Assistant and with the physical side switch.
- **The LED Ring switch in Home Assistant works again** (firmware): entity off
  = device dark at rest; entity on = the gentle "ready" pulse.
- **Completely reworked Configuration tab**: options grouped logically
  (Basics → Model & voice → Conversation → Web search → Audio →
  Home Assistant → Advanced), every description rewritten in plain practical
  language, and a full Dutch translation included (shown automatically when
  your HA is set to Dutch). Confusing or broken switches were removed; rarely
  needed expert fields stay hidden until you need them.
- **The add-on now has its own icon.**
- Friendlier defaults for new installs: follow-up mic delay 200 ms and
  playback buffer 150 ms. **Existing installs keep their saved values** — if
  yours still say 0, consider setting 200/150 manually (Conversation / Audio
  groups) for fewer ghost triggers and less crackle.

### Heads-up: the firmware stub template was improved

The per-device stub in ESPHome Builder used to reference the firmware in a
form that lets ESPHome **cache the downloaded YAML for a day** — clicking
Update shortly after a release could then silently rebuild yesterday's code.
The stub templates in the firmware repo are fixed; existing users can apply
the same fix once by replacing **only the `packages:` block** in their
device's YAML in ESPHome Builder (everything else — your name, secrets,
`dashboard_import` — stays exactly the same):

```yaml
packages:
  realtime:
    url: https://github.com/HaipeiWang/home-assistant-voice-pe-qwen
    ref: main
    files: [home-assistant-voice.realtime.yaml]
    refresh: 0s
```

Current templates for reference:
[esphome-builder.dhcp.yaml](https://github.com/HaipeiWang/home-assistant-voice-pe-qwen/blob/main/esphome-builder.dhcp.yaml) ·
[esphome-builder.static-ip.yaml](https://github.com/HaipeiWang/home-assistant-voice-pe-qwen/blob/main/esphome-builder.static-ip.yaml)

## 0.4.26

- **Web search is now ON by default**, using **gpt-5.5** (the best-quality search
  model), so the assistant can look things up online — weather, news, facts — out
  of the box. **Existing installs keep their saved setting**: if you had it off,
  switch `enable_web_search` on (and set `web_search_model` to `gpt-5.5`) in the
  add-on Configuration. The cheaper mini/nano models stay available.

## 0.4.25

- **Fix:** the first thing you said in the few seconds right after an automatic
  reconnect (e.g. after the 60-minute session cap) could be ignored
  (`conversation_already_has_active_response`). The reconnected session no longer
  creates a duplicate response, so that turn answers normally.

## 0.4.24

- **Renamed** to **OpenAI Realtime 2 Voice Agent**.
- Rewrote the store/info description and added a full **Documentation** tab
  (install steps, OpenAI key, Home Assistant MCP setup, recommended settings, web
  search, credits). Removed stale text from the original upstream client.
- Default system prompt is now an English, voice-tuned prompt (silent tool calls,
  varied confirmations, language pinning). Your own saved prompt is not changed.
- Default `follow_up_open_delay_ms` and `playback_prebuffer_ms` set to `0` (raise
  them if the device hears its own tail or you hear crackle).

## 0.4.23

- **Fix:** the 60-minute session cap sometimes left the session dead until a
  restart. It now reconnects automatically in all cases (both the keepalive-drop
  and the `session_expired` forms).

## 0.4.22

- **New options:** voice **speed** (0.25–1.5), **max reply length**
  (`max_output_tokens`), and **input noise reduction** (off / near-field /
  far-field). All default to current behaviour.

## 0.4.21

- Model, voice, web-search-model and transcription-model options are now
  **dropdowns** with the known-good values, each with a **custom** entry if you
  need a value not in the list.

## 0.4.20

- **New:** optional **web search**. Turn on `enable_web_search` to let the
  assistant look things up online (weather, news, facts). Uses your OpenAI key;
  off by default. Model configurable via `web_search_model` (default gpt-5.4-mini).

## 0.4.19

- Clarified the MCP option help text for both the built-in HA MCP Server and the
  unofficial ha-mcp add-on.

## 0.4.18

- **Fix:** removed a meaningless filler reply ("I'm ready to continue…") that could
  appear on the first turn of a session.

## 0.4.17

- **Fix:** cap restored conversation history (`max_context_messages`, default 12) to
  bound per-turn token cost and avoid hitting OpenAI's rate limit.

## 0.4.16

- **Fix:** the device no longer gets stuck blinking "thinking" after a turn-ending
  error (e.g. a rate limit) — it returns to idle so you can retry.

## 0.4.14

- **New:** `playback_prebuffer_ms` jitter buffer to reduce occasional crackle at the
  start of replies.

## 0.4.12 – 0.4.13

- **Fix:** "say stop, then immediately ask again → silence". Disabled the broken
  server-side audio truncation that wedged the next turn.

## 0.4.9 – 0.4.11

- **New:** auto-reconnect the OpenAI Realtime session when its connection drops
  (keepalive timeout / 60-minute cap), instead of going dead until a restart.
  Refined so a normal device disconnect doesn't trigger an unnecessary reconnect.

## 0.4.6 – 0.4.8

- **New:** configurable post-reply **follow-up listening window** (answer back
  without re-saying the wake word) + its open-delay, and per-option help text in the
  UI.
- **New:** the assistant's and user's transcripts are logged to the add-on log
  (`🤖 assistant:` / `🗣️ user:`).

## 0.4.0 – 0.4.4

- **Fix:** resample the device's 16 kHz mic to the 24 kHz OpenAI requires (garbled
  speech), and drop empty audio chunks.
- **New:** device **"stop"** interrupt now actually cancels the reply and clears
  buffered audio.

## 0.3.x

- Switched the target to **gpt-realtime-2**, pinned pipecat-ai 0.0.97, and tuned
  turn detection (semantic VAD), phase delivery to the device, and the startup
  sequence to stop double-responses. Made the disconnect tool and transcription
  model configurable.

## Earlier

- Initial pipecat + WebSocket implementation (forked from
  [fjfricke/ha-openai-realtime](https://github.com/fjfricke/ha-openai-realtime)).
