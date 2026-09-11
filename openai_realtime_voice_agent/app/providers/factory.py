"""Application composition; vendor selection never enters the shared Core."""

import os
from app.core.service import RealtimeCoreService
from app.providers.base import ProviderSession
from app.providers.qwen import QwenConfig, QwenRealtimeProvider
from app.tools.schema import CanonicalTool
from app.tool_names import canonical_tool_name


def create_qwen_service(*, api_key, model, session_properties, **kwargs):
    audio = getattr(session_properties, "audio", None)
    audio_input = getattr(audio, "input", None)
    turn = getattr(audio_input, "turn_detection", None)
    output = getattr(audio, "output", None)
    config = QwenConfig(api_key=api_key, model=model,
        workspace_id=os.getenv("QWEN_WORKSPACE_ID", "").strip(),
        region=os.getenv("QWEN_REGION", "cn-beijing").strip(),
        voice=getattr(output, "voice", None) or "longanqian",
        turn_detection=getattr(turn, "type", None) or "semantic_vad",
        vad_threshold=getattr(turn, "threshold", None),
        vad_silence_duration_ms=getattr(turn, "silence_duration_ms", None))
    config.validate()
    definitions = tuple(CanonicalTool(t["name"], t.get("description", ""),
        t.get("parameters", {"type": "object"}),
        read_only=canonical_tool_name(t["name"]) in {"GetLiveContext", "GetCurrentTime", "GetWeather", "web_search"})
        for t in getattr(session_properties, "tools", None) or [])
    def number(name, default, minimum=0):
        try:
            return max(minimum, float(os.getenv(name, str(default))))
        except ValueError:
            return default
    return RealtimeCoreService(provider_factory=lambda: QwenRealtimeProvider(config),
        session=ProviderSession(getattr(session_properties, "instructions", None) or "", definitions,
                                getattr(session_properties, "max_output_tokens", None)),
        tool_timeout_s=number("QWEN_TOOL_TIMEOUT_SECONDS", 15, 1),
        follow_up_seconds=number("FOLLOW_UP_LISTEN_SECONDS", 8),
        follow_up_open_delay_s=number("FOLLOW_UP_OPEN_DELAY_MS", 700) / 1000,
        router_enabled=os.getenv("CONTROL_ROUTER_ENABLED", "true").lower() != "false",
        enforce_cancel_boundary=False)
