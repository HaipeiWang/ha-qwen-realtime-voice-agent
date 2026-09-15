"""Startup fault injection with a real async queue and out-of-order replies."""
import asyncio
import json
import unittest
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

from app.control_intent_router import EntityCatalog, EntityInfo
from app.tools import ha_aliases as module


class Socket:
    def __init__(self, count, behavior=None):
        self.count = count
        self.behavior = behavior or {}
        self.queue = asyncio.Queue()
        self.queue.put_nowait({'type': 'auth_required'})
        self.tasks = []
        self.requests = []
        self.closed = False
        self.max_pending = 0
        self.pending = set()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    async def send(self, value):
        request = json.loads(value)
        kind = request['type']
        if kind == 'auth':
            self.queue.put_nowait({'type': 'auth_ok'})
            return
        if kind == 'config/entity_registry/list':
            result = [{'entity_id': f'light.e{i}'} for i in range(self.count)]
        elif kind == 'get_states':
            result = [{'entity_id': f'light.e{i}', 'attributes': {'friendly_name': f'灯{i}'}}
                      for i in range(self.count)]
        else:
            eid = request['entity_id']
            self.requests.append(eid)
            self.pending.add(request['id'])
            self.max_pending = max(self.max_pending, len(self.pending))
            delay, status = self.behavior.get(eid, (0, 'ok'))
            async def reply():
                await asyncio.sleep(delay)
                self.pending.discard(request['id'])
                if status == 'disconnect':
                    self.queue.put_nowait(ConnectionError('private details must not appear in logs'))
                    return
                if status == 'malformed':
                    result = {'entity_id': eid, 'aliases': 'invalid'}
                else:
                    result = {'entity_id': eid, 'aliases': [None, '别名' + eid[7:]]}
                self.queue.put_nowait({'id': request['id'], 'success': status != 'deleted',
                                      'result': result})
            self.tasks.append(asyncio.create_task(reply()))
            return
        self.queue.put_nowait({'id': request['id'], 'success': True, 'result': result})

    async def recv(self):
        result = await self.queue.get()
        if isinstance(result, Exception):
            raise result
        return json.dumps(result)


