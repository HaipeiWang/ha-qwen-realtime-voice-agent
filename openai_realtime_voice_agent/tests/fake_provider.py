"""A deterministic event replay provider for Core tests, never selectable in UI."""

import asyncio

from app.core.events import CanonicalEvent, EventKind


class FakeProvider:
    automatic_responses = True
    def __init__(self):
        self.scope = None
        self.session = None
        self.events = asyncio.Queue()
        self.commands = []

    async def connect(self, scope):
        self.scope = scope
        await self.events.put(CanonicalEvent(EventKind.CONNECTED, scope))

    async def disconnect(self):
        if self.scope:
            await self.events.put(CanonicalEvent(EventKind.CLOSED, self.scope))
        self.scope = None

    async def configure_session(self, session):
        self.session = session
        await self.events.put(CanonicalEvent(EventKind.READY, self.scope))

    def begin_turn(self, scope):
        self.scope = scope

    async def send_audio(self, audio):
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
        self.commands.append(("result", call_id, result))

    async def provide_execution_context(self, result):
        self.commands.append(("context", result))

    async def update_tools(self, tools):
        self.commands.append(("tools", tools))

    async def update_instructions(self, instructions):
        self.commands.append(("instructions", instructions))

    async def receive_events(self):
        while True:
            event = await self.events.get()
            yield event
            if event.kind == EventKind.CLOSED:
                return
