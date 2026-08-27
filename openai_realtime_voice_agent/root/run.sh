#!/usr/bin/with-contenv bashio
set -e

# Supervisor normally serves add-on options through bashio. After a HAOS
# reboot we have observed that endpoint briefly return an empty object even
# though the authoritative /data/options.json bind mount is already present.
# Falling back to the local read-only file prevents a false "API key missing"
# crash loop and does not expose option values in logs.
config_value() {
    local key="$1"
    local value
    value=$(bashio::config "$key" 2>/dev/null || true)
    if { [ -z "$value" ] || [ "$value" = "null" ]; } && [ -r /data/options.json ]; then
        value=$(jq -r --arg key "$key" \
            '.[$key] // empty | if type == "array" then .[] else . end' \
            /data/options.json)
    fi
    printf '%s' "$value"
}

config_has_value() {
    local value
    value=$(config_value "$1")
    [ -n "$value" ] && [ "$value" != "null" ]
}

# --- 🔑 Basics ---
# The Configuration tab is authoritative: changing qwen_api_key there must
# take effect after an add-on restart. The old data file and OpenAI option are
# read only as migration fallbacks for existing installations.
QWEN_API_KEY=$(config_value 'qwen_api_key')
if [ -z "$QWEN_API_KEY" ] && [ -r /data/qwen_api_key ]; then
    QWEN_API_KEY=$(tr -d '\r\n' < /data/qwen_api_key)
fi
if [ -z "$QWEN_API_KEY" ]; then
    QWEN_API_KEY=$(config_value 'openai_api_key')
fi
QWEN_WORKSPACE_ID=$(config_value 'qwen_workspace_id')
QWEN_REGION=$(config_value 'qwen_region')
QWEN_REGION=${QWEN_REGION:-cn-beijing}
INSTRUCTIONS=$(config_value 'instructions')
TRANSCRIPTION_LANGUAGE=$(config_value 'transcription_language')

# --- 🗣️ Model & voice ---
QWEN_MODEL=$(config_value 'qwen_model')
QWEN_VOICE=$(config_value 'qwen_voice')
OPENAI_SPEED=$(config_value 'openai_speed')
MAX_OUTPUT_TOKENS=$(config_value 'max_output_tokens')

# --- 💬 Conversation ---
FOLLOW_UP_LISTEN_SECONDS=$(config_value 'follow_up_listen_seconds')
FOLLOW_UP_OPEN_DELAY_MS=$(config_value 'follow_up_open_delay_ms')
WAKE_OPEN_DELAY_MS=$(config_value 'wake_open_delay_ms')
VAD_EAGERNESS=$(config_value 'vad_eagerness')
PHASE_IDLE_DEBOUNCE_MS=$(config_value 'phase_idle_debounce_ms')

# --- 🌐 Web search ---
ENABLE_WEB_SEARCH=$(config_value 'enable_web_search')
WEB_SEARCH_MODEL=$(config_value 'web_search_model')

# --- 🎚️ Audio ---
PLAYBACK_PREBUFFER_MS=$(config_value 'playback_prebuffer_ms')
NOISE_REDUCTION=$(config_value 'noise_reduction')

# --- 🏠 Home Assistant ---
HA_MCP_URL=$(config_value 'ha_mcp_url')
LONGLIVED_TOKEN=$(config_value 'longlived_token')
MCP_TOOL_ALLOWLIST=$(config_value 'mcp_tool_allowlist')
AUTO_TOOL_GENERATION=$(config_value 'auto_tool_generation')
AUTO_TOOL_CAPABILITIES=$(config_value 'auto_tool_capabilities')

# --- ⚙️ Advanced ---
WEBSOCKET_PORT=$(config_value 'websocket_port')
SESSION_REUSE_TIMEOUT_SECONDS=$(config_value 'session_reuse_timeout_seconds')
MAX_CONTEXT_MESSAGES=$(config_value 'max_context_messages')
TRANSCRIPTION_MODEL=$(config_value 'transcription_model')

# --- 🔍 Debug ---
ENABLE_RECORDING=$(config_value 'enable_recording')
QWEN_TOOL_DEBUG=$(config_value 'qwen_tool_debug')
QWEN_TOOL_TIMEOUT_SECONDS=$(config_value 'qwen_tool_timeout_seconds')

