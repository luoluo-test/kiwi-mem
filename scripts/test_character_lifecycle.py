"""Supervisor lifecycle guards: local HTTP and simulated database failures, no PostgreSQL."""
import asyncio
import os
import subprocess
import sys
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch

import asyncpg
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from character_gateway import CharacterError, Registry, WorkerManager, create_app


ROW = {"id": "A", "name": "A", "state": "active", "database_name": "unused"}


class Connection:
    def __init__(self):
        self.closed = False
        self.fail_once = False
        self.list_calls = 0
        self.listeners = []

    def add_termination_listener(self, listener):
        self.listeners.append(listener)

    def terminate(self):
        self.closed = True
        for listener in self.listeners:
            listener(self)

    def is_closed(self):
        return self.closed

    async def fetch(self, *_):
        self.list_calls += 1
        if self.closed:
            raise asyncpg.InterfaceError("connection is closed")
        if self.fail_once:
            self.fail_once = False
            raise asyncpg.QueryCanceledError("simulated transient query cancellation")
        return [ROW]

    async def fetchrow(self, *_):
        if self.closed:
            raise asyncpg.InterfaceError("connection is closed")
        return ROW

    async def close(self):
        self.terminate()


class TestRegistry(Registry):
    def __init__(self):
        super().__init__("postgresql://localhost/unused")
        self.conn = Connection()

    async def open(self):
        self.conn.add_termination_listener(self._connection_terminated)


