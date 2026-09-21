"""Tool execution/exposure guards. Mock callees; no database or provider."""
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main
import mcp_server
import tool_drawer
from character_tools import GLOBAL_WRITE_TOOLS

PROJECT = {'context_mode': 'live_project', 'context_project_id': 'P'}


class ToolScopeTests(unittest.IsolatedAsyncioTestCase):
    async def test_project_cannot_execute_global_writes(self):
        with patch.dict(os.environ, KIWI_CHARACTER_ID='A'):
            for name in GLOBAL_WRITE_TOOLS:
                if name.startswith('_gateway_'):
                    result, _ = await main._execute_gateway_tool(name, {}, {'scope': PROJECT})
                else:
                    fake = AsyncMock(return_value='executed')
                    with patch.object(mcp_server, name, fake):
                        result, _ = await tool_drawer.execute_drawer_tool(name, {}, scope=PROJECT)
                        fake.assert_not_awaited()
                self.assertIn('project_global_write_forbidden', result, name)

    async def test_global_and_legacy_calls_remain_available(self):
        fake = AsyncMock(return_value='executed')
        with patch.object(mcp_server, 'save_calendar_page', fake):
            with patch.dict(os.environ, KIWI_CHARACTER_ID='A'):
                result, _ = await tool_drawer.execute_drawer_tool('save_calendar_page', {}, scope={'context_mode':'global'})
                self.assertEqual(result, 'executed')
            with patch.dict(os.environ, clear=True):
                result, _ = await tool_drawer.execute_drawer_tool('save_calendar_page', {}, scope=PROJECT)
                self.assertEqual(result, 'executed')
        self.assertEqual(fake.await_count, 2)

    async def test_dynamic_expansion_hides_project_global_writes(self):
        tool_drawer._auto_discover_mcp_tools()
        with patch.dict(os.environ, KIWI_CHARACTER_ID='A'):
            for category in ('calendar', 'dream', 'reminder', 'memory'):
                schemas, mapping = tool_drawer.build_tools_for_category(category, scope=PROJECT)
                names = {s['function']['name'] for s in schemas}
                self.assertFalse(names & GLOBAL_WRITE_TOOLS)
                self.assertEqual(names, set(mapping))
            _, mapping = tool_drawer.build_tools_for_category('memory', scope=PROJECT)
            self.assertIn('save_memory', mapping)
            _, mapping = tool_drawer.build_tools_for_category('calendar', scope={'context_mode':'global'})
            self.assertIn('save_calendar_page', mapping)


if __name__ == '__main__':
    unittest.main()
