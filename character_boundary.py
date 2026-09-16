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
    from database import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS kiwi_character_sessions (
                session_id TEXT PRIMARY KEY, project_id TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)


async def bind_session_project(session_id, project_id):
    """A worker has one role; a session is permanently bound to one project scope.

    Keep bindings after deletion, just like tombstones. Existing known ledger
    identity is evidence too, including pre-upgrade conversations without metadata.
    """
    from database import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 1703))", session_id)
            old = await conn.fetchrow("SELECT project_id FROM kiwi_character_sessions WHERE session_id=$1", session_id)
            if old is not None:
                return old["project_id"] == project_id
            history = await conn.fetch("""
                SELECT DISTINCT project_id FROM conversations
                WHERE session_id=$1 AND scope_known=TRUE LIMIT 2
            """, session_id)
            if any(row["project_id"] != project_id for row in history):
                return False
            await conn.execute("INSERT INTO kiwi_character_sessions(session_id,project_id) VALUES ($1,$2)",
                               session_id, project_id)
            return True
