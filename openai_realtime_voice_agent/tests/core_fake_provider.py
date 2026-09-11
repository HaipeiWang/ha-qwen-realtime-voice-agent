"""Deterministic provider used by shared Core integration tests."""

import asyncio

from app.core.events import INPUT_FORMAT


class FakeProvider:
    automatic_responses = True

    def __init__(self):
        self.commands = []
        self.events = asyncio.Queue()
        self.scope = None

    async def connect(self, scope):
        self.scope = scope
        self.commands.append(("connect", scope))

    async def disconnect(self):
        self.commands.append(("disconnect",))
        await self.events.put(None)

    async def configure_session(self, session):
        self.commands.append(("configure", session))

    def begin_turn(self, scope):
        self.scope = scope
        self.commands.append(("begin_turn", scope))

    async def send_audio(self, audio):
        if audio.format != INPUT_FORMAT:
            raise ValueError("Input must be 16 kHz mono PCM16")
        self.commands.append(("audio", audio))

    async def commit_audio(self):
        self.commands.append(("commit",))

    async def clear_input_audio(self):
        self.commands.append(("clear",))

    async def create_response(self, scope):
        self.commands.append(("respond", scope))

    async def cancel_response(self):
        self.commands.append(("cancel",))

    async def send_tool_result(self, call_id, result):
        self.commands.append(("tool_result", call_id, result))

    async def provide_execution_context(self, result):
        self.commands.append(("execution_context", result))

    async def update_tools(self, tools):
        self.commands.append(("tools", tools))

    async def update_instructions(self, instructions):
        self.commands.append(("instructions", instructions))

    async def receive_events(self):
        while (event := await self.events.get()) is not None:
            yield event