class Process:
    returncode = None

    def terminate(self):
        self.returncode = 0

    async def wait(self):
        return self.returncode


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def set_health_response(self, manager):
        await manager.health_client.aclose()
        manager.health_client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"character_id": "A"})))

    async def test_full_business_pool_cannot_starve_heartbeats(self):
        release = asyncio.Event()
        ready = asyncio.Event()
        handlers = set()

        async def serve(reader, writer):
            handlers.add(asyncio.current_task())
            try:
                request = await reader.readuntil(b"\r\n\r\n")
                if request.split(b" ")[1] == b"/_character/ready":
                    ready.set()
                    writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}")
                else:
                    writer.write(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n")
                await writer.drain()
                await release.wait()
            finally:
                writer.close()
                await writer.wait_closed()
                handlers.discard(asyncio.current_task())

        server = await asyncio.start_server(serve, "127.0.0.1", 0, backlog=200)
        base = "http://127.0.0.1:" + str(server.sockets[0].getsockname()[1])
        manager = WorkerManager(TestRegistry())
        manager.workers["A"] = {"url": base, "token": "test", "process": Process()}
        responses, pulse = [], None
        try:
            responses = await asyncio.gather(*(manager.client.send(
                manager.client.build_request("GET", base + "/stream"), stream=True) for _ in range(100)))
            pulse = asyncio.create_task(manager.heartbeat())
            try:
                await asyncio.wait_for(ready.wait(), 2)
            except asyncio.TimeoutError:
                self.fail("100 business streams prevented the healthy worker receiving its heartbeat")
        finally:
            if pulse:
                pulse.cancel()
                await asyncio.gather(pulse, return_exceptions=True)
            await asyncio.gather(*(r.aclose() for r in responses))
            release.set()
            await manager.close()
            server.close()
            await server.wait_closed()
            await asyncio.gather(*list(handlers), return_exceptions=True)

    async def test_startup_probes_do_not_use_business_client(self):
        manager = WorkerManager(TestRegistry())
        process = Process()
        await self.set_health_response(manager)
        await manager.client.aclose()

        def business_request(_):
            self.fail("worker startup probe used the business connection pool")

        manager.client = httpx.AsyncClient(transport=httpx.MockTransport(business_request))
        try:
            with patch("character_gateway.asyncio.create_subprocess_exec", return_value=process):
                worker = await manager.get("A")
            self.assertIs(worker["process"], process)
        finally:
            await manager.close()

    async def test_registry_disconnect_stops_workers_before_shutdown(self):
        registry = TestRegistry()
        manager = WorkerManager(registry)
        process = Process()
        manager.workers["A"] = {"url": "http://worker.test", "token": "test", "process": process}
        await self.set_health_response(manager)
        stopped = asyncio.Event()

        def shutdown():
            self.assertIsNotNone(process.returncode)
            self.assertTrue(manager.health_client.is_closed)
            self.assertTrue(manager.client.is_closed)
            stopped.set()

        app = create_app(registry, manager, shutdown=shutdown)
        async with app.router.lifespan_context(app):
            registry.conn.terminate()
            await asyncio.wait_for(stopped.wait(), 2)
            self.assertTrue(app.state.character_failed)
            self.assertEqual(manager.workers, {})
            with self.assertRaises(CharacterError) as caught:
                await manager.get("A")
            self.assertEqual(caught.exception.status, 503)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                                         base_url="http://test") as client:
                response = await client.get("/characters/A")
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.json()["code"], "character_service_unavailable")

    async def test_shutdown_waits_for_in_progress_worker_start(self):
        registry = TestRegistry()
        manager = WorkerManager(registry)
        process = Process()
        await self.set_health_response(manager)
        started, release = asyncio.Event(), asyncio.Event()

        async def spawn(*_, **__):
            started.set()
            await release.wait()
            return process

        with patch("character_gateway.asyncio.create_subprocess_exec", side_effect=spawn):
            start = asyncio.create_task(manager.get("A"))
            await started.wait()
            close = asyncio.create_task(manager.close())
            await asyncio.sleep(0)
            release.set()
            result = await asyncio.gather(start, return_exceptions=True)
            await asyncio.wait_for(close, 2)
        self.assertIsInstance(result[0], CharacterError)
        self.assertIsNotNone(process.returncode)
        self.assertEqual(manager.workers, {})

    async def test_unexpected_heartbeat_failure_is_supervised(self):
        registry = TestRegistry()
        manager = WorkerManager(registry)
        process = Process()
        manager.workers["A"] = {"url": "http://worker.test", "token": "test", "process": process}
        crash, stopped = asyncio.Event(), asyncio.Event()

        async def heartbeat():
            await crash.wait()
            raise RuntimeError("simulated unexpected failure")

        with patch.object(manager, "heartbeat", heartbeat):
            app = create_app(registry, manager, shutdown=stopped.set)
            async with app.router.lifespan_context(app):
                crash.set()
                await asyncio.wait_for(stopped.wait(), 2)
                self.assertIsNotNone(process.returncode)
                self.assertTrue(manager.client.is_closed)

    async def test_overlapping_shutdown_does_not_lose_live_worker(self):
        class SlowProcess(Process):
            def __init__(self):
                self.returncode = None
                self.terminated, self.exited = asyncio.Event(), asyncio.Event()

            def terminate(self):
                self.terminated.set()

            def kill(self):
                self.returncode = -9
                self.exited.set()

            async def wait(self):
                await self.exited.wait()
                return self.returncode

        registry = TestRegistry()
        manager = WorkerManager(registry)
        process = SlowProcess()
        manager.workers["A"] = {"url": "http://worker.test", "token": "test", "process": process}
        await self.set_health_response(manager)
        app = create_app(registry, manager, shutdown=lambda: None)
        try:
            async with app.router.lifespan_context(app):
                registry.conn.terminate()
                await asyncio.wait_for(process.terminated.wait(), 2)
                # Normal server shutdown cancels the fatal supervisor while its
                # first stop attempt is still waiting for a slow worker to exit.
            self.assertIsNotNone(process.returncode, "shutdown returned with an untracked live worker")
            self.assertEqual(manager.workers, {})
        finally:
            process.kill()

    async def test_real_uvicorn_process_exits_after_registry_disconnect(self):
        result = await asyncio.to_thread(subprocess.run,
            [sys.executable, "-B", str(Path(__file__).resolve()), "--server-fixture"],
            capture_output=True, text=True, timeout=15,
            creationflags=0x08000000 if os.name == "nt" else 0)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("FIXTURE_WORKER_STOPPED", result.stdout)
        self.assertIn("event=character_supervisor_stopped", result.stdout)

    async def test_transient_registry_query_does_not_end_heartbeats(self):
        registry = TestRegistry()
        registry.conn.fail_once = True
        manager = WorkerManager(registry)
        manager.workers["A"] = {"url": "http://worker.test", "token": "test", "process": Process()}
        calls = 0

        async def reply(_):
            nonlocal calls
            calls += 1
            return httpx.Response(200, json={"character_id": "A"})

        await manager.health_client.aclose()
        manager.health_client = httpx.AsyncClient(transport=httpx.MockTransport(reply))
        pulse = asyncio.create_task(manager.heartbeat())
        try:
            await asyncio.sleep(5.2)
            self.assertFalse(pulse.done(), "one transient registry query permanently ended the heartbeat")
            self.assertGreaterEqual(calls, 2)
            self.assertGreaterEqual(registry.conn.list_calls, 2)
        finally:
            pulse.cancel()
            await asyncio.gather(pulse, return_exceptions=True)
            await manager.close()


def run_server_fixture():
    """Real Uvicorn and signal handling; registry/process fixtures never touch a DB."""
    import uvicorn

    class ObservedProcess(Process):
        def terminate(self):
            super().terminate()
            print("FIXTURE_WORKER_STOPPED", flush=True)

    registry = TestRegistry()
    manager = WorkerManager(registry)
    manager.workers["A"] = {"url": "http://worker.test", "token": "test", "process": ObservedProcess()}
    app = create_app(registry, manager)
    original = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        await manager.health_client.aclose()
        manager.health_client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"character_id": "A"})))
        async with original(application):
            async def disconnect():
                await asyncio.sleep(0.1)
                registry.conn.terminate()
            task = asyncio.create_task(disconnect())
            try:
                yield
            finally:
                await task

    app.router.lifespan_context = lifespan
    uvicorn.run(app, host="127.0.0.1", port=0, access_log=False, log_level="critical")


if __name__ == "__main__":
    if "--server-fixture" in sys.argv:
        run_server_fixture()
    else:
        unittest.main()
