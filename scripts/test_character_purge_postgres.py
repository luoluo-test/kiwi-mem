"""Permanent deletion: disposable PostgreSQL, real workers, MOCK model HTTP.

Only the explicit localhost KIWI_TEST_DATABASE_URL is accepted. This script
creates its own random registry and role databases and drops only those names.
It never reads DATABASE_URL or connects to a production/acceptance role database.
"""
import asyncio
import io
import json
import os
import re
import sys
import threading
import uuid
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import urlsplit

import asyncpg
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from character_gateway import CharacterError, Registry, WorkerManager, create_app, database_url_for
from test_character_postgres import Provider

PASSED = []
STREAM_STARTED = threading.Event()
STREAM_RELEASE = threading.Event()


def check(condition, name):
    if not condition:
        raise AssertionError(name)
    PASSED.append(name)
    print("PASS:", name, flush=True)


class PurgeProvider(Provider):
    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        body = json.loads(raw)
        if body.get("stream") and "HOLD_PURGE_STREAM" in raw.decode():
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b'data: {"choices":[{"index":0,"delta":{"content":"held"},"finish_reason":null}]}\n\n')
            self.wfile.flush()
            STREAM_STARTED.set()
            STREAM_RELEASE.wait(timeout=45)
            try:
                self.wfile.write(b"data: [DONE]\n\n")
            except (BrokenPipeError, ConnectionResetError):
                pass
        else:
            self.rfile = io.BytesIO(raw)
            super().do_POST()


async def snapshot(conn):
    # These tables cover all three stored vector forms, receipts, profile,
    # private/project memories, chats, calendar, Dream, reminders and projects.
    tables = ("memories", "mem_scenes", "project_file_chunks", "float_memory_events",
              "conversations", "chat_conversations", "chat_messages", "chat_projects",
              "calendar_pages", "dream_logs", "reminders", "gateway_config")
    result = {}
    for table in tables:
        result[table] = await conn.fetch(f'SELECT to_jsonb(t)::text AS value FROM "{table}" t ORDER BY to_jsonb(t)::text')
    return result


async def seed(conn, cid):
    marker = "acceptance-purge-" + cid
    vec = json.dumps([0.25, 0.5, 0.75] + [0.0] * 29)
    await conn.execute("INSERT INTO chat_projects(id,name) VALUES ('acceptance-project',$1)", marker)
    mid = await conn.fetchval("INSERT INTO memories(content,title,embedding,is_permanent) VALUES ($1,$1,$2,true) RETURNING id", marker, vec)
    await conn.execute("INSERT INTO memories(content,embedding,project_id,is_permanent) VALUES ($1,$2,'acceptance-project',true)", marker + "-project", vec)
    await conn.execute("INSERT INTO mem_scenes(title,narrative,embedding) VALUES ($1::text,$1::text,$2::jsonb)", marker, vec)
    await conn.execute("INSERT INTO project_file_chunks(project_id,file_id,content,embedding) VALUES ('acceptance-project','acceptance-file',$1,$2)", marker, vec)
    await conn.execute("""INSERT INTO float_memory_events(event_id,payload_hash,session_id,message_id,
        source,occurred_at,visibility,memory_id) VALUES ('acceptance-event','hash','acceptance-session',
        'acceptance-message','float','2026-09-26T00:00:00Z','private',$1)""", mid)
    await conn.execute("INSERT INTO conversations(session_id,role,content) VALUES ('acceptance-session','user',$1)", marker)
    await conn.execute("INSERT INTO chat_conversations(id,title) VALUES ('acceptance-session',$1)", marker)
    await conn.execute("INSERT INTO chat_messages(id,conversation_id,role,content) VALUES ('acceptance-message','acceptance-session','user',$1)", marker)
    await conn.execute("INSERT INTO calendar_pages(date,diary) VALUES ('2026-01-01',$1)", marker)
    await conn.execute("INSERT INTO dream_logs(status,dream_narrative) VALUES ('completed',$1)", marker)
    await conn.execute("INSERT INTO reminders(id,title,trigger_time,enabled) VALUES ('acceptance-reminder',$1,'2099-01-01',false)", marker)
    await conn.execute("INSERT INTO gateway_config(key,value) VALUES ('user_profile',$1) ON CONFLICT(key) DO UPDATE SET value=excluded.value", marker)


