"""One public gateway, immutable character workers, one PostgreSQL database each.

No mutable 'current character', query rewriting, or fallback to another worker.
The registry lives in the legacy database; legacy tables are not copied or rewritten.
Run ONE supervisor per registry. See docs/character-isolation.md before upgrading.
"""
import asyncio
import json
import os
import re
import secrets
import signal
import socket
import sys
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from kiwi_version import VERSION


class CharacterError(Exception):
    def __init__(self, code, status=400):
        self.code, self.status = code, status


def validate_character(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise CharacterError("invalid_character_id")
    return value


def select_character(*, path_id=None, header_id=None, body=None):
    candidates = []
    if path_id is not None:
        candidates.append(validate_character(path_id))
    if header_id is not None:
        candidates.append(validate_character(header_id))
    for key in ("character_id", "characterId"):
        if body is not None and key in body:
            candidates.append(validate_character(body[key]))
    if len(set(candidates)) > 1:
        raise CharacterError("character_mismatch", 409)
    return candidates[0] if candidates else "default"


async def request_identity(request, path_id=None, *, allow_upload=False):
    """Parse every accepted body before selecting a role; never ignore JSON selectors."""
    headers = request.headers
    if any(k in request.query_params for k in ("character_id", "characterId")):
        raise CharacterError("character_query_selector_not_supported")
    if len(headers.getlist("x-kiwi-character")) > 1:
        raise CharacterError("duplicate_character_header")
    raw = await request.body()
    body = None
    if raw:
        media = headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if allow_upload and media == "multipart/form-data":
            pass  # Upload ownership is supplied by path/header and checked in its manifest.
        elif not media or media == "application/json" or (media.startswith("application/") and media.endswith("+json")):
            try:
                body = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                raise CharacterError("invalid_json")
            if not isinstance(body, dict):
                raise CharacterError("invalid_json")
        else:
            raise CharacterError("unsupported_media_type", 415)
    cid = select_character(path_id=path_id, header_id=headers.get("x-kiwi-character"), body=body)
    return cid, raw, body


def database_url_for(base, name):
    parts = urlsplit(base)
    if parts.scheme not in ("postgres", "postgresql") or not parts.hostname:
        raise ValueError("DATABASE_URL must be a PostgreSQL URL")
    # Query database overrides would silently reconnect workers to the legacy DB.
    from urllib.parse import parse_qsl
    if any(k.lower() in {"database", "dbname", "user", "password", "host", "port"}
           for k, _ in parse_qsl(parts.query)):
        raise ValueError("DATABASE_URL connection identity must not use query overrides")
    if not re.fullmatch(r"[a-zA-Z0-9_]+", name):
        raise ValueError("invalid managed database name")
    return urlunsplit((parts.scheme, parts.netloc, "/" + name, parts.query, ""))


class Registry:
    def __init__(self, dsn):
        self.dsn = dsn
        self.conn = None
        self.lock = asyncio.Lock()
        self.connection_lost = asyncio.Event()

    def require_lease(self):
        if self.conn is None or self.conn.is_closed() or self.connection_lost.is_set():
            raise CharacterError("character_supervisor_unavailable", 503)

    def _connection_terminated(self, conn):
        if conn is self.conn:
            self.connection_lost.set()

    async def wait_for_disconnect(self):
        await self.connection_lost.wait()

    async def open(self):
        database_url_for(self.dsn, "validation")
        self.conn = await asyncpg.connect(self.dsn)
        # Session lock: a second supervisor must not run duplicate schedulers.
        if not await self.conn.fetchval("SELECT pg_try_advisory_lock(192837465, 1701)"):
            await self.conn.close()
            raise RuntimeError("character supervisor already running")
        self.connection_lost.clear()
        self.conn.add_termination_listener(self._connection_terminated)
        await self.conn.execute("""
            CREATE TABLE IF NOT EXISTS kiwi_characters (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                database_name TEXT NOT NULL UNIQUE,
                state TEXT NOT NULL CHECK(state IN ('provisioning','active','disabled')),
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        await self.conn.execute("""
            INSERT INTO kiwi_characters(id,name,database_name,state)
            VALUES ('default','Default (legacy)',current_database(),'active')
            ON CONFLICT (id) DO NOTHING
        """)
        async with self.lock:
            # A crash between CREATE DATABASE and activation is safely repeatable.
            for row in await self.conn.fetch("SELECT * FROM kiwi_characters WHERE state='provisioning'"):
                await self._provision(row)

    async def _provision(self, row):
        name = row["database_name"]
        if not re.fullmatch(r"kiwi_char_[0-9a-f]{32}", name):
            raise RuntimeError("invalid registry database identity")
        exists = await self.conn.fetchval("SELECT 1 FROM pg_database WHERE datname=$1", name)
        if not exists:
            # Never template from legacy memory. Names are generated, never client SQL.
            await self.conn.execute(f'CREATE DATABASE "{name}" TEMPLATE template0')
        await self.conn.execute("UPDATE kiwi_characters SET state='active' WHERE id=$1", row["id"])

    async def list(self):
        async with self.lock:
            return [dict(r) for r in await self.conn.fetch("SELECT * FROM kiwi_characters ORDER BY created_at,id")]

    async def get(self, cid):
        async with self.lock:
            row = await self.conn.fetchrow("SELECT * FROM kiwi_characters WHERE id=$1", cid)
        if row is None:
            raise CharacterError("character_not_found", 404)
        if row["state"] == "disabled":
            raise CharacterError("character_deleted", 410)
        if row["state"] != "active":
            raise CharacterError("character_not_ready", 503)
        return dict(row)

    async def create(self, cid, name):
        async with self.lock:
            if await self.conn.fetchval("SELECT 1 FROM kiwi_characters WHERE id=$1", cid):
                raise CharacterError("character_exists", 409)
            count = await self.conn.fetchval("SELECT count(*) FROM kiwi_characters WHERE state <> 'disabled'")
            if count >= int(os.getenv("KIWI_MAX_CHARACTERS", "16")):
                raise CharacterError("character_capacity_reached", 409)
            dbname = "kiwi_char_" + uuid.uuid4().hex
            row = await self.conn.fetchrow("""
                INSERT INTO kiwi_characters(id,name,database_name,state)
                VALUES ($1,$2,$3,'provisioning') RETURNING *
            """, cid, name, dbname)
            await self._provision(row)
        return await self.get(cid)

    async def rename(self, cid, name):
        await self.get(cid)
        async with self.lock:
            await self.conn.execute("UPDATE kiwi_characters SET name=$2 WHERE id=$1 AND state='active'", cid, name)

    async def disable(self, cid):
        if cid == "default":
            raise CharacterError("default_character_protected", 409)
        async with self.lock:
            result = await self.conn.execute("UPDATE kiwi_characters SET state='disabled' WHERE id=$1", cid)
        if result == "UPDATE 0":
            raise CharacterError("character_not_found", 404)

    async def close(self):
        if self.conn:
            await self.conn.close()


class WorkerManager:
    def __init__(self, registry):
        self.registry = registry
        self.workers = {}
        self.locks = {}
        self.recoveries = {}
        self._closing = False
        self._closed = False
        self._close_lock = asyncio.Lock()
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(300, connect=5), trust_env=False,
                                        follow_redirects=False)
        # Chat/SSE/MCP streams may occupy every business connection indefinitely.
        # Leases and startup probes must never queue behind those connections.
        self.health_client = httpx.AsyncClient(timeout=5, trust_env=False, follow_redirects=False)

    def _require_running(self):
        if self._closing:
            raise CharacterError("character_supervisor_unavailable", 503)
        self.registry.require_lease()

    async def get(self, cid):
        self._require_running()
        await self.registry.get(cid)  # Unknown IDs must not grow the process lock cache.
        async with self.locks.setdefault(cid, asyncio.Lock()):
            self._require_running()
            row = await self.registry.get(cid)  # Recheck after waiting, including deletion.
            old = self.workers.get(cid)
            if old and old["process"].returncode is None:
                return old
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            token = secrets.token_urlsafe(32)
            env = dict(os.environ, PORT=str(port), KIWI_CHARACTER_ID=cid,
                       KIWI_WORKER_TOKEN=token, PYTHONUNBUFFERED="1")
            env["DATABASE_URL"] = (self.registry.dsn if cid == "default" else
                                   database_url_for(self.registry.dsn, row["database_name"]))
            process = await asyncio.create_subprocess_exec(
                sys.executable, str(Path(__file__).with_name("character_worker.py")), env=env,
                **({"creationflags": 0x08000000} if os.name == "nt" else {}))
            worker = {"process": process, "url": f"http://127.0.0.1:{port}", "token": token}
            self.workers[cid] = worker
            try:
                for _ in range(240):
                    self._require_running()
                    if process.returncode is not None:
                        break
                    try:
                        response = await self.health_client.get(worker["url"] + "/_character/ready",
                                                         headers={"X-Kiwi-Worker-Token": token})
                        if response.status_code == 200 and response.json().get("character_id") == cid:
                            self._require_running()
                            return worker
                    except (httpx.HTTPError, ValueError):
                        pass
                    await asyncio.sleep(0.25)
                raise CharacterError("character_worker_unavailable", 503)
            except BaseException:
                await self._stop(cid)
                raise

    async def _stop(self, cid):
        worker = self.workers.get(cid)
        if worker is None:
            return
        process = worker["process"]
        try:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=15)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
        except asyncio.CancelledError:
            # Fatal cleanup can overlap Uvicorn's lifespan shutdown. Keep the
            # worker tracked, and finish killing it before propagating cancellation.
            if process.returncode is None:
                process.kill()
                await asyncio.shield(process.wait())
            raise
        finally:
            if process.returncode is not None and self.workers.get(cid) is worker:
                self.workers.pop(cid)

    async def disable(self, cid):
        async with self.locks.setdefault(cid, asyncio.Lock()):
            await self.registry.disable(cid)
            await self._stop(cid)

    async def close(self):
        async with self._close_lock:
            if self._closed:
                return
            self._closing = True
            pending = list(self.recoveries.values())
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

            async def stop_locked(cid):
                # A request may still be starting this worker. It sees _closing
                # at its next bounded health probe, then releases this lock.
                async with self.locks.setdefault(cid, asyncio.Lock()):
                    await self._stop(cid)

            await asyncio.gather(*(stop_locked(cid) for cid in set(self.workers) | set(self.locks)))
            await self.health_client.aclose()
            await self.client.aclose()
            self._closed = True

    async def heartbeat(self):
        async def ping(worker):
            try:
                self._require_running()
                await self.health_client.get(worker["url"] + "/_character/ready",
                                      headers={"X-Kiwi-Worker-Token": worker["token"]}, timeout=5)
            except httpx.HTTPError:
                pass
        async def recover(cid):
            try:
                await self.get(cid)
            except Exception:
                pass  # Remain unavailable; retry next heartbeat, never change roles.

        while True:
            self._require_running()
            await asyncio.gather(*(ping(w) for w in list(self.workers.values())))
            try:
                rows = await asyncio.wait_for(self.registry.list(), timeout=5)
            except (asyncpg.PostgresError, asyncpg.InterfaceError, OSError, asyncio.TimeoutError):
                # A cancelled/slow query does not release the session lock. A
                # disconnected session does: never reconnect or renew that lease.
                self.registry.require_lease()
                print("event=character_registry_retry increment=1")
                rows = []
            for row in rows:
                cid = row["id"]
                worker = self.workers.get(cid)
                task = self.recoveries.get(cid)
                if (row["state"] == "active" and (not worker or worker["process"].returncode is not None)
                        and (not task or task.done())):
                    # A slow restart must not starve leases of healthy workers.
                    self.recoveries[cid] = asyncio.create_task(recover(cid))
            await asyncio.sleep(5)


def public_character(row):
    return {k: row[k] for k in ("id", "name", "state")}


def _terminate_supervisor():
    # Uvicorn handles this in both python entry points, then exits. raise_signal
    # also works on Windows without os.kill's immediate process termination.
    signal.raise_signal(signal.SIGTERM)


def create_app(registry=None, manager=None, *, shutdown=None):
    registry = registry or Registry(os.getenv("DATABASE_URL", ""))
    manager = manager or WorkerManager(registry)
    shutdown = shutdown or _terminate_supervisor

    @asynccontextmanager
    async def lifespan(app):
        pulse = disconnected = supervisor = None

        async def supervise():
            await asyncio.wait((pulse, disconnected), return_when=asyncio.FIRST_COMPLETED)
            app.state.character_failed = True
            # Consume exceptions without printing connection details. Losing the
            # registry connection releases the supervisor lock, so stop all old
            # workers before asking the process manager to restart this service.
            for task in (pulse, disconnected):
                if not task.done():
                    task.cancel()
            await asyncio.gather(pulse, disconnected, return_exceptions=True)
            try:
                await manager.close()
            finally:
                try:
                    await registry.close()
                finally:
                    print("event=character_supervisor_stopped increment=1", flush=True)
                    shutdown()

        try:
            if os.getenv("MEMORY_ENABLED", "true").lower() == "false":
                raise RuntimeError("character isolation requires MEMORY_ENABLED=true at startup")
            await registry.open()
            pulse = asyncio.create_task(manager.heartbeat())
            disconnected = asyncio.create_task(registry.wait_for_disconnect())
            supervisor = asyncio.create_task(supervise())
            # Eager workers keep each role's calendar and Dream schedulers running.
            for row in await registry.list():
                if row["state"] == "active":
                    await manager.get(row["id"])
            yield
        finally:
            tasks = [task for task in (supervisor, pulse, disconnected) if task is not None]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await manager.close()
            await registry.close()

    app = FastAPI(title="Kiwi-Mem character gateway", version=VERSION, lifespan=lifespan)
    from fastapi.middleware.cors import CORSMiddleware
    app.add_middleware(CORSMiddleware,
                       allow_origins=os.getenv("CORS_ORIGINS", "*").split(","),
                       allow_methods=["*"], allow_headers=["*"],
                       expose_headers=["X-Kiwi-Character", "X-Kiwi-Session-Id"])

    @app.exception_handler(CharacterError)
    async def character_error(request, exc):
        return JSONResponse({"error": exc.code, "code": exc.code}, status_code=exc.status)

    @app.exception_handler(Exception)
    async def service_error(request, exc):
        # Never return DSNs, provider errors, or another worker as a fallback.
        return JSONResponse({"error": "character_service_unavailable",
                             "code": "character_service_unavailable"}, status_code=503)

    @app.get("/characters")
    async def list_characters():
        return {"characters": [public_character(row) for row in await registry.list()]}

    @app.get("/character-manager", include_in_schema=False)
    async def character_manager():
        from fastapi.responses import FileResponse
        return FileResponse(Path(__file__).parent / "admin-panel" / "characters.html",
                            headers={"Cache-Control": "no-store"})

    async def metadata(request, path_id=None):
        _, _, body = await request_identity(request, path_id)
        if not isinstance(body, dict):
            raise CharacterError("invalid_json")
        name = body.get("name")
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 200:
            raise CharacterError("invalid_character_name")
        return body, name

    @app.post("/characters", status_code=201)
    async def create_character(request: Request):
        body, name = await metadata(request)
        cid = validate_character(body.get("id"))
        select_character(path_id=cid, header_id=request.headers.get("x-kiwi-character"), body=body)
        row = await registry.create(cid, name)
        await manager.get(cid)
        return public_character(row)

    @app.get("/characters/{cid}")
    async def get_character(cid: str, request: Request):
        await request_identity(request, cid)
        return public_character(await registry.get(validate_character(cid)))

    @app.patch("/characters/{cid}")
    async def rename_character(cid: str, request: Request):
        body, name = await metadata(request, cid)
        body.pop("character_id", None)
        body.pop("characterId", None)
        if set(body) != {"name"}:
            raise CharacterError("immutable_character_identity")
        await registry.rename(validate_character(cid), name)
        return public_character(await registry.get(cid))

    @app.delete("/characters/{cid}")
    async def delete_character(cid: str, request: Request):
        await request_identity(request, cid)
        await manager.disable(validate_character(cid))
        return {"status": "disabled", "data_retained": True}

    async def proxy(request, path, path_id=None):
        headers = request.headers
        cid, raw, body = await request_identity(request, path_id,
                                               allow_upload=path in {"sync/import-backup", "v1/files/extract"})
        if path_id is None and path in ("admin", "admin/"):
            from fastapi.responses import RedirectResponse
            await registry.get(cid)
            return RedirectResponse(f"/characters/{cid}/admin/")
        worker = await manager.get(cid)
        if body is not None:
            body.pop("character_id", None)
            body.pop("characterId", None)
            raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        if path.startswith("_character/"):
            raise CharacterError("reserved_worker_path", 404)
        forwarded = {k: v for k, v in headers.items() if k.lower() not in {
            "host", "content-length", "connection", "transfer-encoding", "x-kiwi-worker-token",
            "x-kiwi-character", "accept-encoding"}}
        forwarded["X-Kiwi-Worker-Token"] = worker["token"]
        forwarded["X-Kiwi-Character"] = cid
        forwarded["Accept-Encoding"] = "identity"
        url = worker["url"] + "/" + path
        if request.url.query:
            url += "?" + request.url.query
        response = await manager.client.send(manager.client.build_request(
            request.method, url, headers=forwarded, content=raw), stream=True)
        out_headers = {k: v for k, v in response.headers.items() if k.lower() not in {
            "content-length", "transfer-encoding", "connection", "content-encoding"}}
        out_headers["X-Kiwi-Character"] = cid
        prefix = f"/characters/{cid}" if path_id is not None else ""
        if "location" in out_headers:
            loc = urlsplit(out_headers["location"])
            if loc.hostname in (None, "127.0.0.1"):
                out_headers["location"] = prefix + loc.path + ("?" + loc.query if loc.query else "")
        if prefix and "text/html" in out_headers.get("content-type", ""):
            # Static module URLs and API requests remain in this tab's immutable role.
            html = (await response.aread()).decode("utf-8")
            await response.aclose()
            html = html.replace('href="/admin/', f'href="{prefix}/admin/')
            html = html.replace('src="/admin/', f'src="{prefix}/admin/')
            from fastapi.responses import HTMLResponse
            return HTMLResponse(html, status_code=response.status_code, headers=out_headers)

        async def chunks():
            try:
                async for chunk in response.aiter_bytes():
                    yield chunk
            finally:
                await response.aclose()
        return StreamingResponse(chunks(), status_code=response.status_code, headers=out_headers)

    methods = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]

    @app.api_route("/characters/{cid}/{path:path}", methods=methods)
    async def scoped_proxy(cid: str, path: str, request: Request):
        return await proxy(request, path, cid)

    @app.api_route("/{path:path}", methods=methods)
    async def legacy_proxy(path: str, request: Request):
        return await proxy(request, path)

    return app


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(create_app(), host="0.0.0.0", port=int(os.getenv("PORT", "8080")), access_log=False)
