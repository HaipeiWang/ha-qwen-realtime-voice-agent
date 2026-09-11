"""Qwen model, voice and schema rules, extracted without changing behavior."""

import logging
from app.tool_names import canonical_tool_name
from app.tools.descriptions import (
    CORE_TOOL_DESCRIPTIONS, CORE_PROPERTY_DESCRIPTIONS, CORE_TOOL_PROPERTY_DESCRIPTIONS,
)

logger = logging.getLogger(__name__)


class QwenProfile:
    @staticmethod
    def _as_dict(value):
        if value is None:
            return None
        if hasattr(value, "model_dump"):
            return value.model_dump(exclude_none=True)
        return value

    @staticmethod
    def _is_qwen_audio_realtime_model(model: str) -> bool:
        return str(model or "").startswith("qwen-audio-3.0-realtime-")

    _AUDIO_SYSTEM_VOICES = frozenset({
        "longanqian",
        "longanlingxin",
        "longanlingxi",
        "longanxiaoxin",
        "longanlufeng",
    })

    _OMNI_SYSTEM_VOICES = frozenset({
        "Tina",
        "Cindy",
        "Liora Mira",
        "Sunnybobi",
        "Raymond",
        "Ethan",
        "Theo Calm",
        "Serena",
        "Harvey",
        "Maia",
        "Evan",
        "Qiao",
        "Momo",
        "Wil",
        "Angel",
        "Li Cassian",
        "Mia",
        "Joyner",
        "Gold",
        "Katerina",
        "Ryan",
        "Jennifer",
        "Aiden",
        "Mione",
        "Sunny",
        "Dylan",
        "Eric",
        "Peter",
        "Joseph Chen",
        "Marcus",
        "Li",
        "Kiki",
        "Rocky",
        "Sohee",
        "Lenn",
        "Ono Anna",
        "Sonrisa",
        "Bodega",
        "Emilien",
        "Andre",
        "Radio Gol",
        "Alek",
        "Rizky",
        "Roya",
        "Arda",
        "Hana",
        "Dolce",
        "Jakub",
        "Griet",
        "Eliška",
        "Marina",
        "Siiri",
        "Ingrid",
        "Sigga",
        "Bea",
        "Chloe",
    })

    @classmethod
    def _validated_voice_for_model(cls, model: str, requested_voice: str | None) -> str:
        """Validate the model-family voice and fall back without killing audio.

        The UI prevents new incompatible combinations. This guard handles old
        option records, YAML/API edits and unknown custom values. Qwen-Audio
        cloned voice IDs are provider-generated and carry a qwen-audio prefix.
        """
        model = str(model or "").strip()
        voice = str(requested_voice or "").strip()
        if cls._is_qwen_audio_realtime_model(model):
            valid = voice in cls._AUDIO_SYSTEM_VOICES or voice.startswith(
                (
                    "qwen-audio-3.0-realtime-flash-",
                    "qwen-audio-3.0-realtime-plus-",
                )
            )
            if valid:
                return voice
            logger.error(
                "Incompatible Qwen voice: model=%s requires a longan* system "
                "voice or Qwen-Audio cloned voice ID; got %r. Falling back to "
                "longanqian.",
                model,
                voice,
            )
            return "longanqian"

        if voice in cls._OMNI_SYSTEM_VOICES:
            return voice
        logger.error(
            "Incompatible Qwen voice: model=%s requires a Qwen3.5 Omni voice; "
            "got %r. Falling back to Tina.",
            model,
            voice,
        )
        return "Tina"

    @classmethod
    def _to_qwen_tool(cls, value):
        """Convert one Pipecat/OpenAI tool into Qwen's native nested schema."""
        raw = cls._as_dict(value)
        if not isinstance(raw, dict) or raw.get("type") != "function":
            raise ValueError(f"tool must be an object with type=function: {raw!r}")

        # Qwen native Realtime requires the function object to be nested.  The
        # project's inherited OpenAI Realtime format is deliberately flat.
        function = raw.get("function")
        if function is None:
            function = {
                key: raw.get(key)
                for key in ("name", "description", "parameters")
                if raw.get(key) is not None
            }
        if not isinstance(function, dict):
            raise ValueError(f"tool.function must be an object: {raw!r}")

        name = function.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"tool.function.name must be a non-empty string: {raw!r}")
        parameters = function.get("parameters") or {"type": "object", "properties": {}}
        if not isinstance(parameters, dict) or parameters.get("type") != "object":
            raise ValueError(f"{name}: function.parameters.type must be object")
        properties = parameters.get("properties", {})
        required = parameters.get("required", [])
        if not isinstance(properties, dict) or not isinstance(required, list):
            raise ValueError(f"{name}: invalid properties/required JSON schema")

        canonical_name = canonical_tool_name(name)
        description = CORE_TOOL_DESCRIPTIONS.get(
            canonical_name, function.get("description") or ""
        )
        # HA MCP's generic schemas are intentionally provider-neutral and many
        # selector fields have no descriptions.  Qwen's Chinese audio model was
        # observed choosing a spoken false-success with the 24 English schemas,
        # while an isolated native probe with these four Chinese descriptions
        # emitted HassTurnOff with the exact entity/area/domain arguments.  Keep
        # the original types/constraints and only enrich descriptions.
        qwen_properties = {}
        for property_name, property_schema in properties.items():
            if isinstance(property_schema, dict):
                property_schema = dict(property_schema)
                chinese_description = CORE_TOOL_PROPERTY_DESCRIPTIONS.get(
                    (canonical_name, property_name)
                ) or CORE_PROPERTY_DESCRIPTIONS.get(property_name)
                if canonical_name in CORE_TOOL_DESCRIPTIONS and chinese_description:
                    property_schema["description"] = chinese_description
            qwen_properties[property_name] = property_schema

        return {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": {
                    **parameters,
                    "properties": qwen_properties,
                    "required": required,
                },
            },
        }

    @staticmethod
    def _qwen_tool_names(tools):
        names = set()
        for tool in tools or []:
            if not isinstance(tool, dict):
                continue
            function = tool.get("function")
            if isinstance(function, dict) and isinstance(function.get("name"), str):
                names.add(function["name"])
        return names