class InitializationTests(unittest.IsolatedAsyncioTestCase):
    async def run_probe(self, count=4, behavior=None, *, total=1, detail=.1, exposed=None):
        catalog = EntityCatalog([EntityInfo(f'灯{i}, 别名{i}', 'light') for i in range(count)])
        socket = Socket(count, behavior)
        with ExitStack() as stack:
            stack.enter_context(patch.object(module, 'connect', return_value=socket))
            stack.enter_context(patch.object(module, 'exposed_entity_ids', AsyncMock(
                return_value=exposed if exposed is not None else {f'light.e{i}' for i in range(count)})))
            stack.enter_context(patch.object(module, 'INITIALIZATION_TIMEOUT', total))
            stack.enter_context(patch.object(module, 'DETAIL_TIMEOUT', detail))
            result = await module.enrich_aliases(catalog, 'http://ha', 'secret')
        return result, socket, catalog

    async def test_many_entities_with_out_of_order_replies_and_bounded_window(self):
        result, socket, _ = await self.run_probe(30, {f'light.e{i}': ((i % 4) * .002, 'ok') for i in range(30)})
        self.assertEqual([e.name for e in result.entities], [f'灯{i}' for i in range(30)])
        self.assertLessEqual(socket.max_pending, module.DETAIL_WINDOW)
        self.assertTrue(socket.closed)

    async def test_deleted_entity_does_not_discard_other_aliases(self):
        result, _, original = await self.run_probe(behavior={'light.e1': (0, 'deleted')})
        self.assertEqual(result.entities[1], original.entities[1])
        self.assertEqual(result.entities[2].aliases, ('别名2',))

    async def test_malformed_detail_is_isolated(self):
        result, _, original = await self.run_probe(behavior={'light.e1': (0, 'malformed')})
        self.assertEqual(result.entities[1], original.entities[1])
        self.assertEqual(result.entities[0].name, '灯0')

    async def test_slow_entity_does_not_block_later_entities(self):
        result, socket, original = await self.run_probe(8, {'light.e0': (.2, 'ok')}, detail=.02)
        self.assertEqual(result.entities[0], original.entities[0])
        self.assertEqual(result.entities[7].name, '灯7')
        self.assertEqual(len(socket.requests), 8)

    async def test_late_reply_cannot_be_assigned_to_new_request(self):
        result, _, original = await self.run_probe(8, {
            'light.e0': (.03, 'ok'), 'light.e4': (.04, 'ok'), 'light.e5': (.04, 'ok'),
            'light.e6': (.04, 'ok'), 'light.e7': (.04, 'ok')}, detail=.02)
        self.assertEqual(result.entities[0], original.entities[0])
        self.assertEqual(result.entities[4], original.entities[4])
        self.assertEqual(result.entities[1].entity_id, 'light.e1')

    async def test_global_deadline_keeps_completed_results(self):
        result, socket, original = await self.run_probe(4, {'light.e1': (.2, 'ok')}, total=.03)
        self.assertEqual(result.entities[1], original.entities[1])
        self.assertEqual(result.entities[0].name, '灯0')
        self.assertTrue(socket.closed)

    async def test_disconnect_keeps_completed_results_without_sensitive_logging(self):
        with self.assertLogs(module.logger, 'WARNING') as logs:
            result, _, original = await self.run_probe(behavior={'light.e1': (.01, 'disconnect')})
        self.assertEqual(result.entities[1], original.entities[1])
        self.assertEqual(result.entities[0].name, '灯0')
        self.assertNotIn('private details', ''.join(logs.output))

    async def test_exposure_timeout_returns_original_without_connecting(self):
        catalog = EntityCatalog([EntityInfo('灯', 'light')])
        async def stalled(*args):
            await asyncio.sleep(10)
        with patch.object(module, 'exposed_entity_ids', stalled), patch.object(module, 'INITIALIZATION_TIMEOUT', .01), patch.object(module, 'connect') as connect:
            self.assertIs(await module.enrich_aliases(catalog, 'http://ha', 'secret'), catalog)
            connect.assert_not_called()

    async def test_caller_cancellation_propagates_and_closes_socket(self):
        socket = Socket(1, {'light.e0': (10, 'ok')})
        with patch.object(module, 'connect', return_value=socket), patch.object(module, 'exposed_entity_ids', AsyncMock(return_value={'light.e0'})):
            task = asyncio.create_task(module.enrich_aliases(EntityCatalog([EntityInfo('灯0', 'light')]), 'http://ha', 'secret'))
            while not socket.requests:
                await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(socket.closed)

    async def test_unexposed_entities_never_queried_or_enriched(self):
        result, socket, original = await self.run_probe(exposed={'light.e0'})
        self.assertEqual(socket.requests, ['light.e0'])
        self.assertEqual(result.entities[1:], original.entities[1:])

    async def test_authentication_failure_returns_original(self):
        socket = Socket(1)
        socket.queue.get_nowait()
        socket.queue.put_nowait({'type': 'auth_invalid'})
        catalog = EntityCatalog([EntityInfo('灯0', 'light')])
        with patch.object(module, 'connect', return_value=socket), patch.object(module, 'exposed_entity_ids', AsyncMock(return_value={'light.e0'})):
            self.assertIs(await module.enrich_aliases(catalog, 'http://ha', 'secret'), catalog)
        self.assertFalse(socket.requests)
        self.assertTrue(socket.closed)

    async def test_connection_failure_returns_original(self):
        catalog = EntityCatalog([EntityInfo('灯0', 'light')])
        with patch.object(module, 'connect', side_effect=OSError('offline')), patch.object(module, 'exposed_entity_ids', AsyncMock(return_value={'light.e0'})):
            self.assertIs(await module.enrich_aliases(catalog, 'http://ha', 'secret'), catalog)

    async def test_empty_catalog_does_not_access_network(self):
        catalog = EntityCatalog([])
        with patch.object(module, 'exposed_entity_ids', AsyncMock()) as expose:
            self.assertIs(await module.enrich_aliases(catalog, 'http://ha', 'secret'), catalog)
            expose.assert_not_awaited()
