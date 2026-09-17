"""Project-aware memory tools inside an already isolated character worker."""
import json
import os

MEMORY_TOOLS = {"search_memory", "save_memory", "get_recent", "lock_memory", "unlock_memory"}


async def execute_memory_tool(name, args, scope=None):
    import database as db
    mode = (scope or {}).get("context_mode", "global")
    project = (scope or {}).get("context_project_id") if mode == "live_project" else None
    if mode not in ("global", "live_project") or (mode == "live_project" and not project):
        return '[tool_error] {"code":"invalid_project_ownership"}'
    allowed = {
        "search_memory": {"query", "limit"}, "get_recent": {"limit"},
        "save_memory": {"content", "title", "importance"},
        "lock_memory": {"memory_id"}, "unlock_memory": {"memory_id"},
    }
    if name not in allowed or set(args) - allowed[name]:
        return '[tool_error] {"code":"invalid_tool_arguments"}'
    pool = await db.get_pool()
    if project:
        async with pool.acquire() as conn:
            if not await conn.fetchval("SELECT 1 FROM chat_projects WHERE id=$1", project):
                return '[tool_error] {"code":"invalid_project_ownership"}'
    if name == "search_memory":
        rows = await db.search_memories(args["query"], limit=max(1, min(int(args.get("limit", 10)), 50)),
                                        project_id=project)
        return json.dumps([dict(r) for r in rows], ensure_ascii=False, default=str)
    if name == "get_recent":
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT id,title,content,importance FROM memories
                WHERE (project_id IS NULL OR project_id=$1)
                  AND COALESCE(memory_type,'fragment') NOT IN ('digested','dream_deleted')
                  AND (valid_until IS NULL OR valid_until>now())
                ORDER BY created_at DESC LIMIT $2
            """, project, max(1, min(int(args.get("limit", 20)), 50)))
        return json.dumps([dict(r) for r in rows], ensure_ascii=False)
    if name == "save_memory":
        content = args.get("content")
        if not isinstance(content, str) or not content.strip():
            return '[tool_error] {"code":"invalid_content"}'
        mid = await db.save_memory(content, title=args.get("title", ""),
                                   importance=max(1, min(int(args.get("importance", 5)), 10)),
                                   source="user_explicit", source_session="tool", project_id=project)
        return json.dumps({"id": mid, "status": "saved"})
    async with pool.acquire() as conn:
        # Mutating tools never change the shared foundation from a project context.
        result = await conn.execute("""
            UPDATE memories SET is_permanent=$2, lock_source=$3
            WHERE id=$1 AND project_id IS NOT DISTINCT FROM $4
        """, int(args["memory_id"]), name == "lock_memory",
            "user" if name == "lock_memory" else None, project)
    return json.dumps({"status": "updated"} if result != "UPDATE 0" else {"code": "memory_not_found"})


# These stores have no project column. Project models may read the shared
# foundation, but may not write private project facts back into it.
GLOBAL_WRITE_TOOLS = frozenset({
    "save_calendar_page", "add_comment", "trigger_digest", "trigger_dream", "stop_dream",
    "_gateway_create_reminder", "_gateway_complete_reminder", "_gateway_delete_reminder",
})


def project_tool_allowed(name, scope):
    return not (os.getenv("KIWI_CHARACTER_ID") and
                (scope or {}).get("context_mode", "global") != "global" and
                name in GLOBAL_WRITE_TOOLS)
