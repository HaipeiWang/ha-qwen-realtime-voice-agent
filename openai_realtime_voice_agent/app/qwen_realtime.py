"""Native DashScope/Qwen Realtime bridge for the Voice PE pipeline.

Qwen's realtime WebSocket intentionally resembles OpenAI's event model, but it
is not API compatible: it uses different audio event names, formats and session
fields.  This adapter retains Pipecat's frame and MCP function execution model
while speaking Qwen's native protocol on the provider side.
"""

import asyncio
import base64
import json
import logging
import os
import time
import uuid
from collections import deque
from types import SimpleNamespace

from pipecat.frames.frames import (
    AggregationType,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
    TTSAudioRawFrame,
    TTSTextFrame,
    TranscriptionFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.llm_service import FunctionCallFromLLM, FunctionCallParams, LLMService
from pipecat.services.openai.realtime.llm import OpenAIRealtimeLLMService
from pipecat.utils.time import time_now_iso8601
from websockets.asyncio.client import connect as websocket_connect

from app.control_intent_router import ControlIntentRouter, EntityCatalog, Intent
from app.qwen_frames import QwenResponseDoneFrame

logger = logging.getLogger(__name__)


TOOL_EXECUTION_POLICY = """
你是 Home Assistant 的语音控制助手。所有可用 function 工具都是真实的
Home Assistant 操作或查询能力。

严格规则：
1. 用户要求打开、关闭、调节、查询或控制任何家庭设备时，必须先调用最合适的
   function 工具；绝不能只用自然语言声称“正在执行”或“已经完成”。
2. 仅在收到 function 工具的结果后，才能向用户确认执行结果。
3. 若设备名称不够明确，先使用 GetLiveContext 查询可用实体，或向用户澄清；
   不得虚构操作成功。
4. 普通闲聊无需调用工具。工具调用优先于任何语音回复。
""".strip()

TOOL_UNAVAILABLE_POLICY = """
当前 Home Assistant 工具没有成功注册。对于任何设备控制或状态查询，必须明确告知
用户“设备控制服务暂时不可用”，不得声称正在执行或已经执行。
""".strip()

CORE_TOOL_DESCRIPTIONS = {
    "GetLiveContext": (
        "查询 Home Assistant 中设备、实体或区域的实时状态。目标不明确、需要确认实体或"
        "用户询问当前状态时，必须先调用此工具。"
    ),
    "HassTurnOn": (
        "打开、开启或启动 Home Assistant 设备。用户要求开灯、打开开关或启动设备时，"
        "必须调用此工具后再确认。"
    ),
    "HassTurnOff": (
        "关闭、关掉或停用 Home Assistant 设备。用户要求关灯、关闭开关或停止设备时，"
        "必须调用此工具后再确认。"
    ),
    "HassLightSet": (
        "设置 Home Assistant 灯光亮度、颜色或色温。用户要求调暗、调亮或改变灯光时，"
        "必须调用此工具后再确认。"
    ),
}

CORE_PROPERTY_DESCRIPTIONS = {
    "name": "设备或实体名称，例如卧室吸顶灯。",
    "area": "房间或区域名称，例如卧室。",
    "floor": "楼层名称。",
    "domain": "Home Assistant 实体域；灯使用 light。",
    "device_class": "设备类别。",
    "brightness": "灯光亮度百分比，取值 0 到 100。",
    "color": "灯光颜色名称。",
    "temperature": "灯光色温值。",
}


class QwenRealtimeLLMService(OpenAIRealtimeLLMService):
    """OpenAI-frame-compatible service backed by Qwen's native WebSocket API."""

    def __init__(self, *, api_key: str, model: str, session_properties, **kwargs):
        # Qwen does not use OpenAI's session serializer, so its OpenAI event
        # classes are never sent.  The parent still supplies the mature Pipecat
        # context aggregation and MCP function dispatcher used by this project.
        workspace_id = os.getenv("QWEN_WORKSPACE_ID", "").strip()
        region = os.getenv("QWEN_REGION", "cn-beijing").strip()
        if not workspace_id or not region:
            raise ValueError("QWEN_WORKSPACE_ID and QWEN_REGION must not be empty")
        super().__init__(
            api_key=api_key,
            model=model,
            # Native Qwen Realtime WebSocket endpoint for this Beijing Model
            # Studio workspace. The parent appends the required `?model=...`
            # query parameter during service construction.
            base_url=(
                f"wss://{workspace_id}.{region}.maas.aliyuncs.com/"
                "api-ws/v1/realtime"
            ),
            session_properties=session_properties,
            **kwargs,
        )
        self._qwen_model = model
        # Qwen-Audio has the same event flow as Qwen Omni Realtime, but its
        # session fields and smart-turn behavior are intentionally different.
        # Keep the distinction local to this adapter so the Pipecat/MCP shape
        # and the Voice PE protocol do not fork by model.
        self._provider_auto_response = self._is_qwen_audio_realtime_model(model)
        self._tool_debug = os.getenv("QWEN_TOOL_DEBUG", "false").strip().lower() == "true"
        try:
            self._tool_timeout_s = max(
                1.0, float(os.getenv("QWEN_TOOL_TIMEOUT_SECONDS", "15"))
            )
        except (TypeError, ValueError):
            self._tool_timeout_s = 15.0
        self._expected_qwen_tools = []
        self._expected_tool_names = set()
        self._tools_ready = False
        self._function_argument_deltas = {}
        self._handled_tool_call_ids = set()
        self._pending_tool_call_ids = set()
        self._notified_assistant_item_ids = set()
        self._tool_call_seen_for_turn = False
        self._pending_response_after_tool_result = False
        self._skip_next_response_create = False
        self._tool_unavailable_policy_sent = False
        self._session_instructions = ""
        self._response_boundary_open = False
        # `response.create` and `response.created` are separated by a network
        # round trip.  Treat that gap as an active generation window so a
        # duplicate VAD boundary cannot create a second overlapping response.
        self._response_create_inflight = False
        self._response_cancel_pending = False
        self._response_after_cancel_pending = False
        self._response_cancel_watchdog_task = None
        # Deterministic control-intent routing (Qwen has no tool_choice, so a
        # local router forces high-confidence device commands).
        self.entity_catalog: Optional[EntityCatalog] = None
        self.control_router: Optional[ControlIntentRouter] = None
        self._last_user_transcript = ""
        self._transcript_gate_task = None
        # Every Voice PE wake is an input generation. A delayed VAD stop or
        # transcription from the previous wake must never be allowed to consume
        # the next turn's model response slot or deterministic control router.
        self._turn_generation = 0
        self._speech_started_generation = None
        self._wake_guard_until = 0.0
        self._det_confirm_pending = False
        # Maps tool_name -> cached MCP result for the current turn, populated
        # after the deterministic router executes a tool locally.  Used to
        # short-circuit the model's own duplicate call (see _handle_function_call)
        # so a command is executed exactly once.
        self._det_executed_results: dict = {}
        try:
            self._transcript_wait_s = max(
                0.0, float(os.getenv("CONTROL_ROUTER_TRANSCRIPT_WAIT_SECONDS", "0.7"))
            )
        except (TypeError, ValueError):
            self._transcript_wait_s = 0.7
        self._control_router_enabled = (
            os.getenv("CONTROL_ROUTER_ENABLED", "true").strip().lower() != "false"
        )
        self._interrupt_response = (
            os.getenv("INTERRUPT_RESPONSE", "false").strip().lower() == "true"
        )
        # The Voice PE socket and HA tool catalogue live for the lifetime of the
        # add-on, but the provider socket is conversation-scoped. Keeping a
        # Qwen-Audio socket open while the device is asleep makes the provider's
        # response-idle limit an application failure every few minutes. A wake
        # opens one provider session; the device follow-up cut-off closes it.
        self._conversation_lifecycle_lock = asyncio.Lock()
        self._conversation_ready = asyncio.Event()
        self._conversation_active = False
        self._conversation_close_task = None
        self._provider_generation = 0
        self._provider_state = "disconnected"
        self._provider_close_handler = None
        # Normal idle close is legal only after Qwen's real response.done for
        # the current provider generation has crossed the paced-output boundary.
        # Synthetic/cancellation end frames must never masquerade as that edge.
        self._response_done_generation = None
        self._pending_input_audio = deque()
        self._pending_input_audio_bytes = 0
        self._pending_input_audio_limit = 16000 * 2 * 5  # five seconds, mono PCM16
        # Tool execution is asynchronous. Remember which provider session owns
        # each call so a result finishing after close can never enter the next
        # Qwen conversation.
        self._tool_call_generations = {}
        try:
            follow_up_s = max(0.0, float(os.getenv("FOLLOW_UP_LISTEN_SECONDS", "8")))
            follow_up_delay_s = max(
                0.0, float(os.getenv("FOLLOW_UP_OPEN_DELAY_MS", "700")) / 1000.0
            )
        except (TypeError, ValueError):
            follow_up_s, follow_up_delay_s = 8.0, 0.7
        self._conversation_close_delay_s = follow_up_s + follow_up_delay_s + 2.0

    async def start(self, frame):
        """Start Pipecat without opening Qwen until the device wakes."""
        await LLMService.start(self, frame)
        logger.info("Qwen on-demand mode ready; provider socket will open on device wake")

    def _cancel_conversation_close(self):
        task = getattr(self, "_conversation_close_task", None)
        if task and task is not asyncio.current_task() and not task.done():
            task.cancel()
        self._conversation_close_task = None

    def set_provider_close_handler(self, handler):
        """Register an async reset hook for provider-independent output state."""
        self._provider_close_handler = handler

    async def _notify_provider_closed(self, reason: str):
        handler = self._provider_close_handler
        if handler is None:
            return
        try:
            await handler(reason)
        except Exception:
            logger.exception("Qwen provider-close state reset failed: %s", reason)

    async def open_conversation(self, timeout_s: float = 6.0):
        """Open or reuse Qwen for the current Voice PE conversation."""
        self._cancel_conversation_close()
        async with self._conversation_lifecycle_lock:
            if self._api_session_ready and self._websocket is not None:
                self._conversation_active = True
                self._provider_state = "active"
                logger.info(
                    "Qwen wake reusing provider generation=%u", self._provider_generation
                )
                return True

            self._conversation_active = True
            if self._websocket is not None or self._receive_task is not None:
                await self._disconnect_provider_locked(
                    "stale socket before wake", self._provider_generation
                )
                self._conversation_active = True

            self._provider_generation += 1
            generation = self._provider_generation
            self._provider_state = "connecting"
            self._conversation_ready.clear()
            self._api_session_ready = False
            self._run_llm_when_api_session_ready = False
            self._qwen_first_input_logged = False
            t0 = time.monotonic()
            logger.info("Qwen conversation generation=%u connecting on wake", generation)
            await self._connect()
            if self._websocket is None:
                self._conversation_active = False
                self._provider_state = "disconnected"
                raise ConnectionError("Qwen WebSocket connection could not be established")
            try:
                await asyncio.wait_for(self._conversation_ready.wait(), timeout=timeout_s)
            except asyncio.TimeoutError as exc:
                self._conversation_active = False
                await self._disconnect_provider_locked("session.updated timeout", generation)
                raise TimeoutError("Qwen session initialization timed out") from exc
            if generation != self._provider_generation:
                raise RuntimeError("Qwen generation changed during session initialization")
            self._provider_state = "active"
            logger.info(
                "Qwen conversation generation=%u ready in %.3fs",
                generation,
                time.monotonic() - t0,
            )
            await self._flush_pending_input_audio()
            return True

    async def _connect(self):
        """Connect one WebSocket whose receive loop owns a fixed generation."""
        generation = self._provider_generation
        try:
            websocket = await websocket_connect(
                uri=self.base_url,
                additional_headers={"Authorization": f"Bearer {self.api_key}"},
                close_timeout=1.5,
            )
            if generation != self._provider_generation:
                await websocket.close()
                return
            self._websocket = websocket
            self._receive_task = self.create_task(
                self._receive_task_handler(websocket, generation)
            )
        except Exception as exc:
            logger.exception("Qwen provider connection failed generation=%u", generation)
            self._websocket = None
            self._receive_task = None
            await self.push_error(error_msg=f"Error connecting: {exc}", exception=exc)

    async def _disconnect_provider_locked(self, reason: str, generation: int):
        """Close Qwen while the caller holds the lifecycle lock."""
        if generation != self._provider_generation:
            logger.info(
                "Qwen stale close ignored: requested=%u current=%u reason=%s",
                generation, self._provider_generation, reason,
            )
            return False
        self._provider_state = "closing"
        self._disconnecting = True
        self._api_session_ready = False
        self._conversation_ready.clear()
        receive_task = self._receive_task
        websocket = self._websocket
        self._receive_task = None
        self._websocket = None
        try:
            await self.stop_all_metrics()
            if receive_task:
                await self.cancel_task(receive_task, timeout=0.25)
            if websocket:
                close_started = time.monotonic()
                try:
                    # websockets 15 uses close_timeout internally while it waits
                    # for both the peer close frame and TCP termination. Give
                    # that internal 1.5 s deadline a little scheduling margin;
                    # the former equal 0.5/0.5 deadlines made our outer timeout
                    # win every time even when the library was already closing.
                    await asyncio.wait_for(websocket.close(), timeout=1.75)
                    logger.info(
                        "Qwen close handshake finished in %.3fs code=%s (%s)",
                        time.monotonic() - close_started,
                        getattr(websocket, "close_code", None),
                        reason,
                    )
                except Exception as exc:
                    transport = getattr(websocket, "transport", None)
                    if transport is not None:
                        transport.abort()
                    logger.info(
                        "Qwen close fallback aborted transport after %.3fs (%s): %r",
                        time.monotonic() - close_started,
                        reason,
                        exc,
                    )
        finally:
            self._completed_tool_calls = set()
            self._reset_provider_turn_state()
            await self._notify_provider_closed(reason)
            self._provider_state = "disconnected"
            self._disconnecting = False
            logger.info(
                "Qwen provider generation=%u closed: %s", generation, reason
            )
        return True

    def _reset_provider_turn_state(self):
        """Erase every Qwen-session-local response, VAD and tool latch."""
        self._stop_cancel_response_watchdog()
        self._cancel_transcript_gate()
        self._current_assistant_response = None
        self._current_audio_response = None
        self._qwen_tts_active = False
        self._qwen_audio_bytes = 0
        self._response_boundary_open = False
        self._response_done_generation = None
        self._response_create_inflight = False
        self._response_cancel_pending = False
        self._response_after_cancel_pending = False
        self._pending_response_after_tool_result = False
        self._skip_next_response_create = False
        self._det_confirm_pending = False
        self._speech_started_generation = None
        self._last_user_transcript = ""
        self._tool_call_seen_for_turn = False
        self._tools_ready = False
        self._tool_unavailable_policy_sent = False
        self._function_argument_deltas.clear()
        self._handled_tool_call_ids.clear()
        self._pending_tool_call_ids.clear()
        self._notified_assistant_item_ids.clear()
        self._det_executed_results.clear()
        self._pending_input_audio.clear()
        self._pending_input_audio_bytes = 0

    async def close_conversation(
        self, reason: str = "conversation complete", expected_generation: int | None = None
    ):
        """End Qwen while leaving the Voice PE socket and HA tools online."""
        self._cancel_conversation_close()
        async with self._conversation_lifecycle_lock:
            generation = (
                self._provider_generation
                if expected_generation is None
                else expected_generation
            )
            if generation != self._provider_generation:
                logger.info(
                    "Qwen scheduled close ignored for stale generation=%u current=%u",
                    generation, self._provider_generation,
                )
                return False
            self._conversation_active = False
            self._run_llm_when_api_session_ready = False
            if self._websocket is not None or self._receive_task is not None:
                return await self._disconnect_provider_locked(reason, generation)
            self._reset_provider_turn_state()
            await self._notify_provider_closed(reason)
            self._provider_state = "disconnected"
            return True

    def schedule_conversation_close(self, delay_s: float, reason: str):
        self._cancel_conversation_close()
        generation = self._provider_generation

        async def _close_later():
            try:
                await asyncio.sleep(max(0.0, delay_s))
                await self.close_conversation(reason, expected_generation=generation)
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("Qwen scheduled conversation close failed")

        self._conversation_close_task = asyncio.create_task(
            _close_later(), name="qwen-conversation-close"
        )

    async def on_output_drained(self):
        """Start the close timer only after complete response audio left the pacer."""
        if not self._conversation_active or not self._api_session_ready:
            return
        if self._current_assistant_response or self._pending_tool_call_ids:
            logger.info("Qwen close timer deferred because response/tool work remains")
            return
        if self._response_done_generation != self._provider_generation:
            logger.info(
                "Qwen output boundary ignored for close: no complete response.done "
                "for generation=%u",
                self._provider_generation,
            )
            return
        logger.info(
            "Qwen output drained; generation=%u closes after %.1fs follow-up window",
            self._provider_generation, self._conversation_close_delay_s,
        )
        self.schedule_conversation_close(
            self._conversation_close_delay_s,
            "complete response drained and follow-up window elapsed",
        )

    async def _flush_pending_input_audio(self):
        if not self._pending_input_audio:
            return
        chunks = list(self._pending_input_audio)
        total = self._pending_input_audio_bytes
        self._pending_input_audio.clear()
        self._pending_input_audio_bytes = 0
        logger.info("Qwen flushing %u buffered upstream PCM bytes after session ready", total)
        for chunk in chunks:
            await self._send_audio_bytes(chunk)

    async def reset_conversation(self):
        """Replace a failed active Qwen session without a long close wait."""
        async with self._conversation_lifecycle_lock:
            generation = self._provider_generation
            await self._disconnect_provider_locked("active conversation recovery", generation)
            self._conversation_active = True
            self._llm_needs_conversation_setup = False
            self._run_llm_when_api_session_ready = False
        await self.open_conversation()

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

        description = CORE_TOOL_DESCRIPTIONS.get(
            name, function.get("description") or ""
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
                chinese_description = CORE_PROPERTY_DESCRIPTIONS.get(property_name)
                if name in CORE_TOOL_DESCRIPTIONS and chinese_description:
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

    def _validate_server_tools(self, session):
        server_tools = session.get("tools") if isinstance(session, dict) else None
        server_tools = server_tools if isinstance(server_tools, list) else []
        actual_names = self._qwen_tool_names(server_tools)
        echoed_names = []
        malformed_count = 0
        for tool in server_tools:
            function = tool.get("function") if isinstance(tool, dict) else None
            tool_type = tool.get("type") if isinstance(tool, dict) else None
            name = function.get("name") if isinstance(function, dict) else None
            parameters = function.get("parameters") if isinstance(function, dict) else None
            if (
                tool_type != "function"
                or not isinstance(name, str)
                or not name
                or not isinstance(parameters, dict)
                or parameters.get("type") != "object"
            ):
                malformed_count += 1
                continue
            echoed_names.append(name)
        missing = sorted(self._expected_tool_names - actual_names)
        unexpected = sorted(actual_names - self._expected_tool_names)
        duplicate_names = sorted({name for name in echoed_names if echoed_names.count(name) > 1})
        local_missing = []
        for name in sorted(self._expected_tool_names):
            try:
                if not self.has_function(name):
                    local_missing.append(name)
            except Exception:
                local_missing.append(name)
        expected_count = len(self._expected_qwen_tools)
        valid = (
            expected_count > 0
            and len(server_tools) == expected_count
            and len(echoed_names) == expected_count
            and not missing
            and not unexpected
            and not duplicate_names
            and not local_missing
            and malformed_count == 0
        )
        self._tools_ready = valid

        if valid:
            logger.info(
                "Qwen tool registration verified: expected=%u, registered=%u",
                len(self._expected_tool_names), len(actual_names),
            )
        else:
            logger.error(
                "QWEN TOOL REGISTRATION FAILED: expected=%u registered=%u "
                "missing=%s unexpected=%s duplicates=%s malformed=%u local_missing=%s; "
                "device control disabled for this session",
                expected_count, len(server_tools), missing, unexpected,
                duplicate_names, malformed_count, local_missing,
            )
        if self._tool_debug:
            logger.info(
                "Qwen DEBUG session.updated tools=%s",
                json.dumps(server_tools, ensure_ascii=False, sort_keys=True),
            )
        return valid

    def _build_entity_catalog_hint(self) -> str:
        """Render the exposed entity catalog as a compact prompt hint.

        Qwen hears the user's audio natively and had no way to know that the
        user's "卧室的灯" refers to the entity *named* "卧室吸顶灯".  Injecting
        the exact names + areas removes that ambiguity for the model's own
        tool calls, complementing the deterministic router (which uses the
        same catalog locally).
        """
        catalog = getattr(self, "entity_catalog", None)
        if not catalog or not catalog.entities:
            return ""
        lines = ["当前可控制的 Home Assistant 设备（名称 / 所在区域）："]
        for entity in sorted(catalog.entities, key=lambda e: (e.domain, e.name)):
            if entity.domain not in {
                "light", "switch", "cover", "fan", "climate", "media_player",
            }:
                continue
            area = f"（{entity.area}）" if entity.area else ""
            lines.append(f"- {entity.name} [{entity.domain}]{area}")
        return "\n".join(lines)

    async def send_client_event(self, event):  # pragma: no cover - defensive bridge
        """Prevent inherited OpenAI event classes from reaching DashScope."""
        payload = self._as_dict(event)
        await self._ws_send(payload)

    async def _ws_send_checked(self, payload):
        """Send a protocol-critical event and surface transport failures.

        Pipecat's inherited ``_ws_send`` intentionally converts WebSocket send
        exceptions into ErrorFrames.  That behavior is useful for streaming mic
        audio, but it made a failed function result look successful here.  Tool
        results use this checked path so they are never acknowledged in logs
        unless the provider socket actually accepted the JSON message.
        """
        websocket = self._websocket
        if self._disconnecting or websocket is None:
            raise ConnectionError("Qwen WebSocket is not connected")
        await websocket.send(json.dumps(payload))

    async def _update_settings(self, extra_instructions: str = ""):
        """Translate the project's existing settings into Qwen session.update."""
        settings = self._session_properties
        audio = getattr(settings, "audio", None)
        audio_input = getattr(audio, "input", None)
        audio_output = getattr(audio, "output", None)
        turn = getattr(audio_input, "turn_detection", None)

        is_qwen_audio = self._provider_auto_response
        turn_payload = {"type": "smart_turn"} if is_qwen_audio else {"type": "semantic_vad"}
        if turn and not is_qwen_audio:
            turn_type = getattr(turn, "type", None)
            if turn_type:
                turn_payload["type"] = str(turn_type)
            # Qwen native Realtime does not document OpenAI semantic-VAD's
            # `eagerness` or `tool_choice`; never send provider-incompatible
            # fields merely because they exist in inherited Pipecat models.
            threshold = getattr(turn, "threshold", None)
            if threshold is not None:
                turn_payload["threshold"] = threshold
            silence_duration_ms = getattr(turn, "silence_duration_ms", None)
            if silence_duration_ms is not None:
                turn_payload["silence_duration_ms"] = silence_duration_ms

        if not is_qwen_audio:
            # Qwen Omni remains on bridge-created responses. Its historic
            # server-VAD auto-response could speak a false tool success before
            # the MCP result was available.
            turn_payload["create_response"] = False
            turn_payload["interrupt_response"] = False

        raw_tools = list(getattr(settings, "tools", None) or [])
        qwen_tools = []
        for index, tool in enumerate(raw_tools):
            try:
                qwen_tools.append(self._to_qwen_tool(tool))
            except Exception:
                logger.exception("Invalid Qwen tool definition at index %u: %r", index, tool)

        self._expected_qwen_tools = qwen_tools
        self._expected_tool_names = self._qwen_tool_names(qwen_tools)
        if not qwen_tools:
            logger.error(
                "QWEN TOOL REGISTRATION FAILED: zero valid tools before session.update; "
                "chat remains available but Home Assistant control is disabled"
            )

        # Qwen expects 16-kHz PCM16 input and emits 24-kHz PCM16 output.  The
        # model itself performs ASR, so no separate OpenAI transcription model
        # is sent. The explicit transcription option gives our diagnostics the
        # final user utterance event.
        self._session_instructions = "\n\n".join(
            part for part in (
                getattr(settings, "instructions", "") or "",
                self._build_entity_catalog_hint(),
                TOOL_EXECUTION_POLICY,
                TOOL_UNAVAILABLE_POLICY if not qwen_tools else "",
            ) if part
        )
        effective_instructions = self._session_instructions
        if extra_instructions:
            effective_instructions = f"{effective_instructions}\n\n{extra_instructions}"
        requested_voice = getattr(audio_output, "voice", None)
        effective_voice = self._validated_voice_for_model(
            self._qwen_model, requested_voice
        )
        session = {
            "modalities": ["text", "audio"],
            # The generic inherited OpenAI prompt only says that the assistant
            # *can* control a home. Qwen therefore replied "正在打开" without
            # making a function call. This provider-neutral policy makes real
            # HA control a required precondition for a success confirmation.
            "instructions": effective_instructions,
            "voice": effective_voice,
            "turn_detection": turn_payload,
            "tools": qwen_tools,
        }
        if is_qwen_audio:
            # Qwen-Audio Realtime has fixed PCM16 16 kHz input / 24 kHz output
            # and documents the legacy format fields. Its smart_turn owns the
            # response boundary, so no Omni-only audio object or ASR option is
            # sent.
            session["input_audio_format"] = "pcm"
            session["output_audio_format"] = "pcm"
            session["max_history_turns"] = 50
        else:
            # Qwen Omni Realtime supports explicit sample-rate configuration.
            session["audio"] = {
                "input": {"format": {"type": "pcm", "sample_rate": 16000}},
                "output": {"format": {"type": "pcm", "sample_rate": 24000}},
            }
            session["input_audio_transcription"] = {
                "model": "qwen3-asr-flash-realtime"
            }
        max_tokens = getattr(settings, "max_output_tokens", None)
        if max_tokens:
            session["max_tokens"] = max_tokens
        logger.info(
            "Qwen session.update: model=%s, profile=%s, input=pcm/16000 "
            "output=pcm/24000, modalities=%s, tools=%u, vad=%s, voice=%s",
            self._qwen_model, "audio-smart-turn" if is_qwen_audio else "omni-manual",
            session["modalities"], len(session["tools"]), turn_payload, session["voice"],
        )
        if self._tool_debug:
            logger.info(
                "Qwen DEBUG outbound tools=%s",
                json.dumps(qwen_tools, ensure_ascii=False, sort_keys=True),
            )
        await self._ws_send({"type": "session.update", "session": session})

    async def _apply_tool_unavailable_policy(self):
        """Prevent spoken false-success when the server rejects tool setup."""
        if self._tool_unavailable_policy_sent:
            return
        self._tool_unavailable_policy_sent = True
        instructions = "\n\n".join(
            part for part in (self._session_instructions, TOOL_UNAVAILABLE_POLICY) if part
        )
        logger.error(
            "Qwen tool registration validation failed; applying safe no-control policy"
        )
        try:
            await self._ws_send_checked({
                "type": "session.update",
                "session": {"instructions": instructions},
            })
        except Exception:
            logger.exception("Failed to apply Qwen tool-unavailable safety policy")
            await self.push_error(
                error_msg="Qwen realtime receive loop died: tool safety policy send failed"
            )

    async def refresh_idle_session(self):
        """Reset Qwen's idle timer without closing a healthy WebSocket.

        A partial native ``session.update`` is documented and already used by
        the tool-unavailable policy. Re-sending the unchanged instructions is
        side-effect free, keeps the registered tools intact, and avoids the
        roughly 10-second close handshake observed with proactive reconnects.
        """
        busy = bool(
            self._current_assistant_response
            or self._response_create_inflight
            or self._response_cancel_pending
            or self._pending_tool_call_ids
        )
        if busy:
            logger.info("Qwen idle keepalive skipped because a turn is active")
            return False
        await self._ws_send_checked({
            "type": "session.update",
            "session": {"instructions": self._session_instructions},
        })
        logger.info("Qwen idle keepalive session.update sent")
        return True

    async def _create_response(self):
        # Any genuine new response extends the same wake-owned conversation.
        # In particular, a tool-call response.done may already have drained
        # while the final spoken confirmation is being created.
        self._cancel_conversation_close()
        if self._skip_next_response_create:
            self._skip_next_response_create = False
            logger.error("Qwen response.create suppressed because tool-result delivery failed")
            return
        if self._response_cancel_pending:
            # A fresh user turn can finish while the cancelled response is
            # still closing. Queue exactly one replacement response and wait
            # for response.done (or the watchdog) before creating it.
            self._response_after_cancel_pending = True
            logger.info("Qwen response.create deferred until cancellation boundary")
            return
        if self._response_create_inflight:
            logger.warning("Duplicate Qwen response.create suppressed while request is in flight")
            return
        if not self._api_session_ready:
            self._run_llm_when_api_session_ready = True
            return
        if self._pending_tool_call_ids:
            self._pending_response_after_tool_result = True
            logger.info(
                "Qwen final response deferred; waiting for MCP call_ids=%s",
                sorted(self._pending_tool_call_ids),
            )
            return
        if self._current_assistant_response:
            self._pending_response_after_tool_result = True
            logger.info("Qwen final response deferred until current tool-call response.done")
            return
        # Provider VAD only marks speech boundaries; this bridge explicitly
        # creates normal user-turn and post-tool responses.
        self._pending_response_after_tool_result = False
        self._response_done_generation = None
        if not self._response_boundary_open:
            self._response_boundary_open = True
            await self.push_frame(LLMFullResponseStartFrame())
        await self.start_processing_metrics()
        await self.start_ttfb_metrics()
        self._response_create_inflight = True
        try:
            await self._ws_send_checked({"type": "response.create"})
        except Exception as exc:
            self._response_create_inflight = False
            logger.exception("Qwen response.create delivery failed")
            await self._reset_failed_response("response.create delivery failed")
            await self.push_error(
                error_msg=f"Qwen realtime receive loop died: response.create send failed: {exc!r}"
            )

    # ------------------------------------------------------------------
    # Deterministic control-intent routing
    # ------------------------------------------------------------------

    def _cancel_transcript_gate(self):
        task = self._transcript_gate_task
        if task and not task.done():
            task.cancel()
        self._transcript_gate_task = None

    def begin_wake_turn(self, guard_ms: int = 0):
        """Start a hard input generation boundary for a device wake.

        This does not reset the Qwen conversation history. It only discards
        local, not-yet-committed turn state so late events from the previous
        microphone segment cannot answer the next wake with an old request.
        """
        self._turn_generation += 1
        self._speech_started_generation = None
        self._wake_guard_until = time.monotonic() + max(0, guard_ms) / 1000.0
        self._last_user_transcript = ""
        self._tool_call_seen_for_turn = False
        self._det_executed_results.clear()
        self._cancel_transcript_gate()
        logger.info(
            "Qwen wake generation=%u started; VAD guard=%ums",
            self._turn_generation,
            max(0, guard_ms),
        )

    def _wake_guard_active(self) -> bool:
        return time.monotonic() < self._wake_guard_until

    async def _on_speech_stopped(self):
        """Route the just-finished turn, waiting briefly for the final transcript."""
        if not self._control_router_enabled or self.control_router is None:
            logger.info("Qwen VAD speech_stopped -> explicit response.create")
            await self._create_response()
            return
        self._cancel_transcript_gate()
        self._transcript_gate_task = asyncio.create_task(
            self._transcript_gated_response(), name="qwen-transcript-gate"
        )

    async def _transcript_gated_response(self):
        try:
            loop = asyncio.get_event_loop()
            deadline = loop.time() + self._transcript_wait_s
            transcript = self._last_user_transcript or ""
            while not transcript and loop.time() < deadline:
                await asyncio.sleep(0.04)
                transcript = self._last_user_transcript or ""
            if transcript and await self._try_route_control_intent(transcript):
                return
            logger.info("Qwen VAD speech_stopped -> normal response.create")
            await self._create_response()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Transcript-gated routing failed; falling back to normal response")
            await self._create_response()

    async def _try_route_control_intent(self, transcript: str) -> bool:
        router = self.control_router
        if router is None:
            return False
        intent = router.resolve(transcript)
        if intent is None:
            return False
        if not self.has_function(intent.tool):
            logger.warning(
                "Deterministic intent tool %s is not registered; deferring to model",
                intent.tool,
            )
            return False
        logger.info(
            "Deterministic control intent resolved: tool=%s args=%s",
            intent.tool, intent.arguments,
        )
        result = await self._execute_ha_tool_locally(intent.tool, intent.arguments)
        if result is None:
            logger.error("Deterministic tool execution failed: tool=%s", intent.tool)
            return False
        self._det_executed_results[intent.tool] = result
        await self._speak_deterministic_confirmation(intent, result)
        return True

    async def _execute_ha_tool_locally(self, tool_name: str, arguments: dict):
        """Run a registered MCP handler directly and capture its result.

        Unlike ``run_function_calls`` this does NOT route the result back to
        Qwen as a ``function_call_output`` — the model never issued the call,
        so Qwen has no matching call_id and would reject it.  The captured
        result is used only to build the spoken confirmation.
        """
        item = self._functions.get(tool_name) if hasattr(self, "_functions") else None
        if item is None:
            logger.error("No local MCP handler for tool=%s", tool_name)
            return None
        result_holder: dict = {}

        async def capture(result, *, properties=None):
            result_holder["result"] = result

        params = FunctionCallParams(
            function_name=tool_name,
            tool_call_id=f"det_{uuid.uuid4().hex[:12]}",
            arguments=arguments,
            llm=self,
            context=self._context,
            result_callback=capture,
        )
        try:
            # The registered handler is SafeRealtimeLLMService's
            # `liveness_tracked` wrapper, which already applies the timeout and
            # always reports a result (success or failure) via result_callback.
            await item.handler(params)
        except Exception:
            logger.exception("Deterministic tool execution failed: tool=%s", tool_name)
        result = result_holder.get("result")
        if result is None:
            return None
        if isinstance(result, dict):
            return json.dumps(result, ensure_ascii=False)
        return result if isinstance(result, str) else str(result)

    async def _speak_deterministic_confirmation(self, intent: Intent, result: str):
        note = self._build_confirmation_note(intent, result)
        self._det_confirm_pending = True
        await self._update_settings(extra_instructions=note)
        await self._create_response()

    @staticmethod
    def _build_confirmation_note(intent: Intent, result: str) -> str:
        success = True
        detail = ""
        try:
            data = json.loads(result)
        except (json.JSONDecodeError, TypeError):
            data = None

        if isinstance(data, dict):
            # Home Assistant MCP control actions return
            # {"response_type":"action_done","data":{"success":[...],"failed":[...]}}.
            inner = data.get("data")
            if isinstance(inner, dict):
                ok = inner.get("success") or []
                bad = inner.get("failed") or []
                if bad:
                    success = False
                    detail = QwenRealtimeLLMService._target_names(bad) or str(bad)
                elif ok:
                    success = True
                    detail = QwenRealtimeLLMService._target_names(ok) or "已执行"
            elif "success" in data:
                success = bool(data.get("success"))
                detail = str(data.get("result") or data.get("error") or "")
            elif data.get("result"):
                success = True
                detail = str(data["result"])
            elif data.get("error"):
                success = False
                detail = str(data["error"])
        else:
            # Non-JSON result is treated as a failure message (e.g. the MCP
            # client's "Error calling tool: <MatchFailedError ...>" string).
            success = False
            detail = result

        if not detail:
            detail = result
        if success:
            return (
                f"[系统] 刚刚已通过工具 {intent.tool} 完成操作：{intent.description}。"
                f"执行结果：{detail}。请用一句简洁自然的中文向用户确认这个已完成的结果。"
            )
        return (
            f"[系统] 刚刚尝试通过工具 {intent.tool} 执行：{intent.description}，"
            f"但失败了，原因是：{detail}。请用一句简洁自然的中文如实告诉用户失败原因。"
        )

    @staticmethod
    def _target_names(targets) -> str:
        names = [
            str(t.get("name", ""))
            for t in targets
            if isinstance(t, dict) and t.get("name")
        ]
        return "、".join(names)

    async def _send_audio_bytes(self, audio: bytes):
        await self._ws_send_checked({
            "type": "input_audio_buffer.append",
            "audio": base64.b64encode(audio).decode("ascii"),
        })

    async def _send_user_audio(self, frame):
        if not getattr(self, "_qwen_first_input_logged", False):
            self._qwen_first_input_logged = True
            logger.info("Qwen first upstream PCM: %u bytes", len(frame.audio))
        if not self._api_session_ready or self._websocket is None:
            # PCM is data, never a session-control signal. Only a device wake may
            # create Qwen. Buffer solely while that wake's connection is already
            # in progress; otherwise discard stale/late capture packets.
            if self._conversation_active and self._provider_state == "connecting":
                audio = bytes(frame.audio)
                self._pending_input_audio.append(audio)
                self._pending_input_audio_bytes += len(audio)
                while (
                    self._pending_input_audio
                    and self._pending_input_audio_bytes > self._pending_input_audio_limit
                ):
                    dropped = self._pending_input_audio.popleft()
                    self._pending_input_audio_bytes -= len(dropped)
                    logger.error(
                        "Qwen pre-connect audio buffer overflow; dropped %u oldest PCM bytes",
                        len(dropped),
                    )
                logger.info(
                    "Qwen upstream PCM buffered until wake connection is ready: %u bytes total",
                    self._pending_input_audio_bytes,
                )
            else:
                logger.debug(
                    "Dropped %u PCM bytes without an active wake-owned Qwen session",
                    len(frame.audio),
                )
            return
        await self._send_audio_bytes(frame.audio)

    async def _send_tool_result(self, tool_call_id: str, result: str):
        owner_generation = self._tool_call_generations.get(tool_call_id)
        if (
            owner_generation is None
            or owner_generation != self._provider_generation
            or not self._conversation_active
            or not self._api_session_ready
        ):
            logger.warning(
                "Stale Qwen tool result discarded: call_id=%s owner=%s current=%u state=%s",
                tool_call_id, owner_generation, self._provider_generation, self._provider_state,
            )
            self._tool_call_generations.pop(tool_call_id, None)
            self._pending_tool_call_ids.discard(tool_call_id)
            return False
        output = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
        logger.info(
            "MCP result received -> Qwen result return: call_id=%s, %u characters",
            tool_call_id, len(output),
        )
        try:
            await self._ws_send_checked({
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": tool_call_id,
                    "output": output,
                },
            })
            self._pending_tool_call_ids.discard(tool_call_id)
            logger.info(
                "Qwen tool result returned: call_id=%s pending=%u",
                tool_call_id, len(self._pending_tool_call_ids),
            )
            self._tool_call_generations.pop(tool_call_id, None)
            return True
        except Exception:
            self._tool_call_generations.pop(tool_call_id, None)
            self._pending_tool_call_ids.discard(tool_call_id)
            self._skip_next_response_create = True
            logger.exception("Qwen tool result return failed: call_id=%s", tool_call_id)
            await self._reset_failed_response("tool result return failed")
            # A checked send failure means the provider socket is no longer a
            # trustworthy transport.  Enter the existing ConnectionRecovery
            # path immediately; otherwise HA may have acted while the bridge
            # silently remains unable to return the result.
            await self.push_error(
                error_msg="Qwen realtime receive loop died: tool result send failed"
            )
            return False

    async def _process_completed_function_calls(self, send_new_results: bool):
        """Return only results owned by the current provider generation."""
        if self._context is None:
            return
        sent_new_result = False
        for message in self._context.get_messages():
            if not message.get("role") or message.get("content") == "IN_PROGRESS":
                continue
            call_id = message.get("tool_call_id")
            if not call_id or call_id in self._completed_tool_calls:
                continue
            self._completed_tool_calls.add(call_id)
            if not send_new_results:
                continue
            if self._tool_call_generations.get(call_id) != self._provider_generation:
                logger.info(
                    "Historical/stale tool context ignored: call_id=%s current_generation=%u",
                    call_id, self._provider_generation,
                )
                continue
            if await self._send_tool_result(call_id, message.get("content")):
                sent_new_result = True
        if sent_new_result:
            await self._create_response()

    def _stop_cancel_response_watchdog(self):
        task = self._response_cancel_watchdog_task
        if task and task is not asyncio.current_task():
            task.cancel()
        self._response_cancel_watchdog_task = None

    async def _finish_cancelled_response(self, source):
        """Release a cancelled response and run one queued replacement turn."""
        pending = self._response_after_cancel_pending
        self._stop_cancel_response_watchdog()
        self._response_cancel_pending = False
        self._response_after_cancel_pending = False
        self._response_create_inflight = False
        self._current_assistant_response = None
        self._response_boundary_open = False
        logger.info("Qwen cancelled response finalized via %s", source)
        if pending:
            await self._create_response()

    async def _cancel_response_watchdog_runner(self):
        try:
            await asyncio.sleep(1.5)
            if self._response_cancel_pending:
                logger.warning(
                    "Qwen cancellation boundary timed out; releasing response lock"
                )
                await self._finish_cancelled_response("timeout")
        except asyncio.CancelledError:
            raise

    def _start_cancel_response_watchdog(self):
        self._stop_cancel_response_watchdog()
        self._response_cancel_watchdog_task = asyncio.create_task(
            self._cancel_response_watchdog_runner(),
            name="qwen-response-cancel-watchdog",
        )

    async def _handle_interruption(self):
        # Voice PE owns speaker stop. Clear Qwen's buffered generation only;
        # do not send OpenAI's unsupported conversation.item.truncate event.
        response_active = bool(
            self._current_assistant_response or self._response_create_inflight
        )
        if response_active and not self._response_cancel_pending:
            try:
                await self._ws_send_checked({"type": "response.cancel"})
            except Exception as exc:
                logger.exception("Qwen response.cancel delivery failed")
                await self._reset_failed_response("response.cancel delivery failed")
                await self.push_error(
                    error_msg=f"Qwen realtime receive loop died: response.cancel send failed: {exc!r}"
                )
                return
            self._response_cancel_pending = True
            self._start_cancel_response_watchdog()
            if self._response_boundary_open:
                await self.push_frame(LLMFullResponseEndFrame())
            if getattr(self, "_qwen_tts_active", False):
                self._qwen_tts_active = False
                await self.push_frame(TTSStoppedFrame())
            self._response_boundary_open = False

    async def _reset_failed_response(self, reason):
        """Close all local response boundaries without tearing down the socket."""
        logger.warning("Qwen response state reset: %s", reason)
        if getattr(self, "_qwen_tts_active", False):
            self._qwen_tts_active = False
            await self.push_frame(TTSStoppedFrame())
        # PhaseEmitter's authoritative idle transition is tied to this boundary,
        # even when the provider failed before response.created.  Always emit it
        # so a zero-audio error cannot leave Voice PE spinning in `thinking`.
        await self.push_frame(LLMFullResponseEndFrame())
        self._stop_cancel_response_watchdog()
        self._current_assistant_response = None
        self._response_boundary_open = False
        self._response_create_inflight = False
        self._response_cancel_pending = False
        self._response_after_cancel_pending = False
        self._pending_response_after_tool_result = False
        self._pending_tool_call_ids.clear()
        self._function_argument_deltas.clear()
        if self._det_confirm_pending:
            self._det_confirm_pending = False
            try:
                await self._update_settings()
            except Exception:
                logger.exception("Failed to restore instructions after a deterministic confirmation reset")

    async def _return_tool_failure(self, event, message):
        """Return a tool-scoped error to Qwen so it can speak a safe fallback."""
        call_id = event.get("call_id") if isinstance(event, dict) else None
        name = event.get("name") if isinstance(event, dict) else None
        if not call_id:
            logger.error("Cannot return tool failure without call_id: %s", message)
            await self._reset_failed_response(message)
            return
        self._handled_tool_call_ids.add(call_id)
        self._tool_call_seen_for_turn = True
        self._pending_tool_call_ids.add(call_id)
        self._tool_call_generations[call_id] = self._provider_generation
        output = json.dumps(
            {"success": False, "tool": name, "error": message},
            ensure_ascii=False,
        )
        await self._send_tool_result(call_id, output)
        # This path is not represented by a FunctionCallResultFrame, so trigger
        # the documented post-tool response ourselves.
        await self._create_response()

    async def _handle_function_call(self, event):
        call_id = event.get("call_id") if isinstance(event, dict) else None
        name = event.get("name") if isinstance(event, dict) else None
        if call_id and call_id in self._handled_tool_call_ids:
            logger.debug("Ignoring duplicate Qwen tool completion: call_id=%s", call_id)
            return
        try:
            if not isinstance(call_id, str) or not call_id:
                raise ValueError("missing call_id")
            if not isinstance(name, str) or not name:
                raise ValueError("missing function name")
            # The deterministic router may have already executed this tool for
            # the current turn (using the exact, catalog-resolved entity name).
            # The model then hears the user's audio again and emits its own
            # duplicate call — often with a truncated name that would fail HA
            # matching.  Return the cached result instead of executing twice.
            if name in self._det_executed_results:
                self._handled_tool_call_ids.add(call_id)
                self._tool_call_seen_for_turn = True
                self._tool_call_generations[call_id] = self._provider_generation
                self._function_argument_deltas.pop(call_id, None)
                logger.info(
                    "Deterministic duplicate skipped: tool=%s call_id=%s (returning cached result)",
                    name, call_id,
                )
                await self._send_tool_result(call_id, self._det_executed_results[name])
                return
            arguments = event.get("arguments")
            if not isinstance(arguments, str):
                raise ValueError("arguments must be a JSON string")
            args = json.loads(arguments or "{}")
            if not isinstance(args, dict):
                raise ValueError("decoded arguments must be a JSON object")
            if not self._tools_ready:
                raise RuntimeError("Qwen session tool registration is not verified")
            if name not in self._expected_tool_names:
                raise ValueError(f"tool was not registered in Qwen session: {name}")
            if not self.has_function(name):
                raise ValueError(f"no local MCP handler registered for: {name}")

            # Qwen sometimes supplies the correct unique entity name together
            # with a human room name that conflicts with Home Assistant's
            # inherited device area.  MCP treats name+area as AND constraints,
            # so validate the selector against the Assist-exposed catalog before
            # dispatch.  Ambiguous names and area-wide queries remain untouched.
            router = self.control_router
            if router is not None:
                args, normalization = router.catalog.normalize_control_arguments(
                    name, args
                )
                if normalization is not None:
                    logger.info(
                        "Tool selector normalized before MCP: tool=%s details=%s",
                        name,
                        normalization,
                    )

            self._handled_tool_call_ids.add(call_id)
            self._pending_tool_call_ids.add(call_id)
            self._tool_call_generations[call_id] = self._provider_generation
            self._tool_call_seen_for_turn = True
            self._function_argument_deltas.pop(call_id, None)
            logger.info("Qwen tool call received: %s(%s), call_id=%s", name, args, call_id)
            logger.info("MCP execution started: tool=%s call_id=%s", name, call_id)
            await self.run_function_calls([
                FunctionCallFromLLM(
                    context=self._context,
                    tool_call_id=call_id,
                    function_name=name,
                    arguments=args,
                )
            ])
            # Pipecat dispatches the registered handler as a background task.
            # The actual timeout/completion boundary therefore lives in
            # SafeRealtimeLLMService.register_function(), not around this call.
            logger.info("MCP execution scheduled: tool=%s call_id=%s", name, call_id)
        except Exception as exc:
            if call_id:
                self._pending_tool_call_ids.discard(call_id)
            logger.exception(
                "Qwen function-call parse/dispatch failed; raw_event=%s",
                json.dumps(event, ensure_ascii=False) if isinstance(event, dict) else repr(event),
            )
            await self._return_tool_failure(event, f"Home Assistant operation failed: {exc}")

    async def _recover_function_call_from_output(self, event):
        """Fallback if the provider's dedicated `arguments.done` event is lost."""
        response = event.get("response") if isinstance(event, dict) else None
        output = response.get("output", []) if isinstance(response, dict) else []
        for item in output if isinstance(output, list) else []:
            if isinstance(item, dict) and item.get("type") == "function_call":
                logger.warning(
                    "Recovering Qwen tool call from response.done: call_id=%s",
                    item.get("call_id"),
                )
                await self._handle_function_call(item)

    async def _handle_qwen_error_event(self, event):
        detail = event.get("error", {}) if isinstance(event, dict) else {}
        message = detail.get("message") if isinstance(detail, dict) else str(detail)
        code = detail.get("code") if isinstance(detail, dict) else None
        logger.error("Qwen error event: code=%s message=%s raw=%s", code, message, event)
        code_text = str(code or "").lower()
        message_text = str(message or "").lower()
        fatal_session_error = (
            code_text in {"user_idle_timeout", "session_expired", "session_closed"}
            or "session was closed" in message_text
            or "session is closed" in message_text
        )
        # Most Qwen errors are request-scoped and should not tear down a healthy
        # session.  Session-closing errors are different: waiting for the socket
        # to close left the device blue for about ten seconds in live logs, so
        # surface those immediately to ConnectionRecovery.
        await self._reset_failed_response(f"provider request error: {message or code or 'unknown'}")
        if fatal_session_error:
            await self.push_error(
                error_msg=f"Qwen realtime receive loop died: {code}: {message}"
            )

    async def _notify_assistant_response_created(self, response_id):
        """Bridge Qwen response boundaries into the existing stop-race guard."""
        if not response_id or response_id in self._notified_assistant_item_ids:
            return
        self._notified_assistant_item_ids.add(response_id)
        await self._call_event_handler(
            "on_conversation_item_created",
            response_id,
            SimpleNamespace(id=response_id, role="assistant"),
        )

    async def _handle_server_event(self, event):
        kind = event.get("type")
        if self._tool_debug and (
            "function" in (kind or "")
            or (
                kind in {"conversation.item.created", "response.output_item.added", "response.output_item.done"}
                and isinstance(event.get("item"), dict)
                and event["item"].get("type") == "function_call"
            )
        ):
            logger.info("Qwen DEBUG tool event=%s", json.dumps(event, ensure_ascii=False))

        if kind == "session.created":
            self._stop_cancel_response_watchdog()
            self._tools_ready = False
            self._tool_unavailable_policy_sent = False
            self._handled_tool_call_ids.clear()
            self._pending_tool_call_ids.clear()
            self._notified_assistant_item_ids.clear()
            self._function_argument_deltas.clear()
            self._det_executed_results.clear()
            self._skip_next_response_create = False
            self._response_boundary_open = False
            self._response_create_inflight = False
            self._response_cancel_pending = False
            self._response_after_cancel_pending = False
            logger.info("Qwen session.created; applying native audio/tool configuration")
            await self._update_settings()
        elif kind == "session.updated":
            self._api_session_ready = True
            self._conversation_ready.set()
            tools_valid = self._validate_server_tools(event.get("session") or {})
            if not tools_valid:
                await self._apply_tool_unavailable_policy()
            logger.info("Qwen session.updated; realtime session ready")
            if self._run_llm_when_api_session_ready:
                self._run_llm_when_api_session_ready = False
                await self._create_response()
        elif kind == "input_audio_buffer.speech_started":
            self._cancel_conversation_close()
            if self._wake_guard_active():
                logger.info(
                    "Dropped speech_started in wake guard: generation=%u",
                    self._turn_generation,
                )
                return
            self._speech_started_generation = self._turn_generation
            self._tool_call_seen_for_turn = False
            self._last_user_transcript = ""
            self._det_executed_results.clear()
            self._cancel_transcript_gate()
            # A speech_started while the reply is already playing is almost
            # always the reply's own audio leaking back through the mic (or the
            # follow-up mic reopening on the reply tail).  With hands-free
            # barge-in disabled, interrupting here cuts the reply off mid-word —
            # exactly the "replied partially then stopped" symptom.  Only a real
            # device interrupt (stop button / stop wake word, a separate path)
            # may cancel a playing reply in that mode.
            reply_active = bool(
                self._current_assistant_response or self._response_create_inflight
            )
            if reply_active and not self._interrupt_response:
                logger.info(
                    "speech_started during reply ignored (barge-in disabled)"
                )
            else:
                await self.push_interruption_task_frame_and_wait()
            await self.push_frame(UserStartedSpeakingFrame())
        elif kind == "input_audio_buffer.speech_stopped":
            if self._speech_started_generation != self._turn_generation:
                logger.warning(
                    "Dropped orphan speech_stopped: generation=%u, started_generation=%s",
                    self._turn_generation,
                    self._speech_started_generation,
                )
                return
            await self.start_ttfb_metrics()
            await self.start_processing_metrics()
            await self.push_frame(UserStoppedSpeakingFrame())
            if self._provider_auto_response:
                logger.info(
                    "Qwen-Audio smart_turn speech_stopped -> provider response (no duplicate response.create)"
                )
            elif self._current_assistant_response:
                logger.warning(
                    "Qwen speech_stopped ignored while a response is active"
                )
            else:
                await self._on_speech_stopped()
        elif kind == "conversation.item.input_audio_transcription.completed":
            transcript = event.get("transcript", "")
            if transcript:
                if self._speech_started_generation != self._turn_generation:
                    logger.warning(
                        "Dropped stale transcript outside active wake generation=%u: %r",
                        self._turn_generation,
                        transcript[:120],
                    )
                    return
                self._last_user_transcript = transcript
                await self.push_frame(
                    TranscriptionFrame(transcript, "", time_now_iso8601(), result=event),
                    FrameDirection.UPSTREAM,
                )
        elif kind == "conversation.item.input_audio_transcription.failed":
            logger.error("Qwen input transcription failed: %s", event)
        elif kind == "response.created":
            self._cancel_conversation_close()
            self._response_create_inflight = False
            self._current_assistant_response = event.get("response") or {"id": event.get("response_id")}
            self._qwen_first_audio_logged = False
            self._qwen_audio_bytes = 0
            logger.info("Qwen response.created: %s", self._current_assistant_response.get("id", "unknown"))
            if self._response_cancel_pending:
                logger.info("Qwen response.created arrived after cancellation; audio will be discarded")
                return
            if not self._response_boundary_open:
                self._response_boundary_open = True
                await self.push_frame(LLMFullResponseStartFrame())
            await self._notify_assistant_response_created(
                self._current_assistant_response.get("id")
            )
        elif kind == "response.audio.delta":
            if self._response_cancel_pending:
                logger.debug("Discarding Qwen audio for a cancelled response")
                return
            audio = base64.b64decode(event.get("delta", ""), validate=True)
            if audio:
                if not getattr(self, "_qwen_tts_active", False):
                    self._qwen_tts_active = True
                    await self.push_frame(TTSStartedFrame())
                self._qwen_audio_bytes = getattr(self, "_qwen_audio_bytes", 0) + len(audio)
                if not getattr(self, "_qwen_first_audio_logged", False):
                    self._qwen_first_audio_logged = True
                    logger.info("Qwen first downstream PCM: %u bytes at 24000 Hz", len(audio))
                await self.stop_ttfb_metrics()
                await self.push_frame(TTSAudioRawFrame(audio=audio, sample_rate=24000, num_channels=1))
        elif kind == "response.audio.done":
            logger.info("Qwen audio.done: %u PCM bytes", getattr(self, "_qwen_audio_bytes", 0))
            if getattr(self, "_qwen_tts_active", False):
                self._qwen_tts_active = False
                await self.push_frame(TTSStoppedFrame())
        elif kind == "response.audio_transcript.delta":
            delta = event.get("delta", "")
            if delta:
                frame = TTSTextFrame(delta, aggregated_by=AggregationType.SENTENCE)
                frame.includes_inter_frame_spaces = True
                await self.push_frame(frame)
        elif kind == "response.text.delta":
            delta = event.get("delta", "")
            if delta:
                await self.push_frame(LLMTextFrame(delta))
        elif kind == "response.function_call_arguments.delta":
            call_id = event.get("call_id")
            if call_id:
                self._function_argument_deltas[call_id] = (
                    self._function_argument_deltas.get(call_id, "") + event.get("delta", "")
                )
        elif kind == "response.function_call_arguments.done":
            await self._handle_function_call(event)
        elif kind == "response.output_item.done":
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "function_call":
                await self._handle_function_call(item)
        elif kind == "response.done":
            response = event.get("response") if isinstance(event.get("response"), dict) else {}
            status = response.get("status")
            if self._response_cancel_pending:
                logger.info("Qwen cancelled response.done: status=%s", status)
                await self.stop_processing_metrics()
                await self._finish_cancelled_response("response.done")
                return
            if status in {"failed", "incomplete"}:
                logger.error("Qwen response ended with status=%s: %s", status, event)
            await self._recover_function_call_from_output(event)
            logger.info(
                "Qwen response.done: status=%s emitted=%u PCM bytes tool_called=%s",
                status, getattr(self, "_qwen_audio_bytes", 0), self._tool_call_seen_for_turn,
            )
            await self.stop_processing_metrics()
            self._response_done_generation = self._provider_generation
            # Travel through the same ordered Pipecat queue as every PCM frame.
            # LLMAssistantAggregator consumes the generic full-response end, so
            # this provider-specific DataFrame carries the boundary through it
            # without overtaking audio that is still queued downstream.
            await self.push_frame(
                QwenResponseDoneFrame(generation=self._provider_generation)
            )
            await self.push_frame(LLMFullResponseEndFrame())
            self._response_create_inflight = False
            self._current_assistant_response = None
            self._response_boundary_open = False
            if self._pending_response_after_tool_result:
                self._pending_response_after_tool_result = False
                logger.info("Qwen starting final spoken response after tool result")
                await self._create_response()
            if self._det_confirm_pending:
                self._det_confirm_pending = False
                logger.info("Qwen deterministic confirmation complete; restoring instructions")
                await self._update_settings()
        elif kind == "error":
            await self._handle_qwen_error_event(event)
        elif "function" in (kind or ""):
            logger.warning("Unhandled Qwen function event: %s", event)

    async def _receive_task_handler(self, websocket=None, generation=None):
        """Map Qwen native events without letting one malformed event kill the socket."""
        websocket = websocket or self._websocket
        generation = self._provider_generation if generation is None else generation
        try:
            async for raw in websocket:
                try:
                    if generation != self._provider_generation:
                        logger.info(
                            "Discarded Qwen event from stale generation=%u current=%u",
                            generation, self._provider_generation,
                        )
                        continue
                    event = json.loads(raw)
                    if not isinstance(event, dict):
                        raise ValueError("server event is not a JSON object")
                    await self._handle_server_event(event)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    preview = raw[:2000] if isinstance(raw, str) else repr(raw)[:2000]
                    logger.exception("Qwen server event processing failed; raw=%s", preview)
                    # Event-level protocol and tool errors are isolated. Keep
                    # reading the healthy WebSocket and return the device idle.
                    await self._reset_failed_response("malformed server event")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if generation != self._provider_generation or self._disconnecting:
                logger.info(
                    "Qwen receive loop ended during expected generation change: old=%u current=%u",
                    generation, self._provider_generation,
                )
                return
            logger.exception("Qwen realtime receive loop died")
            # Socket-level failure is the only case that should enter the
            # existing ConnectionRecovery path.
            await self.push_error(error_msg=f"Qwen realtime receive loop died: {exc!r}")
