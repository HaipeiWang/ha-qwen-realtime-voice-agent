"""Enrich only the MCP-exposed catalog with HA entity-registry aliases."""
import asyncio
import json
from dataclasses import replace
from collections import Counter
from websockets.asyncio.client import connect
from app.control_intent_router import EntityCatalog
from app.tools.ha_facts import exposed_entity_ids


def merge_aliases(catalog, exposed_ids, entries, states):
    names = {s['entity_id']: (s.get('attributes') or {}).get('friendly_name') for s in states
             if s.get('entity_id') in exposed_ids}
    counts = Counter((entity_id.split('.')[0], name) for entity_id, name in names.items())
    registry = {e['entity_id']: e for e in entries if e.get('entity_id') in exposed_ids}
    result = []
    for entity in catalog.entities:
        matches = [i for i, n in names.items() if n == entity.name and i.split('.')[0] == entity.domain]
        if len(matches) == 1 and counts[(entity.domain, entity.name)] == 1:
            entity_id = matches[0]
            aliases = tuple(a.strip() for a in registry.get(entity_id, {}).get('aliases', [])
                            if isinstance(a, str) and a.strip())
            entity = replace(entity, entity_id=entity_id, aliases=aliases)
        result.append(entity)
    return EntityCatalog(result)


async def enrich_aliases(catalog, base_url, token):
    exposed = await exposed_entity_ids(base_url, token)
    url = (base_url.rstrip('/') + '/api/websocket').replace('https://', 'wss://', 1).replace('http://', 'ws://', 1)
    async with asyncio.timeout(10):
        async with connect(url, open_timeout=5, close_timeout=1, max_size=8 * 1024 * 1024) as ws:
            if json.loads(await ws.recv()).get('type') != 'auth_required':
                raise PermissionError('HA authentication protocol unavailable')
            await ws.send(json.dumps({'type': 'auth', 'access_token': token}))
            if json.loads(await ws.recv()).get('type') != 'auth_ok':
                raise PermissionError('HA authentication failed')
            results = []
            for request_id, command in enumerate(('config/entity_registry/list', 'get_states'), 1):
                await ws.send(json.dumps({'id': request_id, 'type': command}))
                response = json.loads(await ws.recv())
                if response.get('id') != request_id or response.get('success') is not True:
                    raise PermissionError('HA alias metadata unavailable')
                results.append(response['result'])
    return merge_aliases(catalog, exposed, *results)
