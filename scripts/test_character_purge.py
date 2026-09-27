"""Permanent-delete HTTP and supervisor guards. Fakes are NOT PostgreSQL evidence."""
import asyncio
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from character_gateway import CharacterError, WorkerManager, create_app


class RegistryFixture:
    def __init__(self):
        self.rows = {cid: {"id": cid, "name": cid, "state": "active",
                          "database_name": "kiwi_char_" + ("a" if cid == "A" else "b") * 32,
                          "deletion_error": None} for cid in ("default", "A", "B")}
        self.events = []
        self.failure = None
        self.drop_started = asyncio.Event()
        self.drop_release = None

    def require_lease(self):
        pass

    async def list(self):
        return list(self.rows.values())

    async def get(self, cid):
        if cid not in self.rows:
            raise CharacterError("character_not_found", 404)
        row = self.rows[cid]
        if row["state"] in ("disabled", "deleted"):
            raise CharacterError("character_deleted", 410)
        if row["state"] == "deleting":
            raise CharacterError("character_deletion_in_progress", 409)
        return dict(row)

    async def create(self, cid, name):
        if cid in self.rows:
            raise CharacterError("character_exists", 409)
        raise AssertionError("Test must not create unrelated IDs")

    async def disable(self, cid):
        if cid == "default":
            raise CharacterError("default_character_protected", 409)
        row = await self.get(cid)
        self.rows[cid]["state"] = "disabled"
        return row

    async def begin_purge(self, cid):
        if cid == "default":
            raise CharacterError("default_character_protected", 409)
        if cid not in self.rows:
            raise CharacterError("character_not_found", 404)
        row = self.rows[cid]
        if row["state"] != "deleted":
            row["state"] = "deleting"
        self.events.append(("begin", cid))
        return dict(row)

    async def drop_character_database(self, row):
        cid = row["id"]
        self.events.append(("drop", cid))
        self.drop_started.set()
        if self.drop_release:
            await self.drop_release.wait()
        if self.failure:
            raise self.failure

    async def finish_purge(self, cid):
        self.events.append(("finish", cid))
        self.rows[cid].update(state="deleted", name="", deletion_error=None)

    async def record_purge_error(self, cid, code):
        self.rows[cid]["deletion_error"] = code


class ManagerFixture:
    def __init__(self, registry):
        self.registry = registry
        self.calls = []
        self.purges = []
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(self.respond))

    async def get(self, cid):
        await self.registry.get(cid)
        return {"url": "http://" + cid.lower() + ".test", "token": "fixture"}

    async def disable(self, cid):
        await self.registry.disable(cid)

    async def purge(self, cid):
        self.purges.append(cid)
        row = await self.registry.begin_purge(cid)
        if row["state"] != "deleted":
            await self.registry.drop_character_database(row)
            await self.registry.finish_purge(cid)

    async def respond(self, request):
        self.calls.append(request)
        return httpx.Response(200, json={"memories": []})


class PurgeHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.registry = RegistryFixture()
        self.manager = ManagerFixture(self.registry)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(
            app=create_app(self.registry, self.manager)), base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.manager.client.aclose()

    async def test_purge_is_registry_operation_and_repeatable(self):
        for _ in range(2):
            response = await self.client.post("/characters/A/purge", json={"confirm_character_id": "A"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {"status": "deleted", "data_retained": False})
        self.assertEqual(self.registry.rows["A"]["state"], "deleted")
        self.assertEqual(self.registry.rows["B"]["state"], "active")
        self.assertEqual(self.manager.calls, [], "purge must never be forwarded to a role worker")
        self.assertEqual(self.registry.events.count(("drop", "A")), 1)

    async def test_confirmation_is_required_exact_and_only_field(self):
        for body in ({}, {"confirm_character_id": "B"}, {"confirm_character_id": " A"},
                     {"confirm_character_id": None}, {"confirm_character_id": True},
                     {"confirm_character_id": "A", "database_name": "anything"},
                     {"confirm_character_id": "A", "force": True},
                     {"confirm_character_id": "A", "character_id": "A"}):
            with self.subTest(body=body):
                response = await self.client.post("/characters/A/purge", json=body)
                self.assertEqual(response.status_code, 400)
        for raw in (b"", b"null", b"[]", b"{"):
            response = await self.client.post("/characters/A/purge", content=raw,
                                              headers={"content-type": "application/json"})
            self.assertEqual(response.status_code, 400)
        self.assertEqual(self.manager.purges, [])
        self.assertEqual(self.manager.calls, [])

    async def test_all_selector_conflicts_fail_before_purge(self):
        cases = [({"confirm_character_id": "A"}, {"X-Kiwi-Character": "B"}),
                 ({"confirm_character_id": "A", "character_id": "B"}, {}),
                 ({"confirm_character_id": "A", "characterId": "B"}, {})]
        for body, headers in cases:
            response = await self.client.post("/characters/A/purge", json=body, headers=headers)
            self.assertEqual(response.status_code, 409)
        self.assertEqual(self.manager.purges, [])
        self.assertEqual(self.manager.calls, [])

    async def test_default_unknown_and_invalid_selectors_are_protected(self):
        for cid, status in (("default", 409), ("unknown", 404)):
            response = await self.client.post(f"/characters/{cid}/purge", json={"confirm_character_id": cid})
            self.assertEqual(response.status_code, status)
        for suffix, headers in (("?character_id=A", {}), ("", [("X-Kiwi-Character", "A"),
                                                           ("X-Kiwi-Character", "A")])):
            response = await self.client.post("/characters/A/purge" + suffix,
                json={"confirm_character_id": "A"}, headers=headers)
            self.assertEqual(response.status_code, 400)
        self.assertEqual(self.registry.rows["default"]["state"], "active")
        self.assertEqual(self.registry.rows["A"]["state"], "active")

    async def test_legacy_disable_retains_data_and_disabled_can_be_purged(self):
        response = await self.client.delete("/characters/A")
        self.assertEqual(response.json(), {"status": "disabled", "data_retained": True})
        self.assertEqual(self.registry.events, [])
        response = await self.client.post("/characters/A/purge", json={"confirm_character_id": "A"})
        self.assertEqual(response.json(), {"status": "deleted", "data_retained": False})

    async def test_deleted_and_deleting_cannot_reach_workers_or_reuse_id(self):
        for state, status in (("deleting", 409), ("deleted", 410)):
            self.registry.rows["A"]["state"] = state
            for path in ("/characters/A/debug/memories", "/characters/A/admin/", "/characters/A"):
                self.assertEqual((await self.client.get(path)).status_code, status)
            response = await self.client.post("/debug/memories", json={"character_id": "A"})
            self.assertEqual(response.status_code, status)
            response = await self.client.post("/characters", json={"id": "A", "name": "replacement"})
            self.assertEqual(response.status_code, 409)
        self.assertEqual(self.manager.calls, [])


class PurgeLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.registry = RegistryFixture()
        self.manager = WorkerManager(self.registry)
        async def stop(cid):
            self.registry.events.append(("stop", cid))
            self.manager.workers.pop(cid, None)
        self.manager._stop = AsyncMock(side_effect=stop)

    async def asyncTearDown(self):
        await self.manager.close()

    async def test_worker_stops_before_drop_and_tombstone_is_last(self):
        await self.manager.purge("A")
        self.assertEqual([event for event in self.registry.events if event[0] != "begin"],
                         [("stop", "A"), ("drop", "A"), ("finish", "A")])
        await self.manager.purge("A")
        self.assertEqual(self.registry.events.count(("drop", "A")), 1)

    async def test_failure_is_retryable_and_does_not_expose_exception(self):
        self.registry.failure = RuntimeError("postgresql://private-secret@db/private")
        with self.assertRaises(CharacterError) as caught:
            await self.manager.purge("A")
        self.assertGreaterEqual(caught.exception.status, 500)
        self.assertNotIn("private", caught.exception.code)
        self.assertEqual(self.registry.rows["A"]["state"], "deleting")
        self.assertIsNotNone(self.registry.rows["A"]["deletion_error"])
        self.assertNotIn("private", self.registry.rows["A"]["deletion_error"])
        self.assertNotIn(("finish", "A"), self.registry.events)
        self.registry.failure = None
        await self.manager.purge("A")
        self.assertEqual(self.registry.rows["A"]["state"], "deleted")
        self.assertIsNone(self.registry.rows["A"]["deletion_error"])

    async def test_concurrent_start_and_repeated_purge_cannot_revive_worker(self):
        self.registry.drop_release = asyncio.Event()
        first = asyncio.create_task(self.manager.purge("A"))
        try:
            await asyncio.wait_for(self.registry.drop_started.wait(), 2)
            with patch("character_gateway.asyncio.create_subprocess_exec", new_callable=AsyncMock) as spawn:
                with self.assertRaises(CharacterError) as caught:
                    await self.manager.get("A")
                self.assertEqual(caught.exception.code, "character_deletion_in_progress")
                spawn.assert_not_awaited()
                second = asyncio.create_task(self.manager.purge("A"))
                self.registry.drop_release.set()
                await asyncio.gather(first, second)
            self.assertEqual(self.registry.events.count(("drop", "A")), 1)
            self.assertEqual(self.registry.rows["B"]["state"], "active")
        finally:
            self.registry.drop_release.set()
            await asyncio.gather(first, return_exceptions=True)

    async def test_stop_failure_never_attempts_drop(self):
        self.manager._stop.side_effect = RuntimeError("worker stop failed")
        with self.assertRaises(CharacterError):
            await self.manager.purge("A")
        self.assertNotIn(("drop", "A"), self.registry.events)
        self.assertEqual(self.registry.rows["A"]["state"], "deleting")
        self.manager._stop.side_effect = None

    async def test_stale_parallel_purge_rechecks_tombstone_under_role_lock(self):
        self.registry.drop_release = asyncio.Event()
        stale_read, stale_release = asyncio.Event(), asyncio.Event()
        original_begin = self.registry.begin_purge

        async def begin(cid):
            row = await original_begin(cid)
            if asyncio.current_task().get_name() == "stale-request":
                stale_read.set()
                await stale_release.wait()
            return row

        self.registry.begin_purge = begin
        first = asyncio.create_task(self.manager.purge("A"))
        second = None
        try:
            await asyncio.wait_for(self.registry.drop_started.wait(), 2)
            second = asyncio.create_task(self.manager.purge("A"), name="stale-request")
            await asyncio.wait_for(stale_read.wait(), 2)
            self.registry.drop_release.set()
            await first
            await asyncio.sleep(0)  # Allow the first task's cleanup callback to run.
            stale_release.set()
            await second
            self.assertEqual(self.registry.events.count(("drop", "A")), 1)
            self.assertEqual(self.registry.events.count(("finish", "A")), 1)
        finally:
            self.registry.drop_release.set()
            stale_release.set()
            await asyncio.gather(first, *([second] if second else []), return_exceptions=True)

    async def test_waiting_for_startup_lock_already_blocks_new_business(self):
        lock = self.manager.locks.setdefault("A", asyncio.Lock())
        await lock.acquire()
        task = asyncio.create_task(self.manager.purge("A"))
        try:
            for _ in range(20):
                if self.registry.rows["A"]["state"] == "deleting":
                    break
                await asyncio.sleep(0)
            self.assertEqual(self.registry.rows["A"]["state"], "deleting")
            with self.assertRaises(CharacterError) as caught:
                await self.manager.get("A")
            self.assertEqual(caught.exception.status, 409)
            self.assertFalse(self.registry.drop_started.is_set())
        finally:
            lock.release()
            await task

    async def test_client_cancellation_does_not_cancel_durable_purge(self):
        self.registry.drop_release = asyncio.Event()
        request = asyncio.create_task(self.manager.purge("A"))
        try:
            await asyncio.wait_for(self.registry.drop_started.wait(), 2)
            request.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await request
            self.assertEqual(self.registry.rows["A"]["state"], "deleting")
            self.registry.drop_release.set()
            await asyncio.wait_for(self.manager.purge("A"), 2)
            self.assertEqual(self.registry.rows["A"]["state"], "deleted")
            self.assertEqual(self.registry.events.count(("drop", "A")), 1)
        finally:
            self.registry.drop_release.set()
            await asyncio.gather(request, return_exceptions=True)

    async def test_supervisor_close_leaves_interrupted_deletion_retryable(self):
        self.registry.drop_release = asyncio.Event()
        request = asyncio.create_task(self.manager.purge("A"))
        try:
            await asyncio.wait_for(self.registry.drop_started.wait(), 2)
            await asyncio.wait_for(self.manager.close(), 2)
            await asyncio.gather(request, return_exceptions=True)
            self.assertEqual(self.registry.rows["A"]["state"], "deleting")
            self.assertEqual(self.registry.rows["A"]["deletion_error"], "character_purge_interrupted")
            self.assertNotIn(("finish", "A"), self.registry.events)
        finally:
            self.registry.drop_release.set()
            await asyncio.gather(request, return_exceptions=True)

    async def test_heartbeat_recovery_failure_does_not_block_other_roles(self):
        self.registry.rows["A"]["state"] = "deleting"
        self.registry.failure = RuntimeError("unavailable database")
        self.manager.get = AsyncMock(return_value={})
        pulse = asyncio.create_task(self.manager.heartbeat())
        try:
            await asyncio.wait_for(self.registry.drop_started.wait(), 2)
            for _ in range(20):
                if self.registry.rows["A"]["deletion_error"]:
                    break
                await asyncio.sleep(0)
            self.manager.get.assert_any_await("B")
            self.manager.get.assert_any_await("default")
            self.assertEqual(self.registry.rows["A"]["state"], "deleting")
            self.assertIsNotNone(self.registry.rows["A"]["deletion_error"])
            self.assertFalse(pulse.done())
        finally:
            pulse.cancel()
            await asyncio.gather(pulse, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
