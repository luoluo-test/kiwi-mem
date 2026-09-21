"""Worker authentication and strict chat contracts; no mutable role context."""
import os
import secrets
import time
import re

last_supervisor_contact = time.monotonic()

from starlette.responses import JSONResponse


class WorkerBoundary:
    def __init__(self, app):
        self.app = app
        self.character = os.getenv("KIWI_CHARACTER_ID")
        self.token = os.getenv("KIWI_WORKER_TOKEN")

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        error, status = None, 403
        if self.token and not secrets.compare_digest(
                headers.get(b"x-kiwi-worker-token", b""), self.token.encode()):
            error = "private_character_worker"
        selected = headers.get(b"x-kiwi-character")
        if selected is not None and selected != (self.character or "default").encode():
            error, status = "character_mismatch", 409
        if error:
            return await JSONResponse({"code": error, "error": error}, status_code=status)(scope, receive, send)
        if self.token and scope["path"] == "/_character/ready":
            global last_supervisor_contact
            last_supervisor_contact = time.monotonic()
        return await self.app(scope, receive, send)


def chat_contract(body):
    """Return (error, status) or None; remove local-only request metadata."""
    expected = os.getenv("KIWI_CHARACTER_ID", "default")
    for key in ("character_id", "characterId"):
        if key in body:
            value = body.pop(key)
            if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
                return "invalid_character_id", 400
            if value != expected:
                return "character_mismatch", 409
    mode = body.pop("memory_mode", "interaction")
    if mode not in ("interaction", "auxiliary"):
        return "invalid_memory_mode", 400
    if mode == "auxiliary":
        body["skip_system_prompt"] = True
    if os.getenv("KIWI_CHARACTER_ID") and body.get("mcp_servers"):
        return "external_mcp_disabled_in_character_mode", 400
    return None


async def initialize_character_tables():
    from float_memory_api import initialize
    await initialize()
    from database import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS kiwi_character_sessions (
                session_id TEXT PRIMARY KEY, project_id TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        if await conn.fetchval("""
            SELECT EXISTS (
                SELECT 1 FROM kiwi_character_sessions b JOIN chat_conversations c ON c.id=b.session_id
                WHERE b.project_id IS DISTINCT FROM c.project_id
            )
        """):
            raise RuntimeError("character session metadata conflicts with permanent bindings; restore consistent backup before startup")


class CharacterScopeError(ValueError):
    """Fixed public error codes, never include session/project identities."""
    def __init__(self, code):
        self.code = code
        self.status = 400 if code == "invalid_project_id" else 409
        super().__init__(code)


def session_project_value(data):
    values = [data[k] for k in ("projectId", "project_id") if k in data]
    for value in values:
        if value is not None and (not isinstance(value, str) or not value.strip() or len(value) > 128):
            raise CharacterScopeError("invalid_project_id")
    if len(values) == 2 and values[0] != values[1]:
        raise CharacterScopeError("invalid_project_ownership")
    return values[0] if values else None


async def guard_session_project_tx(conn, session_id, project_id):
    """Called inside the same transaction as metadata changes, import or binding."""
    if not os.getenv("KIWI_CHARACTER_ID"):
        return
    await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 1703))", session_id)
    if project_id is not None and not await conn.fetchval("SELECT 1 FROM chat_projects WHERE id=$1 FOR KEY SHARE", project_id):
        raise CharacterScopeError("invalid_project_ownership")
    bound = await conn.fetchrow("SELECT project_id FROM kiwi_character_sessions WHERE session_id=$1", session_id)
    metadata = await conn.fetchrow("SELECT project_id FROM chat_conversations WHERE id=$1", session_id)
    history = await conn.fetch("SELECT DISTINCT project_id FROM conversations WHERE session_id=$1 AND scope_known=TRUE", session_id)
    evidence = [row for row in (bound, metadata) if row is not None] + list(history)
    if any(row["project_id"] != project_id for row in evidence):
        raise CharacterScopeError("session_project_mismatch")
    if bound is None:
        await conn.execute("INSERT INTO kiwi_character_sessions(session_id,project_id) VALUES ($1,$2)", session_id, project_id)


async def bind_session_project(session_id, project_id):
    from database import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        try:
            async with conn.transaction():
                await guard_session_project_tx(conn, session_id, project_id)
        except CharacterScopeError:
            return False
    return True
