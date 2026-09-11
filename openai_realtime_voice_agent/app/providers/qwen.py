"""Native Qwen adapter: transport, session encoding and normalized events only.

No HA request, model-selected function execution or device playback lives here.
The legacy service remains a temporary migration bridge until its controller is
extracted; this class can be tested independently with a fake WebSocket.
"""

import asyncio
import base64
import json
import re
from collections import deque
from dataclasses import asdict, dataclass, field, replace
from typing import AsyncIterator
from urllib.parse import urlencode

from websockets.asyncio.client import connect as websocket_connect

from app.core.events import (
    AudioChunk, CanonicalEvent, ErrorKind, EventKind, EventScope,
    INPUT_FORMAT, OUTPUT_FORMAT, ProviderError, ResponseStatus,
)
from app.providers.base import ProviderSession
from app.providers.qwen_profile import QwenProfile
from app.tools.schema import CanonicalTool, CanonicalToolResult


@dataclass(frozen=True)
class QwenConfig:
    api_key: str = field(repr=False)
    workspace_id: str = field(repr=False)
    region: str = "cn-beijing"
    model: str = "qwen-audio-3.0-realtime-flash"
    voice: str = "longanqian"
    turn_detection: str = "semantic_vad"
    vad_threshold: float | None = None
    vad_silence_duration_ms: int | None = None

    def validate(self):
        if not self.api_key.strip():
            raise ValueError("Qwen API key is missing")
        if not re.fullmatch(r"[A-Za-z0-9-]+", self.workspace_id):
            raise ValueError("Qwen workspace ID is missing or invalid")
        if not re.fullmatch(r"[A-Za-z0-9-]+", self.region):
            raise ValueError("Qwen region is missing or invalid")
        if not self.model.strip() or not self.voice.strip():
            raise ValueError("Qwen model and voice must not be empty")
        if self.turn_detection not in {"semantic_vad", "server_vad"}:
            raise ValueError("Invalid Qwen turn detection setting")

    @property
    def endpoint(self):
        return (
            f"wss://{self.workspace_id}.{self.region}.maas.aliyuncs.com/api-ws/v1/realtime?"
            + urlencode({"model": self.model})
        )


