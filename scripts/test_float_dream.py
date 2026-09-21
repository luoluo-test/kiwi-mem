"""Disposable real PostgreSQL adapter/Dream guards; embeddings and Dream model are fixtures."""
import asyncio
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import sys
import uuid
from unittest.mock import AsyncMock, patch
from urllib.parse import urlsplit, urlunsplit
import asyncpg
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

async def run():
    base = os.environ.get('KIWI_TEST_DATABASE_URL', '')
    parts = urlsplit(base)
    if parts.hostname not in ('127.0.0.1', 'localhost', '::1'):
        raise SystemExit('BLOCKED: disposable localhost KIWI_TEST_DATABASE_URL required')
    control = await asyncpg.connect(base)
    name = 'acceptance_float_dream_' + uuid.uuid4().hex
    await control.execute(f'CREATE DATABASE "{name}" TEMPLATE template0')
    dsn = urlunsplit(parts._replace(path='/' + name))
    os.environ.update(DATABASE_URL=dsn, KIWI_CHARACTER_ID='test', MEMORY_ENABLED='true',
        API_KEY='', MEMORY_API_KEY='', API_BASE_URL='http://127.0.0.1:9/mock', MEMORY_API_BASE_URL='http://127.0.0.1:9/mock')
    import database, config, dream, float_memory_api as adapter
    errors=[]
    try:
        await database.init_tables()
        await adapter.initialize()
        await adapter.initialize()
        with patch.object(database, 'get_embedding', AsyncMock(return_value=None)), patch.object(config, 'get_config_bool', AsyncMock(return_value=True)):
            ids=[]
            for i in range(6):
                event=adapter.Event(event_id=f'g-{i}', session_id='group', message_id=f'm-{i}', source='chat:group', occurred_at='2026-01-01T00:00:00Z', content=f'SHARED_{i}', visibility='group')
                ids.append((await adapter.ingest(event))['memory_id'])
            stats=dict(memories_merged=0, memories_deleted=0)
            result=await dream._execute_dream_action(dict(type='merge', memory_ids=ids[:2], merged_content='MERGED'), 1, stats)
            recalled=await adapter.recall(group_session_id='group')
            try:
                assert len(recalled['memories'])==6, 'F3: group originals must survive Dream merge'
                assert result.get('skipped'), 'F3: group action must be explicitly skipped'
                assert not ({m['id'] for m in await database.get_unprocessed_memories()} & set(ids)), 'F3: group memories excluded from Dream candidates'
                assert not await database.soften_memory(ids[2], 'SHORT'), 'F3: autonomous soften must preserve group originals'
                await dream._execute_dream_action(dict(type='delete', memory_ids=[ids[3]]),1,stats)
                assert len((await adapter.recall(group_session_id='group'))['memories'])==6
                await database.update_memory(ids[4], content='USER_EDIT')
                assert any(m['content']=='USER_EDIT' for m in (await adapter.recall(group_session_id='group'))['memories'])
                await database.delete_memory(ids[5])
                assert len((await adapter.recall(group_session_id='group'))['memories'])==5
                print('PASS F3: real DB group originals protected from Dream/soften; explicit edit/delete retained')
            except AssertionError as e: errors.append(str(e)); print('FAIL', e)
            private_ids=[]
            for i in range(6):
                private_ids.append((await adapter.ingest(adapter.Event(event_id=f'p-{i}',session_id='private',message_id=f'p-{i}',source='chat:direct',occurred_at='2026-01-01T00:00:00Z',content=f'PRIVATE_{i}',visibility='private')))['memory_id'])
            pool=await database.get_pool()
            async with pool.acquire() as conn:
                await conn.execute("UPDATE memories SET dream_processed_at=now(), created_at=now()-interval '30 days' WHERE id=ANY($1::int[])", ids + private_ids[-1:])
                await conn.execute("UPDATE float_memory_events SET created_at=now()-interval '48 hours'")
                assert await conn.fetchval('SELECT count(*) FROM chat_messages')==0
            aging=await database.get_aging_memories(limit=1)
            assert len(aging)==1 and aging[0]['id']==private_ids[-1], 'group rows must not starve private aging candidates'
            calls=[]
            class Noon(datetime):
                @classmethod
                def now(cls,tz=None): return datetime.now(tz).replace(hour=12)
            async def fixture_dream(**kwargs):
                calls.append(kwargs)
                yield {'type':'complete','data':'fixture'}
            with patch.object(dream,'datetime',Noon),patch.object(config,'get_config',AsyncMock(return_value=None)),patch.object(dream,'run_dream',fixture_dream):
                try:
                    assert await dream.auto_dream_check(), 'F7: float-only idle activity must permit auto Dream'
                    assert len(calls)==1
                    async with pool.acquire() as conn:
                        await conn.execute("UPDATE float_memory_events SET created_at=now()+interval '1 hour'")
                    assert not await dream.auto_dream_check(), 'F7: recent activity must defer auto Dream'
                    assert len(calls)==1
                    print('PASS F7: no chat rows, idle float activity triggers, recent activity defers (Dream model fixture)')
                except AssertionError as e: errors.append(str(e)); print('FAIL',e)
        if errors: raise AssertionError('; '.join(errors))
    finally:
        await database.close_pool()
        await control.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        assert not await control.fetchval('SELECT 1 FROM pg_database WHERE datname=$1',name)
        await control.close()
        print('PASS: SQL verified disposable Dream database cleanup')

if __name__=='__main__': asyncio.run(run())
