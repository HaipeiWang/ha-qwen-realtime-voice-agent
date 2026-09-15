"""Enrich only the MCP-exposed catalog with HA entity-registry aliases."""
import asyncio
import json
import logging
from dataclasses import replace
from websockets.asyncio.client import connect
from app.control_intent_router import EntityCatalog
from app.tools.ha_facts import exposed_entity_ids


logger = logging.getLogger(__name__)
INITIALIZATION_TIMEOUT = 10.0
DETAIL_TIMEOUT = 2.0
DETAIL_WINDOW = 4


def merge_aliases(catalog, exposed_ids, entries, states):
    names = {s['entity_id']: (s.get('attributes') or {}).get('friendly_name') for s in states
             if s.get('entity_id') in exposed_ids}
    registry = {e['entity_id']: e for e in entries if e.get('entity_id') in exposed_ids}
    result = []
    for entity in catalog.entities:
        matches = []
        for entity_id, friendly in names.items():
            if entity_id.split('.')[0] != entity.domain or not isinstance(friendly, str):
                continue
            entry = registry.get(entity_id, {})
            raw = entry.get('aliases')
            # HA 2026.9 uses null for the computed name. Older HA stores only
            # additional aliases. Compare complete rendered groups, never split
            # on commas: a real entity name may itself contain a comma.
            aliases = tuple(a.strip() for a in (raw or []) if isinstance(a, str) and a.strip())
            current = tuple(friendly if a is None else a.strip() for a in (raw or [])
                            if a is None or isinstance(a, str) and a.strip())
            legacy = tuple(dict.fromkeys((friendly, *aliases)))
            variants = (current, legacy) if None not in (raw or []) else (current,)
            for group in variants:
                if group and (entity.name == ', '.join(group) or
                              entity.name == friendly and None not in (raw or []) and group == legacy):
                    matches.append((entity_id, group))
                    break
            else:
                # Preserve old canonical-name enrichment and fixtures.
                if entity.name == friendly and 'aliases' not in entry:
                    matches.append((entity_id, (friendly,)))
        if len(matches) == 1 and matches[0][0] in registry:
            entity_id, group = matches[0]
            primary = names[entity_id] if names[entity_id] in group else group[0]
            entity = replace(entity, name=primary, entity_id=entity_id,
                             aliases=tuple(a for a in group if a != primary))
        result.append(entity)
    return EntityCatalog(result)


async def _read_details(ws, entity_ids, detailed):
    """One receiver, bounded in-flight requests, independent entity deadlines."""
    loop = asyncio.get_running_loop()
    remaining = iter(entity_ids)
    pending = {}
    request_id = 2
    exhausted = False
    failed = 0
    while pending or not exhausted:
        while len(pending) < DETAIL_WINDOW and not exhausted:
            entity_id = next(remaining, None)
            if entity_id is None:
                exhausted = True
                break
            request_id += 1
            await ws.send(json.dumps({'id': request_id, 'type': 'config/entity_registry/get',
                                      'entity_id': entity_id}))
            pending[request_id] = (entity_id, loop.time() + DETAIL_TIMEOUT)
        if not pending:
            break
        expired = [key for key, (_, deadline) in pending.items() if deadline <= loop.time()]
        for key in expired:
            del pending[key]
            failed += 1
        if expired:
            continue
        timeout = min(deadline for _, deadline in pending.values()) - loop.time()
        try:
            response = json.loads(await asyncio.wait_for(ws.recv(), max(0, timeout)))
        except TimeoutError:
            continue
        # Late replies to timed-out requests must not satisfy a newer request.
        owner = pending.pop(response.get('id'), None)
        if owner is None:
            continue
        entry = response.get('result')
        aliases = entry.get('aliases') if isinstance(entry, dict) else None
        if (response.get('success') is True and isinstance(entry, dict)
                and entry.get('entity_id') == owner[0] and isinstance(aliases, list)
                and all(alias is None or isinstance(alias, str) for alias in aliases)):
            detailed.append(entry)
        else:
            failed += 1
    return failed


async def enrich_aliases(catalog, base_url, token):
    """Best-effort startup enrichment; keep successful results on partial failure.

    The total budget includes exposure discovery, connection and snapshots.
    Cancellation from the caller is deliberately not swallowed.
    """
    if not catalog.entities:
        return catalog
    detailed = []
    states = None
    exposed = set()
    requested = 0
    failed = 0
    failure = None
    url = (base_url.rstrip('/') + '/api/websocket').replace('https://', 'wss://', 1).replace('http://', 'ws://', 1)
    try:
        async with asyncio.timeout(INITIALIZATION_TIMEOUT):
            exposed = await exposed_entity_ids(base_url, token)
            if not exposed:
                return catalog
            async with connect(url, open_timeout=5, close_timeout=1, max_size=8 * 1024 * 1024) as ws:
                if json.loads(await ws.recv()).get('type') != 'auth_required':
                    raise PermissionError('HA authentication protocol unavailable')
                await ws.send(json.dumps({'type': 'auth', 'access_token': token}))
                if json.loads(await ws.recv()).get('type') != 'auth_ok':
                    raise PermissionError('HA authentication failed')
                snapshots = []
                for request_id, command in enumerate(('config/entity_registry/list', 'get_states'), 1):
                    await ws.send(json.dumps({'id': request_id, 'type': command}))
                    response = json.loads(await ws.recv())
                    if (response.get('id') != request_id or response.get('success') is not True
                            or not isinstance(response.get('result'), list)
                            or not all(isinstance(entry, dict) for entry in response['result'])):
                        raise ValueError('HA alias snapshot unavailable')
                    snapshots.append(response['result'])
                states = snapshots[1]
                if any(not isinstance(state.get('entity_id'), str)
                       or not isinstance(state.get('attributes', {}), dict) for state in states):
                    raise ValueError('HA state snapshot malformed')
                domains = {entity.domain for entity in catalog.entities}
                entity_ids = sorted({entry['entity_id'] for entry in snapshots[0]
                                     if isinstance(entry.get('entity_id'), str)
                                     and entry['entity_id'] in exposed
                                     and entry['entity_id'].split('.')[0] in domains})
                requested = len(entity_ids)
                failed = await _read_details(ws, entity_ids, detailed)
    except Exception as error:
        # Never log raw network errors, tokens, entity metadata, or response bodies.
        failure = type(error).__name__
    if failure or failed:
        logger.warning('HA alias enrichment incomplete: requested=%d completed=%d '
                       'unresolved=%d reason=%s; retaining original unresolved names',
                       requested, len(detailed), max(0, requested - len(detailed)),
                       failure or 'entity_details_unavailable')
    if states is None or not detailed:
        return catalog
    return merge_aliases(catalog, exposed, detailed, states)
