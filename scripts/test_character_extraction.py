"""Real disposable PostgreSQL, actual extraction/storage/prompt code, mock models.

Only creates/drops its own random database. Requires a localhost test DSN.
"""
import asyncio
import json
import os
from pathlib import Path
import sys
import uuid
from unittest.mock import AsyncMock, patch
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import asyncpg
import httpx
from character_gateway import database_url_for

BASE = os.environ.get('KIWI_TEST_DATABASE_URL', '')
if not BASE or urlsplit(BASE).hostname not in ('localhost', '127.0.0.1', '::1'):
    raise SystemExit('BLOCKED: set KIWI_TEST_DATABASE_URL to disposable localhost PostgreSQL')

FACTS = ('PROJECT_AUTO_PRIVATE_SENTINEL', 'PROJECT_MANUAL_PRIVATE_SENTINEL', 'UNKNOWN_PRIVATE_SENTINEL')
GLOBAL_FACT = 'GLOBAL_ALLOWED_SENTINEL'
PROJECT = dict(scope_known=True, ledger_project_id='P', context_mode='live_project', context_project_id='P', event_code=None)
GLOBAL = dict(scope_known=True, ledger_project_id=None, context_mode='global', context_project_id=None, event_code=None)


async def run():
    failures, checks = [], []
    def check(condition, label):
        checks.append(label)
        if not condition:
            failures.append(label)
        print(('PASS: ' if condition else 'FAIL: ') + label)

    control = await asyncpg.connect(BASE)
    name = 'acceptance_extraction_' + uuid.uuid4().hex
    await control.execute(f'CREATE DATABASE "{name}" TEMPLATE template0')
    os.environ.update(DATABASE_URL=database_url_for(BASE, name), KIWI_CHARACTER_ID='A', MEMORY_ENABLED='true')
    os.environ.pop('KIWI_WORKER_TOKEN', None)
    try:
        import database, main, character_boundary, config
        await database.init_tables()
        await character_boundary.initialize_character_tables()
        await database.sync_create_project({'id': 'P', 'name': 'P'})
        await config.set_config('extract_interval', '2')
        pool = await database.get_pool()
        captured = []
        async def extract(messages, **kwargs):
            text = json.dumps(messages)
            captured.append(text)
            return [{'content': fact, 'title': fact, 'importance': 8}
                    for fact in (*FACTS, GLOBAL_FACT) if fact in text]

        with patch.object(main, 'extract_memories', extract), \
             patch.object(main, 'get_embedding', AsyncMock(return_value=[1.0, 0.0])), \
             patch.object(database, 'get_embedding', AsyncMock(return_value=[1.0, 0.0])):
            await main.process_memories_background('project-session', FACTS[0], 'ack', 'mock', scope=PROJECT)
            await main.process_memories_background('global-session', '请记住 ' + GLOBAL_FACT, 'ack', 'mock', scope=GLOBAL)
            check(FACTS[0] not in captured[-1] and GLOBAL_FACT in captured[-1], 'automatic extraction receives global material only')
            async with pool.acquire() as conn:
                check(await conn.fetchval('SELECT count(*) FROM memories WHERE content=$1', FACTS[0]) == 0,
                      'automatic extraction cannot promote project text into global memory')
                # Unknown legacy rows stay stored, but are not evidence of global ownership.
                await conn.execute("INSERT INTO conversations(session_id,role,content,model,scope_known) VALUES ('legacy-unknown','user',$1,'mock',FALSE)", FACTS[2])
            await main.process_memories_background('project-session', FACTS[1], 'ack', 'mock', scope=PROJECT)
            snapshot = await database.snapshot_recent_conversation(limit=2)
            check(len(snapshot['rows']) == 2 and set(snapshot['sources']) == {'global-session'},
                  'scope filter precedes limit and retains atomic source snapshots')
            OriginalClient = httpx.AsyncClient
            async with OriginalClient(transport=httpx.ASGITransport(app=main.app), base_url='http://test') as client:
                response = await client.post('/admin/extract-now', json={})
                check(response.status_code == 200 and GLOBAL_FACT in captured[-1] and not any(f in captured[-1] for f in FACTS),
                      'manual extraction excludes project and unknown source rows')
                async with pool.acquire() as conn:
                    check(await conn.fetchval('SELECT count(*) FROM memories WHERE content=ANY($1::text[])', list(FACTS)) == 0,
                          'manual extraction never stores private source facts')
                requests = []
                async def provider(request):
                    requests.append(json.loads(request.content))
                    return httpx.Response(200, json={'choices': [{'message': {'content': 'ack'}, 'finish_reason': 'stop'}]})
                with patch.object(httpx, 'AsyncClient', lambda **kw: OriginalClient(transport=httpx.MockTransport(provider), **kw)), \
                     patch.object(main, 'resolve_provider_for_model', AsyncMock(return_value={
                         'model_id': 'mock', 'api_key': 'mock', 'api_format': 'openai', 'api_base_url': 'http://mock.test', 'provider_name': 'mock'})), \
                     patch.object(main, 'process_memories_background', AsyncMock(return_value={'action': 'skip'})):
                    response = await client.post('/v1/chat/completions', json={
                        'conversation_id': 'global-new-window', 'messages': [{'role': 'user', 'content': 'what do you remember?'}], 'model': 'mock'})
                    await asyncio.sleep(0)
                    text = json.dumps(requests[-1])
                    check(response.status_code == 200 and GLOBAL_FACT in text and not any(f in text for f in FACTS),
                          'new global window receives its fact without private project facts')
            async with pool.acquire() as conn:
                check(await conn.fetchval('SELECT count(*) FROM conversations WHERE content=ANY($1::text[])', list(FACTS)) == 3,
                      'project and legacy raw rows remain unchanged')
            # Legacy single-role readers keep their historical contract.
            with patch.dict(os.environ):
                os.environ.pop('KIWI_CHARACTER_ID')
                rows = await database.get_recent_conversation(limit=50)
                check(all(f in json.dumps([dict(r) for r in rows], default=str) for f in FACTS),
                      'standalone legacy reader remains compatible')
        await database.close_pool()
    finally:
        await control.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        assert await control.fetchval('SELECT count(*) FROM pg_database WHERE datname=$1', name) == 0
        await control.close()
        print('PASS: disposable PostgreSQL database removed')
    assert not failures, failures
    print(f'PASS: {len(checks)} extraction scope guards')


asyncio.run(run())
