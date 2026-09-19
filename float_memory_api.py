"""Versioned, character-only memory adapter. No chat/model gateway side effects.

The receipt survives memory deletion: a delayed event retry cannot resurrect it.
All SQL uses the worker's fixed database; projects are deliberately unsupported.
"""
import hashlib
import json
import os

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

router = APIRouter(prefix="/memory-adapter")


class Event(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_id: str = Field(min_length=1, max_length=256)
    session_id: str = Field(min_length=1, max_length=256)
    message_id: str = Field(min_length=1, max_length=256)
    source: str = Field(min_length=1, max_length=64)
    occurred_at: str = Field(min_length=1, max_length=64)
    content: str = Field(min_length=1, max_length=30000)
    visibility: str = Field(pattern="^(private|group)$")
    importance: int = Field(default=5, ge=1, le=10)


async def initialize():
    from database import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""CREATE TABLE IF NOT EXISTS float_memory_events (
            event_id TEXT PRIMARY KEY, payload_hash TEXT NOT NULL,
            session_id TEXT NOT NULL, message_id TEXT NOT NULL,
            source TEXT NOT NULL, occurred_at TEXT NOT NULL,
            visibility TEXT NOT NULL CHECK (visibility IN ('private','group')),
            memory_id INTEGER NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )""")


async def require_character():
    if not os.getenv("KIWI_CHARACTER_ID"):
        raise HTTPException(409, "character_isolation_required")
    from config import get_config_bool
    if not await get_config_bool("memory_enabled", fallback=True):
        raise HTTPException(409, "memory_disabled")


@router.get("/capabilities")
async def capabilities():
    await require_character()
    return {"contract": "float-memory-v1", "character_id": os.environ["KIWI_CHARACTER_ID"],
            "idempotent_events": True, "global_only": True, "group_session_recall": True}


@router.post("/events")
async def ingest(event: Event):
    await require_character()
    from database import get_pool, get_embedding, _insert_memory_tx
    payload_hash = hashlib.sha256(json.dumps(event.model_dump(), sort_keys=True,
                                             ensure_ascii=False).encode()).hexdigest()
    pool = await get_pool()
    # An early receipt check avoids another embedding call on ordinary retries.
    async with pool.acquire() as conn:
        previous = await conn.fetchrow("SELECT * FROM float_memory_events WHERE event_id=$1", event.event_id)
    if previous:
        if previous["payload_hash"] != payload_hash:
            raise HTTPException(409, "event_payload_conflict")
        return {"status": "duplicate", "memory_id": previous["memory_id"], "event_id": event.event_id}
    embedding = await get_embedding(event.content)
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 1801))", event.event_id)
            previous = await conn.fetchrow("SELECT * FROM float_memory_events WHERE event_id=$1", event.event_id)
            if previous:
                if previous["payload_hash"] != payload_hash:
                    raise HTTPException(409, "event_payload_conflict")
                return {"status": "duplicate", "memory_id": previous["memory_id"], "event_id": event.event_id}
            memory_id = await _insert_memory_tx(conn, content=event.content, importance=event.importance,
                source_session=event.session_id, title=event.source, source="float_event", embedding=embedding)
            await conn.execute("""INSERT INTO float_memory_events
                (event_id,payload_hash,session_id,message_id,source,occurred_at,visibility,memory_id)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8)""", event.event_id, payload_hash,
                event.session_id, event.message_id, event.source, event.occurred_at, event.visibility, memory_id)
    return {"status": "stored", "memory_id": memory_id, "event_id": event.event_id}


@router.get("/recall")
async def recall(q: str = "", limit: int = 30, group_session_id: str | None = None):
    await require_character()
    from database import get_pool, search_memories, get_recent_memories
    limit = max(1, min(limit, 100))
    if group_session_id:
        # Only the exact shared event, never private/derived/profile/project content.
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("""SELECT m.id,m.content,m.importance,m.created_at
                FROM memories m JOIN float_memory_events e ON e.memory_id=m.id
                WHERE e.visibility='group' AND e.session_id=$1 AND m.project_id IS NULL
                AND COALESCE(m.memory_type,'fragment') NOT IN ('digested','dream_deleted')
                AND (m.valid_until IS NULL OR m.valid_until>now())
                ORDER BY m.created_at DESC LIMIT $2""", group_session_id, limit)
    elif q.strip():
        rows = await search_memories(q[:4000], limit=limit, track_recall=False, project_id=None)
    else:
        rows = await get_recent_memories(limit=limit, global_only=True)
    return {"memories": [{"id": r["id"], "content": r["content"], "importance": r["importance"],
                           "created_at": str(r["created_at"])} for r in rows]}
