"""Project scope regressions. Creates/drops only an acceptance_review_* database.
Run with existing temporary PostgreSQL and KIWI_TEST_DATABASE_URL pointing at localhost.
Uses real PostgreSQL/API/functions; profile model is mock. No repository modifications.
"""
import asyncio, json, os, sys, uuid
from datetime import datetime, timezone
from unittest.mock import patch, AsyncMock
from urllib.parse import urlsplit
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import asyncpg, httpx
BASE = os.environ.get('KIWI_TEST_DATABASE_URL', '')
if not BASE or urlsplit(BASE).hostname not in ('localhost', '127.0.0.1', '::1'):
    raise SystemExit('BLOCKED: explicitly set KIWI_TEST_DATABASE_URL to local disposable PostgreSQL')
async def run():
    control = await asyncpg.connect(BASE)
    name = 'acceptance_review_' + uuid.uuid4().hex
    await control.execute(f'CREATE DATABASE "{name}" TEMPLATE template0')
    from character_gateway import database_url_for
    os.environ['DATABASE_URL'] = database_url_for(BASE, name)
    os.environ['KIWI_CHARACTER_ID'] = 'review-A'
    os.environ['MEMORY_ENABLED'] = 'true'
    os.environ.pop('KIWI_WORKER_TOKEN', None)
    try:
        import database, main, character_boundary, tool_drawer, config, daily_digest
        await database.init_tables()
        await character_boundary.initialize_character_tables()
        OriginalClient = httpx.AsyncClient
        async with OriginalClient(transport=httpx.ASGITransport(app=main.app), base_url='http://test') as client:
            response = await client.post('/sync/projects', json={'id': 'P', 'name': 'P'})
            assert response.status_code == 200
            response = await client.post('/sync/conversations', json={'id': 'bound-session', 'projectId': 'P'})
            assert response.status_code == 200
            response = await client.put('/sync/conversations/bound-session/messages/m1', json={
                'role': 'user', 'content': 'BOUND_PROJECT_SECRET', 'time': datetime.now(timezone.utc).isoformat()})
            assert response.status_code == 200
            assert await character_boundary.bind_session_project('bound-session', 'P')
            before = await database.search_chat_messages('BOUND_PROJECT_SECRET', project_id='none')
            print('GLOBAL_SEARCH_BEFORE_PATCH', 'BOUND_PROJECT_SECRET' in json.dumps(before, default=str))
            response = await client.patch('/sync/conversations/bound-session', json={'projectId': None})
            assert response.status_code == 409, response.text
            assert response.json()['code'] == 'session_project_mismatch'
            after = await database.search_chat_messages('BOUND_PROJECT_SECRET', project_id='none')
            assert 'BOUND_PROJECT_SECRET' not in json.dumps(after, default=str)
            print('PASS: PATCH cannot expose project history globally')
            assert not await character_boundary.bind_session_project('bound-session', None)
            pool = await database.get_pool()
            async with pool.acquire() as conn:
                print('BINDING_VS_METADATA', dict(await conn.fetchrow(
                    'SELECT b.project_id AS binding,c.project_id AS metadata FROM kiwi_character_sessions b '
                    'JOIN chat_conversations c ON c.id=b.session_id WHERE b.session_id=$1', 'bound-session')))
            for method, body in [('PUT', {'projectId':None, 'messages':[]}), ('PATCH', {'project_id':'missing'})]:
                response = await client.request(method, '/sync/conversations/bound-session', json=body)
                assert response.status_code == 409, response.text
            response = await client.patch('/sync/conversations/bound-session', json={'title':'renamed'})
            assert response.status_code == 200, response.text
            response = await client.post('/sync/conversations', json={'id':'missing-project-session', 'projectId':'missing'})
            assert response.status_code == 409, response.text
            # Same transaction prevents imported metadata and messages from crossing scope.
            for restore in (False, True):
                async with pool.acquire() as conn:
                    try:
                        async with conn.transaction():
                            await database._upsert_conversation_tx(conn, {'id':'bound-session', 'projectId':None}, restore=restore)
                    except character_boundary.CharacterScopeError:
                        pass
                    else:
                        raise AssertionError('import/restore bypassed binding')
            imported = await client.post('/sync/import', json={'conversations':[{'id':'bound-session','projectId':None,'messages':[]}], 'projects':[]})
            assert imported.status_code == 200, imported.text
            payload = imported.json()
            assert payload['counts']['conversations']['rejected'] == 1, payload
            assert payload['rejected_details'][0]['code'] == 'session_project_mismatch', payload
            import io, zipfile
            def backup(project):
                buffer = io.BytesIO()
                with zipfile.ZipFile(buffer,'w') as archive:
                    archive.writestr('character.json', json.dumps({'character_id':'review-A'}))
                    archive.writestr('conversations.json', json.dumps([{'id':'bound-session','projectId':project,'messages':[{'id':'restored-msg','role':'user','content':'RESTORED_PROJECT_SECRET'}]}]))
                return buffer.getvalue()
            restored = await client.post('/sync/import-backup', files={'file':('backup.zip',backup(None),'application/zip')})
            assert restored.json()['failed_conversations'] == 1, restored.text
            assert restored.json()['scope_errors'] == ['session_project_mismatch'], restored.text
            await client.delete('/sync/conversations/bound-session')
            restored = await client.post('/sync/import-backup', files={'file':('backup.zip',backup('P'),'application/zip')})
            assert restored.json()['conversations'] == 1 and restored.json()['failed_conversations'] == 0, restored.text
            assert 'RESTORED_PROJECT_SECRET' not in json.dumps(await database.search_chat_messages('RESTORED_PROJECT_SECRET', project_id='none'))
            assert 'RESTORED_PROJECT_SECRET' in json.dumps(await database.search_chat_messages('RESTORED_PROJECT_SECRET', project_id='P'),default=str)
            print('PASS: JSON import refuses scope change; ZIP restore retains project and reports rejected scope')
            results = await asyncio.gather(character_boundary.bind_session_project('race', 'P'),
                                           character_boundary.bind_session_project('race', None))
            assert sum(results) == 1, results
            await character_boundary.initialize_character_tables()
            assert await character_boundary.bind_session_project('bound-session', 'P')
            async with pool.acquire() as conn:
                await conn.execute("UPDATE chat_conversations SET project_id=NULL WHERE id='bound-session'")
            try:
                await character_boundary.initialize_character_tables()
            except RuntimeError:
                pass
            else:
                raise AssertionError('inconsistent pre-fix metadata accepted at startup')
            async with pool.acquire() as conn:
                assert await conn.fetchval("SELECT project_id IS NULL FROM chat_conversations WHERE id='bound-session'")
                await conn.execute("UPDATE chat_conversations SET project_id='P' WHERE id='bound-session'")
            await character_boundary.initialize_character_tables()
            print('PASS: PUT/import/restore reject changed ownership; concurrent first binding and repeated migration safe')
            with patch.object(httpx, 'AsyncClient', lambda **kw: OriginalClient(transport=httpx.ASGITransport(app=main.app), **kw)):
                tool_drawer._auto_discover_mcp_tools()
                result, _ = await tool_drawer.execute_drawer_tool('save_calendar_page', {
                    'date': '2026-09-17', 'content': 'PROJECT_P_CALENDAR_SECRET'},
                    scope={'context_mode': 'live_project', 'context_project_id': 'P'})
                assert 'project_global_write_forbidden' in result, result
                result, _ = await tool_drawer.execute_drawer_tool('get_day_page', {'date': '2026-09-17'},
                    scope={'context_mode': 'global', 'context_project_id': None})
                assert 'PROJECT_P_CALENDAR_SECRET' not in result
                result, _ = await tool_drawer.execute_drawer_tool('save_calendar_page', {'date':'2026-09-17','content':'GLOBAL_ONLY'}, scope={'context_mode':'global'})
                assert 'ID:' in result, result
                print('PASS: project diary blocked; global diary still works')
            async def fake_provider(request):
                body = json.loads(request.content)
                assert 'PROJECT_P_CALENDAR_SECRET' not in json.dumps(body)
                assert 'GLOBAL_ONLY' in json.dumps(body)
                return httpx.Response(200, json={'choices': [{'message': {'content': 'PROFILE GLOBAL_ONLY'}}]})
            with patch.object(database, 'resolve_model_endpoint', AsyncMock(return_value=('http://mock.test/model', 'mock', 'openai'))), patch.object(httpx, 'AsyncClient', lambda **kw: OriginalClient(transport=httpx.MockTransport(fake_provider), **kw)):
                print('PROFILE_RESULT', await daily_digest.update_user_profile(model_override='mock'))
            # The early return is after real profile injection, avoiding external embedding calls.
            with patch.object(main, 'get_memory_enabled', AsyncMock(return_value=False)):
                prompt, _ = await main.build_system_prompt_with_memories('hello', scope={'context_mode': 'global', 'context_project_id': None})
                assert 'PROJECT_P_CALENDAR_SECRET' not in prompt
                assert 'GLOBAL_ONLY' in prompt
                print('PASS: project diary absent from profile input and global prompt')
        await database.close_pool()
    finally:
        await control.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        assert await control.fetchval('SELECT count(*) FROM pg_database WHERE datname=$1', name) == 0
        print('PASS: disposable PostgreSQL database removed')
        await control.close()
asyncio.run(run())
