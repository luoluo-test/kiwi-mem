"""Character boundary tests. In-memory fixtures are NOT PostgreSQL evidence."""
import asyncio
import json
import sys
import unittest
import os
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
from character_gateway import select_character, CharacterError, database_url_for, create_app
from character_boundary import WorkerBoundary, chat_contract


class IdentityTests(unittest.TestCase):
    def test_missing_is_legacy_default_only(self):
        self.assertEqual(select_character(), "default")
        self.assertEqual(select_character(body={"character_id": "A"}), "A")

    def test_invalid_never_falls_back(self):
        for value in (None, "", " A", "../A", "A/B", 1, True, [], "x" * 129):
            with self.subTest(value=value), self.assertRaises(CharacterError):
                select_character(body={"character_id": value})

    def test_conflicting_selectors_rejected(self):
        with self.assertRaises(CharacterError) as caught:
            select_character(path_id="A", header_id="B")
        self.assertEqual(caught.exception.status, 409)
        with self.assertRaises(CharacterError):
            select_character(body={"character_id": "A", "characterId": "B"})

    def test_database_url_preserves_connection_options(self):
        self.assertEqual(database_url_for("postgresql://u:p@localhost/old?sslmode=require", "kiwi_char_123"),
                         "postgresql://u:p@localhost/kiwi_char_123?sslmode=require")

    def test_database_query_override_rejected(self):
        for key in ("database", "dbname", "host", "user"):
            with self.assertRaises(ValueError):
                database_url_for("postgresql://localhost/old?" + key + "=old", "new")

    def test_auxiliary_contract(self):
        body = {"memory_mode": "auxiliary"}
        self.assertIsNone(chat_contract(body))
        self.assertEqual(body, {"skip_system_prompt": True})
        self.assertEqual(chat_contract({"memory_mode": "typo"})[1], 400)


class FakeRegistry:
    def __init__(self):
        self.rows = {cid: {"id": cid, "name": cid, "state": "active"}
                     for cid in ("default", "A", "B")}

    async def list(self):
        return list(self.rows.values())

    async def get(self, cid):
        if cid not in self.rows:
            raise CharacterError("character_not_found", 404)
        if self.rows[cid]["state"] == "disabled":
            raise CharacterError("character_deleted", 410)
        return self.rows[cid]

    async def disable(self, cid):
        if cid == "default":
            raise CharacterError("default_character_protected", 409)
        await self.get(cid)
        self.rows[cid]["state"] = "disabled"


class FakeManager:
    def __init__(self, registry):
        self.registry = registry
        self.calls = []
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(self.respond))
        self.data = {cid: [] for cid in registry.rows}

    async def get(self, cid):
        await self.registry.get(cid)
        return {"url": "http://" + cid.lower() + ".test", "token": "private-" + cid}

    async def disable(self, cid):
        await self.registry.disable(cid)

    async def respond(self, request):
        cid = request.headers["X-Kiwi-Character"]
        assert request.headers["X-Kiwi-Worker-Token"] == "private-" + cid
        assert request.url.host == cid.lower() + ".test"
        body = json.loads(request.content) if request.content else {}
        assert "character_id" not in body and "characterId" not in body
        self.calls.append((cid, request.url.path, body))
        await asyncio.sleep(0)  # Deliberately interleave A and B.
        if request.method == "POST":
            self.data[cid].append(body.get("content", ""))
        if request.method == "DELETE":
            self.data[cid].clear()
        if request.url.path == "/admin/":
            return httpx.Response(200, text='<script src="/admin/js/app.js"></script>',
                                  headers={"Content-Type": "text/html"})
        return httpx.Response(200, json={"memories": self.data[cid]})


class RoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_json_media_types_keep_explicit_role(self):
        for media in (None, 'Application/JSON', 'application/vnd.api+json'):
            headers = {} if media is None else {'Content-Type': media}
            response = await self.client.post('/debug/memories',
                content=json.dumps({'character_id': 'A', 'content': 'private'}), headers=headers)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers['X-Kiwi-Character'], 'A')
        response = await self.client.post('/debug/memories', content='{"character_id":"A"}',
                                          headers={'Content-Type': 'text/plain'})
        self.assertEqual(response.status_code, 415)

    async def test_registry_conflicts_do_not_delete(self):
        for method in ('GET', 'DELETE'):
            r = await self.client.request(method, '/characters/A', headers={'X-Kiwi-Character': 'B'})
            self.assertEqual(r.status_code, 409)
        self.assertEqual(self.registry.rows['A']['state'], 'active')

    async def test_registry_body_and_creation_conflicts_are_rejected(self):
        for method, path, body in (
            ('DELETE', '/characters/A', {'character_id':'B'}),
            ('PATCH', '/characters/A', {'name':'renamed', 'character_id':'B'}),
            ('POST', '/characters', {'id':'A', 'name':'A', 'character_id':'B'}),
        ):
            r = await self.client.request(method, path, json=body)
            self.assertEqual(r.status_code, 409)
        self.assertEqual(self.registry.rows['A']['state'], 'active')

    async def test_nonstandard_json_still_rejects_invalid_and_conflicting_roles(self):
        for media in ('Application/JSON', 'application/problem+json'):
            for path, body, status in (
                ('/debug/memories', {'character_id':None}, 400),
                ('/debug/memories', {'character_id':'missing'}, 404),
                ('/characters/A/debug/memories', {'character_id':'B'}, 409),
            ):
                r = await self.client.post(path, content=json.dumps(body), headers={'Content-Type':media})
                self.assertEqual(r.status_code, status)
        self.assertEqual(self.manager.calls, [])

    async def test_scoped_upload_routes_remain_available(self):
        await self.manager.client.aclose()
        self.manager.client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={'role':r.headers['x-kiwi-character']})))
        for path in ('sync/import-backup', 'v1/files/extract'):
            r = await self.client.post('/characters/A/' + path, files={'file':('test.txt', b'test')})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()['role'], 'A')

    async def test_session_header_remains_browser_readable(self):
        r = await self.client.get('/characters/A/debug/memories', headers={'Origin': 'https://float.example'})
        self.assertIn('X-Kiwi-Session-Id', r.headers['access-control-expose-headers'])

    async def asyncSetUp(self):
        self.registry = FakeRegistry()
        self.manager = FakeManager(self.registry)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(
            app=create_app(self.registry, self.manager)), base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.manager.client.aclose()

    async def test_concurrent_calls_and_reused_session_ids(self):
        await asyncio.gather(*(self.client.post("/v1/chat/completions", json={
            "character_id": cid, "conversation_id": "same", "content": cid + str(i)})
            for i in range(10) for cid in ("A", "B")))
        for cid in ("A", "B"):
            response = await self.client.get(f"/characters/{cid}/debug/memories")
            self.assertEqual(len(response.json()["memories"]), 10)
            self.assertTrue(all(s.startswith(cid) for s in response.json()["memories"]))
            self.assertEqual(response.headers["X-Kiwi-Character"], cid)
        self.assertEqual(self.manager.data["default"], [])

    async def test_all_paths_use_role_including_admin_tools_and_jobs(self):
        paths = ["debug/memories", "dream/start", "calendar/2026-09-16", "admin/config",
                 "admin/update-profile-now", "sync/export", "sync/import", "search/messages",
                 "memory/mcp", "calendar/mcp", "sync/reset"]
        for path in paths:
            response = await self.client.post(f"/characters/A/{path}", json={"content": "sentinel"})
            self.assertEqual(response.status_code, 200, path)
            self.assertEqual(self.manager.calls[-1][0], "A")

    async def test_missing_role_only_uses_default(self):
        await self.client.post("/debug/memories", json={"content": "legacy"})
        self.assertEqual((await self.client.get("/characters/B/debug/memories")).json()["memories"], [])

    async def test_unknown_invalid_conflicting_never_reach_worker(self):
        cases = [({"character_id": "missing"}, {}, 404),
                 ({"character_id": None}, {}, 400),
                 ({"character_id": ""}, {}, 400),
                 ({"character_id": "A"}, {"X-Kiwi-Character": "B"}, 409)]
        for body, headers, status in cases:
            response = await self.client.post("/debug/memories", json=body, headers=headers)
            self.assertEqual(response.status_code, status)
        self.assertEqual(self.manager.calls, [])

    async def test_path_conflict_and_query_selector(self):
        self.assertEqual((await self.client.get("/characters/A/debug/memories",
                         headers={"X-Kiwi-Character": "B"})).status_code, 409)
        self.assertEqual((await self.client.get("/debug/memories?character_id=A")).status_code, 400)

    async def test_no_worker_token_spoofing(self):
        response = await self.client.get("/characters/A/debug/memories",
                                        headers={"X-Kiwi-Worker-Token": "private-B"})
        self.assertEqual(response.status_code, 200)

    async def test_delete_does_not_modify_other_role(self):
        self.manager.data["B"] = ["B-private"]
        await self.client.delete("/characters/A/debug/memories")
        self.assertEqual(self.manager.data["B"], ["B-private"])
        self.assertEqual((await self.client.delete("/characters/A")).status_code, 200)
        self.assertEqual((await self.client.get("/characters/A/debug/memories")).status_code, 410)
        self.assertEqual((await self.client.delete("/characters/default")).status_code, 409)

    async def test_admin_url_stays_in_role(self):
        response = await self.client.get("/admin", headers={"X-Kiwi-Character": "A"})
        self.assertEqual(response.headers["location"], "/characters/A/admin/")
        response = await self.client.get(response.headers["location"])
        self.assertIn('/characters/A/admin/js/app.js', response.text)

    async def test_duplicate_header_rejected(self):
        response = await self.client.get("/debug/memories", headers=[
            ("X-Kiwi-Character", "A"), ("X-Kiwi-Character", "B")])
        self.assertEqual(response.status_code, 400)

    async def test_private_worker_enforces_token_and_role(self):
        async def app(scope, receive, send):
            from starlette.responses import JSONResponse
            await JSONResponse({"ok": True})(scope, receive, send)
        with patch.dict(os.environ, {"KIWI_CHARACTER_ID": "A", "KIWI_WORKER_TOKEN": "secret"}):
            boundary = WorkerBoundary(app)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=boundary), base_url="http://test") as c:
            self.assertEqual((await c.get("/debug/memories")).status_code, 403)
            self.assertEqual((await c.get("/debug/memories", headers={
                "X-Kiwi-Worker-Token": "secret", "X-Kiwi-Character": "B"})).status_code, 409)
            self.assertEqual((await c.get("/debug/memories", headers={
                "X-Kiwi-Worker-Token": "secret", "X-Kiwi-Character": "A"})).status_code, 200)


if __name__ == "__main__":
    unittest.main()