# Validate required configuration
if [ -z "$QWEN_API_KEY" ]; then
    bashio::log.error "QWEN_API_KEY is required but not set"
    exit 1
fi
if [ -z "$QWEN_WORKSPACE_ID" ]; then
    bashio::log.error "QWEN_WORKSPACE_ID is required but not set"
    exit 1
fi

# Export environment variables
export QWEN_API_KEY
export QWEN_WORKSPACE_ID
export QWEN_REGION
export INSTRUCTIONS
export TRANSCRIPTION_LANGUAGE
export QWEN_MODEL
export QWEN_VOICE
export OPENAI_SPEED
export MAX_OUTPUT_TOKENS
export FOLLOW_UP_LISTEN_SECONDS
export FOLLOW_UP_OPEN_DELAY_MS
export WAKE_OPEN_DELAY_MS
export VAD_EAGERNESS
export PHASE_IDLE_DEBOUNCE_MS
export ENABLE_WEB_SEARCH
export WEB_SEARCH_MODEL
export PLAYBACK_PREBUFFER_MS
export NOISE_REDUCTION
export LONGLIVED_TOKEN
export MCP_TOOL_ALLOWLIST
export AUTO_TOOL_GENERATION
export AUTO_TOOL_CAPABILITIES
export WEBSOCKET_PORT
export SESSION_REUSE_TIMEOUT_SECONDS
export MAX_CONTEXT_MESSAGES
export TRANSCRIPTION_MODEL
export ENABLE_RECORDING
export QWEN_TOOL_DEBUG
export QWEN_TOOL_TIMEOUT_SECONDS

# The *_custom escape hatches (🗣️/🌐/⚙️) are optional WITHOUT defaults —
# bashio::config prints "null" for unset optionals, and main.py's
# _resolve_choice would treat that literal string as a real custom value.
# Only export when actually set.
if config_has_value 'qwen_model_custom'; then
    QWEN_MODEL_CUSTOM=$(config_value 'qwen_model_custom')
    export QWEN_MODEL_CUSTOM
fi
if config_has_value 'qwen_voice_custom'; then
    QWEN_VOICE_CUSTOM=$(config_value 'qwen_voice_custom')
    export QWEN_VOICE_CUSTOM
fi
if config_has_value 'web_search_model_custom'; then
    WEB_SEARCH_MODEL_CUSTOM=$(config_value 'web_search_model_custom')
    export WEB_SEARCH_MODEL_CUSTOM
fi
if config_has_value 'transcription_model_custom'; then
    TRANSCRIPTION_MODEL_CUSTOM=$(config_value 'transcription_model_custom')
    export TRANSCRIPTION_MODEL_CUSTOM
fi

# Legacy server_vad escape hatch (⚙️ Advanced, optional WITHOUT defaults).
# bashio::config prints the string "null" for unset optional keys, which would
# crash main.py's float()/int() parsing — so only export when actually set.
# Unset = main.py's hardwired defaults (semantic_vad; 0.5/300/800 if server_vad
# is ever selected).
if config_has_value 'turn_detection_type'; then
    TURN_DETECTION_TYPE=$(config_value 'turn_detection_type')
    export TURN_DETECTION_TYPE
fi
if config_has_value 'vad_threshold'; then
    VAD_THRESHOLD=$(config_value 'vad_threshold')
    export VAD_THRESHOLD
fi
if config_has_value 'vad_prefix_padding_ms'; then
    VAD_PREFIX_PADDING_MS=$(config_value 'vad_prefix_padding_ms')
    export VAD_PREFIX_PADDING_MS
fi
if config_has_value 'vad_silence_duration_ms'; then
    VAD_SILENCE_DURATION_MS=$(config_value 'vad_silence_duration_ms')
    export VAD_SILENCE_DURATION_MS
fi

# Removed options (v0.4.29) — no longer exported; main.py env defaults take
# over: SEMANTIC_VAD_CREATE_RESPONSE=true, ENABLE_DISCONNECT_TOOL=false,
# INTERRUPT_RESPONSE=false, DEVICE_INPUT_SAMPLE_RATE=16000.

# Export HA_MCP_URL if set (empty string means use default in main.py)
if [ -n "$HA_MCP_URL" ]; then
    export HA_MCP_URL
fi

# SUPERVISOR_TOKEN is automatically provided by Home Assistant when homeassistant_api: true

# Start the application
export PYTHONUNBUFFERED=1
exec python3 -m app.main