async def run():
    base = os.getenv("KIWI_TEST_DATABASE_URL", "")
    if not base or urlsplit(base).hostname not in ("localhost", "127.0.0.1", "::1"):
        raise SystemExit("BLOCKED: set KIWI_TEST_DATABASE_URL to a disposable localhost PostgreSQL")
    control = await asyncpg.connect(base)
    registry_name = "acceptance_purge_" + uuid.uuid4().hex
    owned = {registry_name}
    await control.execute(f'CREATE DATABASE "{registry_name}" TEMPLATE template0')
    dsn = database_url_for(base, registry_name)
    registry, manager, pulse, client = Registry(dsn), None, None, None
    connections = {}
    server = ThreadingHTTPServer(("127.0.0.1", 0), PurgeProvider)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    endpoint = f"http://127.0.0.1:{server.server_port}/v1/chat/completions"
    env = {"API_KEY": "acceptance-mock-key", "API_BASE_URL": endpoint,
           "MEMORY_API_KEY": "acceptance-mock-key", "MEMORY_API_BASE_URL": endpoint,
           "DEFAULT_MODEL": "mock", "MEMORY_MODEL": "mock", "MEMORY_ENABLED": "true",
           "PYTHONUTF8": "1", "KIWI_MAX_CHARACTERS": "16"}
    try:
        with patch.dict(os.environ, env):
            # Reproduce the old registry shape and a disabled pre-upgrade role.
            old_db = "kiwi_char_" + uuid.uuid4().hex
            owned.add(old_db)
            await control.execute(f'CREATE DATABASE "{old_db}" TEMPLATE template0')
            migration = await asyncpg.connect(dsn)
            try:
                await migration.execute("""CREATE TABLE kiwi_characters (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, database_name TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL CHECK(state IN ('provisioning','active','disabled')),
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
                await migration.execute("INSERT INTO kiwi_characters(id,name,database_name,state) VALUES ('old-disabled','old disabled',$1,'disabled')", old_db)
                before_old = await migration.fetchrow("SELECT id,name,database_name,state,created_at FROM kiwi_characters WHERE id='old-disabled'")
            finally:
                await migration.close()
            await registry.open()
            check(await registry.conn.fetchrow("SELECT id,name,database_name,state,created_at FROM kiwi_characters WHERE id='old-disabled'") == before_old,
                  "old registry migration preserves existing role fields")
            check(await registry.conn.fetchval("SELECT database_oid FROM kiwi_characters WHERE id='old-disabled'") ==
                  await control.fetchval("SELECT oid FROM pg_database WHERE datname=$1", old_db),
                  "old disabled database identity is bound during migration")
            manager = WorkerManager(registry)
            pulse = asyncio.create_task(manager.heartbeat())
            client = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(registry, manager),
                                                                  raise_app_exceptions=False), base_url="http://test", timeout=90)

            async def create(cid):
                response = await client.post("/characters", json={"id": cid, "name": "acceptance " + cid})
                check(response.status_code == 201, "create disposable role " + cid)
                row = next(r for r in await registry.list() if r["id"] == cid)
                owned.add(row["database_name"])
                return row

            async def purge(cid):
                return await client.post(f"/characters/{cid}/purge", json={"confirm_character_id": cid})

            rows = {cid: await create(cid) for cid in ("A", "B")}
            await manager.get("default")
            for cid in ("A", "B", "default"):
                conn = await asyncpg.connect(dsn if cid == "default" else database_url_for(dsn, rows[cid]["database_name"]))
                connections[cid] = conn
                await seed(conn, cid)
            snapshots = {cid: await snapshot(conn) for cid, conn in connections.items()}
            check(all(len(snapshots[cid]["memories"]) == 2 and len(snapshots[cid]["float_memory_events"]) == 1
                      for cid in snapshots), "private/project memories, vectors and receipts seeded in each real database")
            worker_a = manager.workers["A"]
            worker_b = manager.workers["B"]
            # Exercise a real live SSE connection while deleting its worker.
            async with httpx.AsyncClient(timeout=45, trust_env=False) as streaming:
                stream = await streaming.send(streaming.build_request("POST", worker_a["url"] + "/v1/chat/completions",
                    headers={"X-Kiwi-Character": "A", "X-Kiwi-Worker-Token": worker_a["token"]}, json={
                        "model": "mock", "messages": [{"role": "user", "content": "HOLD_PURGE_STREAM"}],
                        "memory_mode": "auxiliary", "stream": True}), stream=True)
                check(stream.status_code == 200 and await asyncio.to_thread(STREAM_STARTED.wait, 10),
                      "real worker holds an active upstream SSE stream before purge")
                response = await purge("A")
                STREAM_RELEASE.set()
                await stream.aclose()
            check(response.status_code == 200 and response.json() == {"status": "deleted", "data_retained": False},
                  "permanent delete confirms database removal")
            check(worker_a["process"].returncode is not None and "A" not in manager.workers,
                  "purge stops the real worker even with an active SSE stream")
            check(not await control.fetchval("SELECT 1 FROM pg_database WHERE datname=$1", rows["A"]["database_name"]),
                  "SQL proves A database and all its vectors and receipts are gone")
            check(connections["A"].is_closed(), "DROP FORCE closes another existing connection to A")
            for cid in ("B", "default"):
                check(await snapshot(connections[cid]) == snapshots[cid], cid + " data and all vector forms remain byte-identical")
            check(manager.workers["B"] is worker_b and worker_b["process"].returncode is None,
                  "B worker remains the same running process")
            check((await purge("A")).status_code == 200, "repeated purge is idempotent")
            check((await client.get("/characters/A/debug/memories")).status_code == 410,
                  "deleted business requests cannot fall back to default")
            check((await client.post("/characters", json={"id": "A", "name": "reuse"})).status_code == 409,
                  "deleted IDs cannot be reused")
            check((await client.delete("/characters/A")).status_code == 410 and
                  next(r for r in await registry.list() if r["id"] == "A")["state"] == "deleted",
                  "legacy DELETE cannot resurrect a deleted tombstone")
            check((await purge("default")).status_code == 409 and
                  await snapshot(connections["default"]) == snapshots["default"], "default database is protected")
            check((await purge("old-disabled")).status_code == 200 and not await control.fetchval(
                  "SELECT 1 FROM pg_database WHERE datname=$1", old_db), "upgraded disabled roles can be permanently erased")

            # Fault before DDL: state and safe error survive while other workers serve.
            with patch.dict(os.environ, {"KIWI_MAX_CHARACTERS": "3"}):
                await create("retry")
            check(True, "deleted tombstones do not consume active role capacity")
            with patch.object(registry, "drop_character_database", new=AsyncMock(side_effect=RuntimeError("PRIVATE_DSN_SENTINEL"))):
                result = await purge("retry")
                check(result.status_code >= 500 and "PRIVATE_DSN_SENTINEL" not in result.text,
                      "DDL failure is a sanitized explicit failure")
                listing = (await client.get("/characters")).json()["characters"]
                retry_row = next(r for r in listing if r["id"] == "retry")
                check(retry_row["state"] == "deleting" and retry_row.get("deletion_error") and
                      "PRIVATE_DSN_SENTINEL" not in json.dumps(listing), "failed purge remains retryable with a safe error code")
                check((await client.get("/characters/retry/debug/memories")).status_code == 409 and
                      (await client.delete("/characters/retry")).status_code == 409,
                      "deleting role rejects business and legacy disable requests")
                check((await client.get("/characters/B/debug/memories")).status_code == 200,
                      "B remains available while A deletion fails")
            check((await purge("retry")).status_code == 200, "same request safely retries a failed purge")

            # Identity guard: never delete an unrelated same-pattern database.
            identity = await create("identity")
            unrelated = "kiwi_char_" + uuid.uuid4().hex
            owned.add(unrelated)
            await control.execute(f'CREATE DATABASE "{unrelated}" TEMPLATE template0')
            async with registry.lock:
                await registry.conn.execute("UPDATE kiwi_characters SET database_name=$1 WHERE id='identity'", unrelated)
            check((await purge("identity")).status_code == 409 and await control.fetchval(
                  "SELECT 1 FROM pg_database WHERE datname=$1", unrelated), "database OID mismatch prevents deleting an unrelated database")
            async with registry.lock:
                await registry.conn.execute("UPDATE kiwi_characters SET database_name=$1 WHERE id='identity'", identity["database_name"])
            check((await purge("identity")).status_code == 200, "correct database identity permits retry")

            # Fault after DROP: restart must finish the existing tombstone and
            # must never re-create the removed database or rebind a replaced OID.
            recovery = await create("recover")
            with patch.object(registry, "finish_purge", new=AsyncMock(side_effect=RuntimeError("after-drop"))):
                check((await purge("recover")).status_code >= 500, "simulated crash window after DROP remains explicit")
            check(not await control.fetchval("SELECT 1 FROM pg_database WHERE datname=$1", recovery["database_name"]),
                  "database was removed before interrupted finalization")
            replaced = await create("replaced")
            with patch.object(registry, "finish_purge", new=AsyncMock(side_effect=RuntimeError("after-drop"))):
                check((await purge("replaced")).status_code >= 500, "second interrupted purge retains its original database identity")
            await control.execute(f'CREATE DATABASE "{replaced["database_name"]}" TEMPLATE template0')
            replacement_oid = await control.fetchval("SELECT oid FROM pg_database WHERE datname=$1", replaced["database_name"])
            check(replacement_oid != replaced["database_oid"], "replacement database has a distinct real PostgreSQL OID")
            await client.aclose()
            client = None
            pulse.cancel()
            await asyncio.gather(pulse, return_exceptions=True)
            pulse = None
            await manager.close()
            await registry.close()
            registry = Registry(dsn)
            await registry.open()
            manager = WorkerManager(registry)
            pulse = asyncio.create_task(manager.heartbeat())
            for _ in range(120):
                if next(r for r in await registry.list() if r["id"] == "recover")["state"] == "deleted":
                    break
                await asyncio.sleep(.1)
            check(next(r for r in await registry.list() if r["id"] == "recover")["state"] == "deleted",
                  "heartbeat resumes pending deletion after supervisor restart")
            check(not await control.fetchval("SELECT 1 FROM pg_database WHERE datname=$1", recovery["database_name"]),
                  "restart never recreates a purged role database")
            final_rows = {r["id"]: r for r in await registry.list()}
            check(final_rows["A"]["state"] == "deleted",
                  "deleted tombstone survives repeated registry migration")
            try:
                await manager.purge("replaced")
                raise AssertionError("replacement database must be protected")
            except CharacterError as exc:
                check(exc.code == "character_database_identity_invalid" and exc.status == 409,
                      "restart never rebinds a deleting role to a replacement database")
            check(await control.fetchval("SELECT oid FROM pg_database WHERE datname=$1", replaced["database_name"]) == replacement_oid,
                  "retry leaves the replacement database untouched")
            for cid in ("B", "default"):
                check(await snapshot(connections[cid]) == snapshots[cid], cid + " data remains unchanged after all failures and restart")
    finally:
        STREAM_RELEASE.set()
        if client:
            await client.aclose()
        if pulse:
            pulse.cancel()
            await asyncio.gather(pulse, return_exceptions=True)
        if manager:
            await manager.close()
        # Registry-owned names are learned only from our random registry; a
        # failed HTTP create might have persisted its row before raising.
        if registry.conn and not registry.conn.is_closed():
            for row in await registry.list():
                if row["id"] != "default":
                    owned.add(row["database_name"])
        await registry.close()
        await asyncio.gather(*(conn.close() for conn in connections.values()), return_exceptions=True)
        server.shutdown()
        server.server_close()
        for db in sorted(owned - {registry_name}) + [registry_name]:
            assert db == registry_name or re.fullmatch(r"kiwi_char_[0-9a-f]{32}", db)
            await control.execute(f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)')
        check(await control.fetchval("SELECT count(*) FROM pg_database WHERE datname=ANY($1::text[])", list(owned)) == 0,
              "SQL proves every disposable database was cleaned up")
        await control.close()
    print(f"PASS: {len(PASSED)} character purge PostgreSQL guards; real workers, MOCK provider, no real model calls")


if __name__ == "__main__":
    asyncio.run(run())