class QwenRealtimeProvider:
    @property
    def automatic_responses(self) -> bool:
        return QwenProfile._is_qwen_audio_realtime_model(self.config.model)

    def __init__(self, config: QwenConfig, *, connector=websocket_connect):
        config.validate()
        self.config = config
        self._connector = connector
        self._socket = None
        self._session = ProviderSession("")
        self._scope = EventScope(0, "")
        self._input_scope = self._scope
        self._input_scopes = {}
        self._response_scopes = {}
        self._call_scopes = {}
        self._pending_responses = deque()
        self._automatic_inputs = deque()
        self._committed_items = set()
        self._arguments = {}
        self._seen_calls = set()
        self._cancelled_responses = set()
        self._cancelled_turns = set()
        self._ended_responses = set()
        self._active_response = None
        self._session_payload = {}

    async def connect(self, scope: EventScope) -> None:
        if self._socket is not None:
            raise RuntimeError("Disconnect the previous provider connection first")
        # Reset only protocol-local correlation. Execution history belongs to Core.
        self._scope = replace(scope, turn_id=None, response_id=None, item_id=None)
        self._input_scope = scope
        self._input_scopes.clear()
        self._response_scopes.clear()
        self._call_scopes.clear()
        self._pending_responses.clear()
        self._automatic_inputs.clear()
        self._committed_items.clear()
        self._arguments.clear()
        self._seen_calls.clear()
        self._cancelled_responses.clear()
        self._cancelled_turns.clear()
        self._ended_responses.clear()
        self._active_response = None
        try:
            self._socket = await self._connector(
                uri=self.config.endpoint,
                additional_headers={"Authorization": f"Bearer {self.config.api_key}"},
                open_timeout=6,
                close_timeout=1.5,
                max_size=4 * 1024 * 1024,
            )
        except Exception:
            raise ConnectionError("Qwen provider connection failed") from None

    async def disconnect(self) -> None:
        socket, self._socket = self._socket, None
        if socket is not None:
            try:
                await asyncio.wait_for(socket.close(), timeout=1.75)
            except TimeoutError:
                transport = getattr(socket, "transport", None)
                if transport:
                    transport.abort()

    async def _send(self, payload) -> None:
        socket = self._socket
        if socket is None:
            raise ConnectionError("Qwen provider is disconnected")
        try:
            await socket.send(json.dumps(payload, ensure_ascii=False))
        except Exception:
            raise ConnectionError("Qwen provider send failed") from None

    @staticmethod
    def _tool_schema(tool: CanonicalTool):
        return QwenProfile._to_qwen_tool({
            "type": "function", "name": tool.name,
            "description": tool.description, "parameters": tool.parameters,
        })

    def session_payload(self, session: ProviderSession) -> dict:
        audio_model = QwenProfile._is_qwen_audio_realtime_model(self.config.model)
        turn = {"type": "smart_turn"} if audio_model else {
            "type": self.config.turn_detection, "create_response": False,
            "interrupt_response": False,
        }
        if not audio_model:
            if self.config.vad_threshold is not None:
                turn["threshold"] = self.config.vad_threshold
            if self.config.vad_silence_duration_ms is not None:
                turn["silence_duration_ms"] = self.config.vad_silence_duration_ms
        payload = {
            "modalities": ["text", "audio"],
            "instructions": session.instructions,
            "voice": QwenProfile._validated_voice_for_model(self.config.model, self.config.voice),
            "turn_detection": turn,
            "tools": [self._tool_schema(tool) for tool in session.tools],
        }
        if audio_model:
            payload.update(input_audio_format="pcm", output_audio_format="pcm", max_history_turns=50)
        else:
            payload["audio"] = {
                "input": {"format": {"type": "pcm", "sample_rate": INPUT_FORMAT.sample_rate}},
                "output": {"format": {"type": "pcm", "sample_rate": OUTPUT_FORMAT.sample_rate}},
            }
            payload["input_audio_transcription"] = {"model": "qwen3-asr-flash-realtime"}
        if session.max_output_tokens:
            payload["max_tokens"] = session.max_output_tokens
        return payload

    async def configure_session(self, session: ProviderSession) -> None:
        self._session = session
        self._session_payload = self.session_payload(session)
        await self._send({"type": "session.update", "session": self._session_payload})

    def begin_turn(self, scope: EventScope) -> None:
        if (scope.generation, scope.conversation_id) != (
            self._scope.generation, self._scope.conversation_id
        ):
            raise ValueError("Input turn belongs to a different provider connection")
        self._input_scope = scope

    async def send_audio(self, audio: AudioChunk) -> None:
        if audio.format != INPUT_FORMAT:
            raise ValueError("Qwen input requires mono PCM16 at 16000 Hz")
        await self._send({
            "type": "input_audio_buffer.append",
            "audio": base64.b64encode(audio.data).decode("ascii"),
        })

    async def commit_audio(self) -> None:
        await self._send({"type": "input_audio_buffer.commit"})

    async def clear_input_audio(self) -> None:
        await self._send({"type": "input_audio_buffer.clear"})

    async def create_response(self, scope: EventScope) -> None:
        if (scope.generation, scope.conversation_id) != (
            self._scope.generation, self._scope.conversation_id
        ):
            raise ValueError("Response belongs to an old connection")
        self._pending_responses.append(scope)
        try:
            await self._send({"type": "response.create"})
        except BaseException:
            if scope in self._pending_responses:
                self._pending_responses.remove(scope)
            raise

    async def cancel_response(self) -> None:
        if self._active_response:
            self._cancelled_responses.add(self._active_response)
        self._cancelled_turns.update(scope.turn_id for scope in self._pending_responses)
        if not self._active_response:
            self._cancelled_turns.update(scope.turn_id for scope in self._automatic_inputs)
        await self._send({"type": "response.cancel"})

    async def send_tool_result(self, call_id: str, result: CanonicalToolResult) -> None:
        if call_id not in self._call_scopes:
            raise ValueError("Cannot return a result without a provider-owned tool call")
        owner = self._call_scopes[call_id]
        if owner.response_id in self._cancelled_responses or owner.turn_id in self._cancelled_turns:
            raise ValueError("Cannot return a result into a cancelled provider response")
        await self._send({"type": "conversation.item.create", "item": {
            "type": "function_call_output", "call_id": call_id,
            "output": json.dumps(asdict(result), ensure_ascii=False),
        }})

    async def provide_execution_context(self, result: CanonicalToolResult) -> None:
        # Router calls have no provider call_id. Use instruction context rather
        # than inventing a function_call_output that the cloud would reject.
        note = "\n本轮本地工具执行结果（仅据此简短如实确认）：" + json.dumps(asdict(result), ensure_ascii=False)
        await self._send({"type": "session.update", "session": {
            "instructions": self._session.instructions + note,
        }})

    async def update_tools(self, tools: tuple[CanonicalTool, ...]) -> None:
        await self.configure_session(replace(self._session, tools=tools))

    async def update_instructions(self, instructions: str) -> None:
        await self._send({"type": "session.update", "session": {"instructions": instructions}})

    def _response_scope(self, raw):
        response_id = raw.get("response_id")
        if not response_id:
            return None
        return self._response_scopes.get(response_id)

    def _error(self, code="invalid_event", kind=ErrorKind.PROTOCOL):
        # Do not forward raw server errors: they may echo headers or transcripts.
        return CanonicalEvent(EventKind.ERROR, self._scope, error=ProviderError(
            kind, code, "Realtime provider event could not be processed",
        ))

    def _tool_event(self, raw, scope):
        call_id, name = raw.get("call_id"), raw.get("name")
        if not scope or not call_id or not name:
            raise ValueError("Unassociated tool call")
        if call_id in self._seen_calls or scope.response_id in self._cancelled_responses:
            return []
        arguments = raw.get("arguments", self._arguments.get(call_id, "{}"))
        if not isinstance(arguments, str) or len(arguments) > 65536:
            raise ValueError("Invalid tool arguments")
        parsed = json.loads(arguments)
        if not isinstance(parsed, dict):
            raise ValueError("Tool arguments must be an object")
        self._seen_calls.add(call_id)
        self._arguments.pop(call_id, None)
        self._call_scopes[call_id] = scope
        return [CanonicalEvent(EventKind.TOOL_CALL, scope, call_id=call_id,
                               tool_name=name, arguments=parsed)]

    def parse_event(self, raw: dict) -> list[CanonicalEvent]:
        """Decode a single event; exposed for deterministic protocol fixtures."""
        try:
            return self._parse_event(raw)
        except (ValueError, TypeError, KeyError, AttributeError):
            return [self._error()]

    def _parse_event(self, raw):
        kind = raw.get("type")
        if not kind and "code" in raw:
            raw = {"type": "error", "error": raw}
            kind = "error"
        if kind == "session.created":
            return [CanonicalEvent(EventKind.CONNECTED, self._scope)]
        if kind == "session.updated":
            tools = (raw.get("session") or {}).get("tools", [])
            expected = [t.name for t in self._session.tools]
            names = [tool["function"]["name"] for tool in tools]
            valid = len(names) == len(set(names)) and sorted(names) == sorted(expected)
            valid = valid and all(
                tool.get("type") == "function"
                and tool["function"].get("parameters", {}).get("type") == "object"
                for tool in tools
            )
            return [CanonicalEvent(EventKind.READY, self._scope, tool_names=tuple(names), tools_valid=valid)]
        if kind == "input_audio_buffer.speech_started":
            item_id = raw.get("item_id")
            if not item_id:
                raise ValueError("Missing input item association")
            scope = replace(self._input_scope, item_id=item_id)
            self._input_scopes.setdefault(item_id, scope)
            return [CanonicalEvent(EventKind.SPEECH_STARTED, self._input_scopes[item_id])]
        input_kinds = {
            "input_audio_buffer.speech_stopped": EventKind.SPEECH_STOPPED,
            "conversation.item.input_audio_transcription.completed": EventKind.TRANSCRIPT_FINAL,
            "conversation.item.input_audio_transcription.failed": EventKind.TRANSCRIPT_FAILED,
        }
        if kind in input_kinds:
            scope = self._input_scopes.get(raw.get("item_id"))
            if not scope:
                return []
            if kind == "input_audio_buffer.speech_stopped" and scope.item_id not in self._committed_items:
                self._automatic_inputs.append(scope)
                self._committed_items.add(scope.item_id)
            return [CanonicalEvent(input_kinds[kind], scope, text=raw.get("transcript", ""))]
        if kind == "response.created":
            response = raw.get("response") or {}
            response_id = response.get("id") or raw.get("response_id")
            if not response_id:
                raise ValueError("Missing response identity")
            if response_id in self._response_scopes:
                return []
            # Explicit and automatic requests keep the input owner captured at
            # their boundary, never the current turn at event-delivery time.
            if self._pending_responses:
                owner = self._pending_responses.popleft()
                self._automatic_inputs = deque(
                    item for item in self._automatic_inputs if item.turn_id != owner.turn_id
                )
            else:
                owner = self._automatic_inputs.popleft() if self._automatic_inputs else self._scope
            scope = replace(owner, response_id=response_id, item_id=None)
            self._response_scopes[response_id] = scope
            if scope.turn_id in self._cancelled_turns:
                self._cancelled_responses.add(response_id)
            self._active_response = response_id
            return [CanonicalEvent(EventKind.RESPONSE_STARTED, scope)]
        scope = self._response_scope(raw)
        response_id = raw.get("response_id")
        if kind == "response.function_call_arguments.delta":
            call_id = raw.get("call_id")
            if scope and call_id and response_id not in self._cancelled_responses and response_id not in self._ended_responses:
                value = self._arguments.get(call_id, "") + raw.get("delta", "")
                if len(value) > 65536:
                    self._arguments.pop(call_id, None)
                    raise ValueError("Tool argument limit exceeded")
                self._arguments[call_id] = value
            return []
        if kind == "response.function_call_arguments.done":
            return self._tool_event(raw, scope)
        if kind == "response.output_item.done":
            item = raw.get("item") or {}
            return self._tool_event(item, scope) if item.get("type") == "function_call" else []
        if kind == "response.done":
            response = raw.get("response") or {}
            response_id = response.get("id") or response_id
            scope = self._response_scopes.get(response_id)
            if not scope:
                return []
            if response_id in self._ended_responses:
                return []
            status = ResponseStatus(response.get("status", "failed"))
            if response_id in self._cancelled_responses:
                status = ResponseStatus.CANCELLED
            events = []
            if status == ResponseStatus.COMPLETED:
                for item in response.get("output", []):
                    if item.get("type") == "function_call":
                        events.extend(self._tool_event(item, scope))
            events.append(CanonicalEvent(EventKind.RESPONSE_ENDED, scope, status=status))
            self._ended_responses.add(response_id)
            if self._active_response == response_id:
                self._active_response = None
            return events
        if kind == "error":
            code = str((raw.get("error") or {}).get("code", ""))
            message = str((raw.get("error") or {}).get("message", "")).lower()
            if code in {"conversation_already_has_active_response", "response_cancel_not_active", "input_audio_buffer_commit_empty"}:
                return []  # Benign protocol races do not kill a healthy turn.
            if code in {"invalid_api_key", "401"}:
                return [self._error("authentication_failed", ErrorKind.AUTHENTICATION)]
            if code in {"rate_limit_exceeded", "429", "insufficient_quota"} or "quota" in message:
                return [self._error("rate_limit", ErrorKind.RATE_LIMIT)]
            if code in {"user_idle_timeout", "session_expired", "session_closed"} or "session is closed" in message:
                return [self._error("session_closed", ErrorKind.CONNECTION)]
            return [self._error("provider_error", ErrorKind.INTERNAL)]
        if not scope or response_id in self._cancelled_responses or response_id in self._ended_responses:
            return []
        if kind == "response.audio.delta":
            data = base64.b64decode(raw.get("delta", ""), validate=True)
            return [CanonicalEvent(EventKind.AUDIO, scope, audio=AudioChunk(data, OUTPUT_FORMAT))]
        if kind == "response.audio.done":
            return [CanonicalEvent(EventKind.AUDIO_ENDED, scope)]
        if kind in {"response.audio_transcript.delta", "response.text.delta"}:
            return [CanonicalEvent(EventKind.TEXT_DELTA, scope, text=raw.get("delta", ""),
                                   text_is_spoken=kind == "response.audio_transcript.delta")]
        if kind in {"response.audio_transcript.done", "response.text.done"}:
            return [CanonicalEvent(EventKind.TEXT_FINAL, scope,
                                   text=raw.get("transcript", raw.get("text", "")),
                                   text_is_spoken=kind == "response.audio_transcript.done")]
        return []

    async def receive_events(self) -> AsyncIterator[CanonicalEvent]:
        socket, scope = self._socket, self._scope
        if socket is None:
            raise ConnectionError("Qwen provider is disconnected")
        try:
            async for message in socket:
                if socket is not self._socket:
                    return
                try:
                    decoded = json.loads(message)
                except (ValueError, TypeError):
                    yield self._error()
                    continue
                for event in self.parse_event(decoded):
                    yield event
        except asyncio.CancelledError:
            raise
        except Exception:
            if socket is self._socket:
                yield self._error("connection_closed", ErrorKind.CONNECTION)
        # Do not yield from a finally block: cancellation/async-generator close
        # must terminate immediately without fabricating a normal close event.
        if socket is self._socket:
            self._socket = None
            yield CanonicalEvent(EventKind.CLOSED, scope)
