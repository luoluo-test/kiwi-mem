"""Actual response paths with a mock provider and Dream launcher; no database."""
import asyncio
from contextlib import ExitStack
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import character_boundary
import httpx
import main

CLIENT = httpx.AsyncClient
MARKER = '<!--dream:trigger-->'


async def provider(request):
    if json.loads(request.content).get('stream'):
        data = {'choices': [{'delta': {'content': MARKER}, 'finish_reason': None}]}
        return httpx.Response(200, headers={'Content-Type': 'text/event-stream'},
                              content=('data: ' + json.dumps(data) + '\n\ndata: [DONE]\n\n').encode())
    return httpx.Response(200, json={'choices': [{'message': {'content': MARKER}, 'finish_reason': 'stop'}]})


class DreamScopeTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, path, *, project, character=True, auxiliary=False, disconnect=False):
        scope = dict(scope_known=True, ledger_project_id=project,
                     context_mode='live_project' if project else 'global',
                     context_project_id=project, event_code=None)
        launch, fallback = Mock(), AsyncMock()
        finalize = AsyncMock(return_value={'action': 'skip'})
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {'KIWI_CHARACTER_ID': 'A'} if character else {}, clear=True))
            stack.enter_context(patch.object(httpx, 'AsyncClient', lambda **kw: CLIENT(transport=httpx.MockTransport(provider), **kw)))
            stack.enter_context(patch.object(main, '_launch_dream_from_marker', launch))
            stack.enter_context(patch.object(main, '_dream_fallback_after_grace', fallback))
            stack.enter_context(patch.object(main, '_finalize_stream_memories', finalize))
            for name, value in [('get_config', None), ('get_config_bool', False), ('get_memory_enabled', False),
                                ('get_reset_generation', 0), ('resolve_scope_snapshot', tuple(scope.values()))]:
                stack.enter_context(patch.object(main, name, AsyncMock(return_value=value)))
            stack.enter_context(patch.object(character_boundary, 'bind_session_project', AsyncMock(return_value=True)))
            stack.enter_context(patch.object(main, 'resolve_provider_for_model', AsyncMock(return_value={
                'model_id': 'mock', 'api_key': 'mock', 'api_format': 'openai',
                'api_base_url': 'http://mock.test', 'provider_name': 'mock'})))
            if path == 'nonstream':
                async with CLIENT(transport=httpx.ASGITransport(app=main.app), base_url='http://test') as client:
                    response = await client.post('/v1/chat/completions', json={
                        'project_id': project, 'conversation_id': 's', 'skip_system_prompt': True,
                        'memory_mode': 'auxiliary' if auxiliary else 'interaction',
                        'messages': [{'role': 'user', 'content': 'sleep'}]})
                self.assertEqual(response.status_code, 200)
                return launch.call_count, False
            if path == 'plain_stream':
                stream = main.stream_and_capture({}, {'model': 'mock', 'stream': True, 'messages': []},
                    's', 'u', 'mock', api_url='http://mock.test', project_id=project,
                    record_events=False, extract_enabled=False, ledger_ctx={'scope': scope}, allow_dream=not auxiliary)
            else:
                stream = main._stream_with_tools(messages=[{'role': 'user', 'content': 'sleep'}],
                    tools=[{'type': 'function', 'function': {'name': '_gateway_list_reminders', 'parameters': {'type': 'object'}}}],
                    tool_map={'_gateway_list_reminders': {'type': 'gateway_builtin', 'scope': scope}},
                    model='mock', temperature=0, tool_events=[], session_id='s', user_message='u', mem_enabled=False,
                    api_url='http://mock.test', api_key='mock', project_id=project,
                    record_events=False, extract_enabled=False, ledger_ctx={'scope': scope})
            output = []
            async for part in stream:
                output.append(part.decode() if isinstance(part, bytes) else part)
                if disconnect and MARKER in output[-1]:
                    await stream.aclose()
                    break
            await asyncio.sleep(0)
            if disconnect:
                finalize.assert_awaited_once()
            return fallback.await_count, 'ev_dream' in ''.join(output)

    async def test_project_markers_neither_launch_nor_emit_event(self):
        for path in ('nonstream', 'plain_stream', 'tool_stream'):
            with self.subTest(path=path):
                self.assertEqual(await self.exercise(path, project='P'), (0, False))

    async def test_global_and_legacy_project_markers_still_work(self):
        for path in ('nonstream', 'plain_stream', 'tool_stream'):
            for project, character in ((None, True), ('P', False)):
                with self.subTest(path=path, character=character):
                    self.assertEqual(await self.exercise(path, project=project, character=character),
                                     (1, path != 'nonstream'))

    async def test_auxiliary_remains_inert(self):
        for path in ('nonstream', 'plain_stream'):
            with self.subTest(path=path):
                self.assertEqual(await self.exercise(path, project=None, auxiliary=True), (0, False))

    async def test_disconnect_preserves_scope_and_finalization(self):
        for path in ('plain_stream', 'tool_stream'):
            for project in ('P', None):
                with self.subTest(path=path, project=project):
                    self.assertEqual(await self.exercise(path, project=project, disconnect=True),
                                     (0 if project else 1, False))


if __name__ == '__main__':
    unittest.main()
