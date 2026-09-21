"""Real TCP supervisor + real workers/PostgreSQL; Node executes float's TS adapter.

Only a disposable localhost cluster is accepted. Provider HTTP is a fixture.
Usage: python scripts/test_float_memory_api.py /path/to/float
"""
import asyncio
import os
from pathlib import Path
import socket
import sys
import threading
import uuid
from unittest.mock import patch
from urllib.parse import urlsplit
from http.server import ThreadingHTTPServer

import asyncpg
import uvicorn
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from character_gateway import Registry, WorkerManager, create_app, database_url_for
from test_character_postgres import Provider


async def run():
    base = os.environ.get("KIWI_TEST_DATABASE_URL", "")
    if urlsplit(base).hostname not in ("127.0.0.1", "localhost", "::1"):
        raise SystemExit("BLOCKED: disposable localhost KIWI_TEST_DATABASE_URL required")
    float_path = Path(sys.argv[1]).resolve()
    if not (float_path / "scripts/check-kiwi-memory.mjs").is_file():
        raise SystemExit("float production adapter test missing")
    control = await asyncpg.connect(base)
    name = "acceptance_float_" + uuid.uuid4().hex
    await control.execute(f'CREATE DATABASE "{name}" TEMPLATE template0')
    registry = Registry(database_url_for(base, name))
    provider = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    threading.Thread(target=provider.serve_forever, daemon=True).start()
    sock = socket.socket(); sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    created = []
    server = task = None
    api = f"http://127.0.0.1:{provider.server_port}/v1/chat/completions"
    try:
        with patch.dict(os.environ, {"API_KEY": "test-only", "API_BASE_URL": api,
                "MEMORY_API_KEY": "test-only", "MEMORY_API_BASE_URL": api,
                "MEMORY_ENABLED": "true", "DEFAULT_MODEL": "mock", "MEMORY_MODEL": "mock", "PYTHONUTF8": "1"}):
            app = create_app(registry, WorkerManager(registry))
            server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
            task = asyncio.create_task(server.serve(sockets=[sock]))
            for _ in range(300):
                if server.started: break
                if task.done(): await task
                await asyncio.sleep(.1)
            assert server.started, "test supervisor did not start"
            proc = await asyncio.create_subprocess_exec("node", str(float_path / "scripts/check-kiwi-memory.mjs"),
                cwd=str(float_path), env={**os.environ, "KIWI_TEST_BASE_URL": f"http://127.0.0.1:{port}"})
            result = await asyncio.wait_for(proc.wait(), 180)
            assert result == 0, f"float test exit {result}"
    finally:
        if registry.conn and not registry.conn.is_closed():
            created = [r["database_name"] for r in await registry.list() if r["id"] != "default"]
        if server:
            server.should_exit = True
        if task:
            await task
        provider.shutdown(); provider.server_close(); sock.close()
        for db in [*created, name]:
            assert db == name or (db.startswith("kiwi_char_") and len(db) == 42)
            await control.execute(f'DROP DATABASE "{db}" WITH (FORCE)')
        assert await control.fetchval("SELECT count(*) FROM pg_database WHERE datname=ANY($1::text[])", [*created, name]) == 0
        await control.close()
        print("PASS: SQL verified disposable registry/role database cleanup", flush=True)


if __name__ == "__main__":
    asyncio.run(run())
