"""Real disposable PostgreSQL + real worker processes + MOCK model HTTP server.

Requires KIWI_TEST_DATABASE_URL pointing at localhost. Never uses DATABASE_URL.
Creates/drops only its own random registry DB and databases from that registry.
"""
import asyncio
import hashlib
import io
import json
import os
import re
import sys
import threading
import uuid
import zipfile
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

import asyncpg
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from character_gateway import Registry, WorkerManager, create_app, database_url_for

CAPTURES = []
PASSED = []


def check(condition, name):
    if not condition:
        raise AssertionError(name)
    PASSED.append(name)
    print("PASS:", name, flush=True)


class Provider(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        if self.path.endswith("/embeddings"):
            value = body.get("input", "")
            if isinstance(value, list):
                value = value[0]
            value = {"READ_A": "A_PRIVATE_KIWI", "READ_A_NEW_WINDOW": "A_PRIVATE_KIWI",
                     "READ_B": "B_PRIVATE_MANGO", "READ_PROJECT": "PROJECT_PRIVATE",
                     "READ_GLOBAL": "PROJECT_PRIVATE"}.get(value, value)
            digest = hashlib.sha256(str(value).encode()).digest()
            vector = [(b - 128) / 128 for b in digest]
            if "NEW_CITY_A" in str(value):
                vector = [1.0, 0.0] + [0.0] * 30
            result = {"data": [{"embedding": vector}]}
        else:
            title = self.headers.get("X-Title", "")
            text = json.dumps(body.get("messages", []), ensure_ascii=False)
            CAPTURES.append((title, body))
            fact = next((x for x in ("A_PRIVATE_KIWI", "B_PRIVATE_MANGO", "LEGACY_ONLY") if x in text), "EMPTY")
            if "Memory Extraction" in title:
                auto = re.search(r"AUTO_[AB]_FACT", text)
                content = json.dumps([{"content": auto.group(), "importance": 7, "title": "Auto"}] if auto else [])
                if "CONTRADICT_A" in text:
                    content = json.dumps([{"content": "NEW_CITY_A", "importance": 7, "title": "city"}])
            elif title == "Day Page Generation":
                content = json.dumps({"summary": fact, "digest": fact,
                    "sections": [{"period": "上午", "title": "test", "content": fact, "keywords": [fact]}],
                    "diary": fact, "all_keywords": [fact]})
            elif title == "User Profile Update":
                content = "PROFILE_" + fact
            elif title == "Dream":
                content = "NARRATIVE: DREAM_" + fact
            else:
                content = "<!--dream:trigger-->" if "DO_NOT_RECORD" in text else "ack"
            if body.get("stream"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for delta, finish in (({"content": content}, None), ({}, "stop")):
                    self.wfile.write(("data: " + json.dumps({"choices": [{"index": 0, "delta": delta,
                        "finish_reason": finish}]}) + "\n\n").encode())
                self.wfile.write(b"data: [DONE]\n\n")
                return
            result = {"id": "mock", "choices": [{"message": {"role": "assistant", "content": content},
                       "finish_reason": "stop"}], "usage": {"prompt_tokens": 10, "completion_tokens": 1}}
        payload = json.dumps(result).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


async def run():
    base = os.getenv("KIWI_TEST_DATABASE_URL", "")
    if not base or urlsplit(base).hostname not in ("localhost", "127.0.0.1", "::1"):
        raise SystemExit("BLOCKED: set KIWI_TEST_DATABASE_URL to a disposable localhost PostgreSQL")
    control = await asyncpg.connect(base)
    name = "acceptance_character_" + uuid.uuid4().hex
    await control.execute(f'CREATE DATABASE "{name}" TEMPLATE template0')
    dsn = database_url_for(base, name)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    api = f"http://127.0.0.1:{server.server_port}/v1/chat/completions"
    registry = Registry(dsn)
    manager = None
    created = []
    env = {"API_KEY": "acceptance-mock-key", "API_BASE_URL": api,
           "MEMORY_API_KEY": "acceptance-mock-key", "MEMORY_API_BASE_URL": api,
           "DEFAULT_MODEL": "mock", "MEMORY_MODEL": "mock", "MEMORY_ENABLED": "true",
           "MEMORY_EXTRACT_INTERVAL": "1", "PYTHONUTF8": "1", "KIWI_MAX_CHARACTERS": "16"}
    try:
        with patch.dict(os.environ, env):
            await registry.open()
            manager = WorkerManager(registry)
            app = create_app(registry, manager)
            pulse = asyncio.create_task(manager.heartbeat())
            client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=120)
            try:
                async def req(method, path, cid="A", **kw):
                    response = await client.request(method, f"/characters/{cid}" + path, **kw)
                    if response.status_code >= 400:
                        raise AssertionError((method, path, response.status_code, response.text[:200]))
                    return response

                # Initialize default and seed legacy data, then rerun registry migration.
                r = await req("POST", "/debug/memories", "default", json={"content": "LEGACY_ONLY"})
                check(r.json().get("status") == "added", "legacy data remains usable")
                await req("PUT", "/admin/config/user_profile", "default", json={"value": "LEGACY_ONLY"})
                legacy_before = await registry.conn.fetchrow("SELECT * FROM memories WHERE id=$1", r.json()["id"])
                # Repeat migration across a real supervisor shutdown. Releasing
                # its registry lease while workers run is now a fatal condition.
                pulse.cancel()
                await asyncio.gather(pulse, return_exceptions=True)
                await client.aclose()
                await manager.close()
                await registry.close()
                await registry.open()
                manager = WorkerManager(registry)
                app = create_app(registry, manager)
                pulse = asyncio.create_task(manager.heartbeat())
                client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=120)
                check(len(await registry.list()) == 1, "registry migration repeatable")
                check(await registry.conn.fetchrow("SELECT * FROM memories WHERE id=$1", r.json()["id"]) == legacy_before,
                      "registry migration leaves legacy memory columns unchanged")
                for cid in ("A", "B"):
                    r = await client.post("/characters", json={"id": cid, "name": "same display name"})
                    check(r.status_code == 201, f"create {cid} independent worker and database")
                created = [r["database_name"] for r in await registry.list() if r["id"] != "default"]
                rows = {r["id"]: r for r in await registry.list()}
                conns = {cid: await asyncpg.connect(dsn if cid == "default" else database_url_for(dsn, row["database_name"]))
                         for cid, row in rows.items()}
                try:
                    for cid in ("A", "B"):
                        check(await conns[cid].fetchval("SELECT count(*) FROM memories") == 0,
                              f"new {cid} does not inherit default memories")
                    ids = {}
                    for cid, fact in (("A", "A_PRIVATE_KIWI"), ("B", "B_PRIVATE_MANGO")):
                        r = await req("POST", "/debug/memories", cid, json={"content": fact, "importance": 9})
                        ids[cid] = r.json()["id"]
                        await req("POST", "/debug/memories/batch-update", cid,
                                  json={"ids": [ids[cid]], "is_permanent": True})
                    renamed = await client.patch("/characters/A", json={"name": "renamed A"})
                    check(renamed.status_code == 200 and renamed.json()["id"] == "A",
                          "renaming preserves stable character identity")
                    async def chat(cid, session, content, **kw):
                        return await req("POST", "/v1/chat/completions", cid, json={
                            "model": "mock", "conversation_id": session, "messages": [{"role": "user", "content": content}], **kw})
                    await asyncio.gather(chat("A", "same-session", "READ_A"), chat("B", "same-session", "READ_B"))
                    await chat("A", "new-session", "READ_A_NEW_WINDOW", stream=True)
                    for marker, own, other in (("READ_A", "A_PRIVATE_KIWI", "B_PRIVATE_MANGO"),
                                              ("READ_B", "B_PRIVATE_MANGO", "A_PRIVATE_KIWI"),
                                              ("READ_A_NEW_WINDOW", "A_PRIVATE_KIWI", "B_PRIVATE_MANGO")):
                        capture = next(b for t, b in CAPTURES if "Extraction" not in t and marker in json.dumps(b))
                        serialized = json.dumps(capture, ensure_ascii=False)
                        check(own in serialized and other not in serialized and "LEGACY_ONLY" not in serialized,
                              "captured prompt isolated: " + marker)
                    await asyncio.gather(chat("A", "auto", "请记住 AUTO_A_FACT"), chat("B", "auto", "请记住 AUTO_B_FACT"))
                    for _ in range(100):
                        counts = [await conns[c].fetchval("SELECT count(*) FROM memories WHERE content=$1", f"AUTO_{c}_FACT") for c in ("A", "B")]
                        if counts == [1, 1]:
                            break
                        await asyncio.sleep(.1)
                    check(counts == [1, 1], "concurrent automatic extraction retained originating role")
                    await chat("A", "auto", "请记住 AUTO_A_FACT")
                    await asyncio.sleep(.5)
                    check(await conns["A"].fetchval("SELECT count(*) FROM memories WHERE content='AUTO_A_FACT'") == 1,
                          "automatic dedup does not duplicate A fact")
                    check(await conns["B"].fetchval("SELECT count(*) FROM memories WHERE content LIKE '%AUTO_A%'") == 0,
                          "B has no A automatic memories")
                    before = await conns["A"].fetchval("SELECT count(*) FROM conversations")
                    await chat("A", "aux", "DO_NOT_RECORD", memory_mode="auxiliary")
                    aux_stream = await chat("A", "aux-stream", "DO_NOT_RECORD_STREAM", memory_mode="auxiliary", stream=True)
                    await asyncio.sleep(.2)
                    check(await conns["A"].fetchval("SELECT count(*) FROM dream_logs") == 0
                          and "ev_dream" not in aux_stream.text, "auxiliary model dream markers cannot start Dream")
                    check(await conns["A"].fetchval("SELECT count(*) FROM conversations") == before,
                          "auxiliary generation did not record conversations")
                    capture = next(b for t, b in CAPTURES if "Extraction" not in t and "DO_NOT_RECORD" in json.dumps(b))
                    check("A_PRIVATE_KIWI" not in json.dumps(capture) and not capture.get("tools"),
                          "auxiliary has no memory prompt or tools")
                    # Same project ID in two roles is two independent projects.
                    await req("POST", "/sync/projects", json={"id": "project", "name": "P"})
                    pr = await req("POST", "/debug/memories", json={"content": "PROJECT_PRIVATE", "project_id": "project"})
                    await req("POST", "/debug/memories/batch-update", json={"ids": [pr.json()["id"]], "is_permanent": True})
                    await chat("A", "project-session", "READ_PROJECT", project_id="project")
                    await chat("A", "global-session", "READ_GLOBAL")
                    pcap = next(b for t, b in CAPTURES if "Extraction" not in t and "READ_PROJECT" in json.dumps(b))
                    gcap = next(b for t, b in CAPTURES if "Extraction" not in t and "READ_GLOBAL" in json.dumps(b))
                    check("PROJECT_PRIVATE" in json.dumps(pcap) and "PROJECT_PRIVATE" not in json.dumps(gcap),
                          "project and character scope both constrain prompt")
                    bad = await client.post("/characters/B/v1/chat/completions", json={"project_id": "project", "messages": []})
                    check(bad.status_code == 409, "another role's project is rejected before model call")
                    bad = await client.post("/characters/A/v1/chat/completions", json={
                        "conversation_id": "project-session", "messages": [{"role": "user", "content": "REUSE"}]})
                    check(bad.status_code == 409, "reusing a session in another project scope is rejected")
                    # Real FastMCP -> database/tool implementation, not a mocked executor.
                    tool = await req("POST", "/memory/mcp", json={"jsonrpc": "2.0", "id": 1,
                        "method": "tools/call", "params": {"name": "get_recent", "arguments": {"limit": 50}}},
                        headers={"Accept": "application/json, text/event-stream"})
                    check("A_PRIVATE_KIWI" in tool.text and "B_PRIVATE_MANGO" not in tool.text
                          and "PROJECT_PRIVATE" not in tool.text, "real memory MCP respects character and global project scope")
                    search = await req("GET", "/debug/memories?q=A_PRIVATE_KIWI", "B")
                    check("A_PRIVATE_KIWI" not in json.dumps(search.json().get("memories", [])),
                          "B keyword/vector search cannot return A fact")
                    b_snapshot = await conns["B"].fetch("SELECT id,content,is_permanent,valid_until FROM memories ORDER BY id")
                    # Calendar's upstream source is synced chat_messages, not the raw ledger.
                    now = datetime.now(timezone.utc).isoformat()
                    for cid, fact in (("A", "A_PRIVATE_KIWI"), ("B", "B_PRIVATE_MANGO")):
                        await req("PUT", "/sync/conversations/calendar-source", cid, json={
                            "title": "acceptance calendar", "messages": [{"id": "calendar-message",
                            "role": "user", "content": fact, "createdAt": now}]})
                    # Calendar/profile/Dream actually run against mock provider and separate DBs.
                    today = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
                    for cid, fact, other in (("A", "A_PRIVATE_KIWI", "B_PRIVATE_MANGO"), ("B", "B_PRIVATE_MANGO", "A_PRIVATE_KIWI")):
                        result = await req("GET", "/admin/day-page?date=" + today, cid)
                        check(result.json().get("status") not in ("error", "skipped") and "error" not in result.json(), f"{cid} day page generated")
                        await req("POST", "/admin/update-profile-now", cid)
                        profile = await conns[cid].fetchval("SELECT value FROM gateway_config WHERE key='user_profile'")
                        check(fact in profile and other not in profile, f"{cid} profile isolation after update")
                        dream = await req("POST", "/dream/start", cid, json={})
                        check("event: complete" in dream.text and "event: error" not in dream.text,
                              f"{cid} Dream completed")
                        check(other not in dream.text, f"{cid} Dream output isolated")
                        if cid == "A":
                            check(await conns["B"].fetch("SELECT id,content,is_permanent,valid_until FROM memories ORDER BY id") == b_snapshot,
                                  "A Dream did not modify B memories or locks")
                    for title, b in CAPTURES:
                        if title in ("Day Page Generation", "Dream", "User Profile Update"):
                            text = json.dumps(b)
                            check(not ("A_PRIVATE_KIWI" in text and "B_PRIVATE_MANGO" in text), title + " source separation")
                            check("PROJECT_PRIVATE" not in text, title + " excludes project foundation")
                    for cid in ("A", "B"):
                        await conns[cid].execute("""
                            INSERT INTO memories(id,content,title,source,importance,embedding)
                            VALUES (900,$1,'city','ai_extracted',7,$2)
                        """, "OLD_CITY_" + cid, json.dumps([0.6, 0.8] + [0.0] * 30))
                    await chat("A", "contradiction", "请记住 CONTRADICT_A")
                    for _ in range(100):
                        invalidated = await conns["A"].fetchval("SELECT valid_until IS NOT NULL FROM memories WHERE id=900")
                        if invalidated:
                            break
                        await asyncio.sleep(.1)
                    check(invalidated, "automatic contradiction detection invalidates A old fact")
                    check(await conns["B"].fetchval("SELECT valid_until IS NULL FROM memories WHERE id=900"),
                          "contradiction does not invalidate same-ID B fact")
                    check(await conns["B"].fetchval("SELECT count(*) FROM memory_edges") == 0,
                          "contradiction edges never enter B")
                    # Export metadata prevents accidental cross-role restoration before any writes.
                    backup = await req("GET", "/sync/export")
                    restored = await client.post("/characters/B/sync/import-backup", files={"file": ("a.zip", backup.content, "application/zip")})
                    check(restored.status_code == 409, "A backup rejected by B")
                    with zipfile.ZipFile(io.BytesIO(backup.content)) as z:
                        check(json.loads(z.read("character.json"))["character_id"] == "A", "backup records role")
                        check(any(m.get("project_id") == "project" for m in json.loads(z.read("memories.json"))), "backup retains project ownership")
                    b_before = await conns["B"].fetchval("SELECT count(*) FROM memories")
                    await req("DELETE", f"/debug/memories/{ids['A']}?force=true")
                    check(await conns["B"].fetchval("SELECT count(*) FROM memories") == b_before, "same numeric memory ID in B survives A deletion")
                    await req("DELETE", "/sync/conversations/same-session")
                    check(await conns["B"].fetchval("SELECT count(*) FROM conversations WHERE session_id='same-session'") > 0,
                          "same session ID in B survives A deletion")
                    deleted = await client.delete("/characters/A")
                    check(deleted.status_code == 200 and "A" not in manager.workers, "delete role stops worker and retains database")
                    check((await client.get("/characters/A/debug/memories")).status_code == 410, "deleted role never falls back")
                    check(await conns["B"].fetchval("SELECT count(*) FROM memories") == b_before, "B survives A role deletion")
                finally:
                    await asyncio.gather(*(c.close() for c in conns.values()))
            finally:
                await client.aclose()
                pulse.cancel()
                await asyncio.gather(pulse, return_exceptions=True)
                await manager.close()
            await registry.close()
            restarted_registry = Registry(dsn)
            restarted_manager = WorkerManager(restarted_registry)
            supervisor_stopped = asyncio.Event()
            restarted_app = create_app(restarted_registry, restarted_manager, shutdown=supervisor_stopped.set)
            async with restarted_app.router.lifespan_context(restarted_app):
                check(set(restarted_manager.workers) == {"default", "B"},
                      "supervisor restart eagerly restores only active roles")
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=restarted_app), base_url="http://test") as c:
                    result = await c.get("/characters/B/debug/memories")
                    check("B_PRIVATE_MANGO" in result.text and "A_PRIVATE_KIWI" not in result.text,
                          "B persisted memories survive supervisor restart")
                    check((await c.get("/characters/A/debug/memories")).status_code == 410,
                          "role deletion tombstone survives restart")
                workers = list(restarted_manager.workers.values())
                # Terminate only this test's registry connection. The lost
                # PostgreSQL session also loses its supervisor advisory lock.
                await control.fetchval("SELECT pg_terminate_backend($1)", restarted_registry.conn.get_server_pid())
                await asyncio.wait_for(supervisor_stopped.wait(), timeout=25)
                check(not restarted_manager.workers and all(w["process"].returncode is not None for w in workers),
                      "real registry disconnect stops all old workers before shutdown")
                check(restarted_manager.client.is_closed and restarted_manager.health_client.is_closed,
                      "registry disconnect closes business and health clients")
            replacement_registry = Registry(dsn)
            try:
                await replacement_registry.open()
                check(len(await replacement_registry.list()) == 3,
                      "fresh supervisor reacquires lease with preserved registry after disconnect")
            finally:
                await replacement_registry.close()
    finally:
        if registry.conn and not registry.conn.is_closed():
            created = [r["database_name"] for r in await registry.list() if r["id"] != "default"]
        await registry.close()
        server.shutdown()
        server.server_close()
        for db in [*created, name]:
            assert db == name or re.fullmatch(r"kiwi_char_[0-9a-f]{32}", db)
            await control.execute(f'DROP DATABASE "{db}" WITH (FORCE)')
        remaining = await control.fetchval("SELECT count(*) FROM pg_database WHERE datname=ANY($1::text[])", [*created, name])
        check(remaining == 0, "SQL proves disposable database cleanup")
        await control.close()
    print(f"PASS: {len(PASSED)} character PostgreSQL guards; real workers, MOCK provider, no real model calls")


if __name__ == "__main__":
    asyncio.run(run())
