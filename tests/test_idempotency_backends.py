"""
Tests for durable idempotency backends — SQLite, Redis (mocked), Postgres (mocked).
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from wire.core.idempotency import IdempotencyGuard, IdempotencyRecord
from wire.core.idempotency_backends import (
    MemoryBackend, SQLiteBackend, RedisBackend, PostgresBackend,
)


# ── Helper ────────────────────────────────────────────────────────────────────

async def _exec(guard: IdempotencyGuard, tool: str, args: dict, val: str) -> tuple:
    calls = []
    async def fn():
        calls.append(val)
        return val
    key = IdempotencyGuard.make_key(tool, args)
    result, dup = await guard.call(key=key, fn=fn, run_id="r1", tool=tool)
    return result, dup, calls


# ── MemoryBackend ─────────────────────────────────────────────────────────────

class TestMemoryBackend:
    @pytest.mark.asyncio
    async def test_get_returns_none_for_missing(self) -> None:
        b = MemoryBackend()
        assert await b.get("missing") is None

    @pytest.mark.asyncio
    async def test_set_and_get_roundtrip(self) -> None:
        from datetime import datetime, timezone
        b = MemoryBackend()
        rec = IdempotencyRecord(key="k", run_id="r", tool="t", result="ok")
        await b.set("k", rec)
        got = await b.get("k")
        assert got is not None
        assert got.tool == "t"
        assert got.result == "ok"

    @pytest.mark.asyncio
    async def test_exists(self) -> None:
        b = MemoryBackend()
        assert not await b.exists("k")
        rec = IdempotencyRecord(key="k", run_id="r", tool="t", result="x")
        await b.set("k", rec)
        assert await b.exists("k")

    @pytest.mark.asyncio
    async def test_delete(self) -> None:
        b = MemoryBackend()
        rec = IdempotencyRecord(key="k", run_id="r", tool="t", result="x")
        await b.set("k", rec)
        await b.delete("k")
        assert not await b.exists("k")

    @pytest.mark.asyncio
    async def test_clear(self) -> None:
        b = MemoryBackend()
        for i in range(5):
            await b.set(str(i), IdempotencyRecord(key=str(i), run_id="r", tool="t", result=i))
        await b.clear()
        assert len(b) == 0

    @pytest.mark.asyncio
    async def test_increment(self) -> None:
        b = MemoryBackend()
        rec = IdempotencyRecord(key="k", run_id="r", tool="t", result="x")
        await b.set("k", rec)
        count = await b.increment("k")
        assert count == 2


# ── IdempotencyGuard with MemoryBackend ───────────────────────────────────────

class TestIdempotencyGuardMemory:
    @pytest.mark.asyncio
    async def test_first_call_executes(self) -> None:
        guard = IdempotencyGuard()
        result, dup, calls = await _exec(guard, "tool", {"x": 1}, "val")
        assert result == "val"
        assert dup is False
        assert calls == ["val"]

    @pytest.mark.asyncio
    async def test_second_call_deduplicated(self) -> None:
        guard = IdempotencyGuard()
        await _exec(guard, "tool", {"x": 1}, "first")
        result, dup, calls = await _exec(guard, "tool", {"x": 1}, "second")
        assert result == "first"
        assert dup is True
        assert calls == []  # fn never called

    @pytest.mark.asyncio
    async def test_different_args_both_execute(self) -> None:
        guard = IdempotencyGuard()
        _, d1, c1 = await _exec(guard, "tool", {"x": 1}, "a")
        _, d2, c2 = await _exec(guard, "tool", {"x": 2}, "b")
        assert not d1 and not d2
        assert c1 == ["a"] and c2 == ["b"]

    @pytest.mark.asyncio
    async def test_clear_allows_re_execution(self) -> None:
        guard = IdempotencyGuard()
        await _exec(guard, "tool", {"x": 1}, "first")
        key = IdempotencyGuard.make_key("tool", {"x": 1})
        await guard.clear(key)
        _, dup, calls = await _exec(guard, "tool", {"x": 1}, "second")
        assert dup is False
        assert calls == ["second"]

    @pytest.mark.asyncio
    async def test_backend_name(self) -> None:
        guard = IdempotencyGuard()
        assert guard.backend_name == "MemoryBackend"


# ── SQLiteBackend ─────────────────────────────────────────────────────────────

class TestSQLiteBackend:
    @pytest.mark.asyncio
    async def test_set_and_get(self, tmp_path: Path) -> None:
        b = SQLiteBackend(str(tmp_path / "idem.db"))
        rec = IdempotencyRecord(key="k1", run_id="r1", tool="jira_create", result={"id": 42})
        await b.set("k1", rec)
        got = await b.get("k1")
        assert got is not None
        assert got.tool == "jira_create"
        assert got.result == {"id": 42}
        await b.close()

    @pytest.mark.asyncio
    async def test_survives_close_and_reopen(self, tmp_path: Path) -> None:
        path = str(tmp_path / "idem.db")
        b1 = SQLiteBackend(path)
        rec = IdempotencyRecord(key="k1", run_id="r1", tool="t", result="persisted")
        await b1.set("k1", rec)
        await b1.close()
        # Reopen — should still find the record
        b2 = SQLiteBackend(path)
        got = await b2.get("k1")
        assert got is not None
        assert got.result == "persisted"
        await b2.close()

    @pytest.mark.asyncio
    async def test_guard_with_sqlite_backend(self, tmp_path: Path) -> None:
        backend = SQLiteBackend(str(tmp_path / "idem.db"))
        guard = IdempotencyGuard(backend=backend)
        result, dup, calls = await _exec(guard, "send_email", {"to": "a@b.com"}, "sent")
        assert result == "sent" and not dup
        # Second call — deduplicated via SQLite
        result2, dup2, calls2 = await _exec(guard, "send_email", {"to": "a@b.com"}, "again")
        assert result2 == "sent" and dup2 is True and calls2 == []
        await backend.close()

    @pytest.mark.asyncio
    async def test_ttl_expiry(self, tmp_path: Path) -> None:
        import time
        b = SQLiteBackend(str(tmp_path / "idem.db"), ttl_seconds=1)
        rec = IdempotencyRecord(key="k", run_id="r", tool="t", result="x")
        await b.set("k", rec)
        assert await b.exists("k")
        # Manually expire by waiting > TTL
        import asyncio
        await asyncio.sleep(1.1)
        got = await b.get("k")
        # Note: TTL check happens on get() — expired records return None
        assert got is None
        await b.close()

    @pytest.mark.asyncio
    async def test_increment(self, tmp_path: Path) -> None:
        b = SQLiteBackend(str(tmp_path / "idem.db"))
        rec = IdempotencyRecord(key="k", run_id="r", tool="t", result="x")
        await b.set("k", rec)
        count = await b.increment("k")
        assert count == 2
        await b.close()

    @pytest.mark.asyncio
    async def test_delete(self, tmp_path: Path) -> None:
        b = SQLiteBackend(str(tmp_path / "idem.db"))
        rec = IdempotencyRecord(key="k", run_id="r", tool="t", result="x")
        await b.set("k", rec)
        await b.delete("k")
        assert not await b.exists("k")
        await b.close()

    @pytest.mark.asyncio
    async def test_clear(self, tmp_path: Path) -> None:
        b = SQLiteBackend(str(tmp_path / "idem.db"))
        for i in range(5):
            await b.set(str(i), IdempotencyRecord(key=str(i), run_id="r", tool="t", result=i))
        await b.clear()
        for i in range(5):
            assert not await b.exists(str(i))
        await b.close()

    @pytest.mark.asyncio
    async def test_backend_name_on_guard(self, tmp_path: Path) -> None:
        backend = SQLiteBackend(str(tmp_path / "idem.db"))
        guard = IdempotencyGuard(backend=backend)
        assert guard.backend_name == "SQLiteBackend"
        await backend.close()


# ── RedisBackend (mocked — no real Redis required) ────────────────────────────

class TestRedisBackend:
    def _make_mock_redis(self, store: dict | None = None) -> MagicMock:
        """Build a mock redis async client backed by a real dict."""
        data = store if store is not None else {}
        client = AsyncMock()
        client.get = AsyncMock(side_effect=lambda k: data.get(k))
        async def _set(k, v, ex=None): data[k] = v
        client.set = AsyncMock(side_effect=_set)
        async def _delete(*keys):
            for k in keys: data.pop(k, None)
        client.delete = AsyncMock(side_effect=_delete)
        async def _exists(k): return 1 if k in data else 0
        client.exists = AsyncMock(side_effect=_exists)
        async def _keys(pattern): return [k for k in data if k.startswith(pattern[:-1])]
        client.keys = AsyncMock(side_effect=_keys)
        client.aclose = AsyncMock()
        return client, data

_redis_available = True
try:
    import redis.asyncio  # noqa: F401
except ImportError:
    _redis_available = False

skip_no_redis = pytest.mark.skipif(not _redis_available, reason="redis not installed")


# ── RedisBackend (mocked — no real Redis required) ────────────────────────────

class TestRedisBackend:
    def _make_mock_redis(self, store: dict | None = None) -> MagicMock:
        """Build a mock redis async client backed by a real dict."""
        data = store if store is not None else {}
        client = AsyncMock()
        client.get = AsyncMock(side_effect=lambda k: data.get(k))
        async def _set(k, v, ex=None): data[k] = v
        client.set = AsyncMock(side_effect=_set)
        async def _delete(*keys):
            for k in keys: data.pop(k, None)
        client.delete = AsyncMock(side_effect=_delete)
        async def _exists(k): return 1 if k in data else 0
        client.exists = AsyncMock(side_effect=_exists)
        async def _keys(pattern): return [k for k in data if k.startswith(pattern[:-1])]
        client.keys = AsyncMock(side_effect=_keys)
        client.aclose = AsyncMock()
        return client, data

    @skip_no_redis
    @pytest.mark.asyncio
    async def test_set_and_get(self) -> None:
        mock_client, store = self._make_mock_redis()
        b = RedisBackend()
        b._client = mock_client
        rec = IdempotencyRecord(key="k", run_id="r", tool="t", result="ok")
        await b.set("k", rec)
        got = await b.get("k")
        assert got is not None
        assert got.result == "ok"

    @skip_no_redis
    @pytest.mark.asyncio
    async def test_dedup_across_process_via_redis(self) -> None:
        shared_store: dict = {}
        mock1, _ = self._make_mock_redis(shared_store)
        mock2, _ = self._make_mock_redis(shared_store)
        b1, b2 = RedisBackend(), RedisBackend()
        b1._client, b2._client = mock1, mock2

        guard1 = IdempotencyGuard(backend=b1)
        guard2 = IdempotencyGuard(backend=b2)

        calls = []
        async def fn(): calls.append("ran"); return "result"
        key = IdempotencyGuard.make_key("payment", {"amount": 100})
        await guard1.call(key=key, fn=fn, run_id="r1", tool="payment")

        result, dup, _ = await _exec(guard2, "payment", {"amount": 100}, "again")
        assert dup is True
        assert result == "result"
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_raises_without_redis_package(self) -> None:
        b = RedisBackend()
        with patch.dict(sys.modules, {"redis": None, "redis.asyncio": None}):
            with pytest.raises(ImportError) as exc_info:
                await b._conn()
            assert "redis" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_backend_name(self) -> None:
        guard = IdempotencyGuard(backend=RedisBackend())
        assert guard.backend_name == "RedisBackend"


# ── PostgresBackend (mocked — no real PG required) ────────────────────────────

class TestPostgresBackend:
    @pytest.mark.asyncio
    async def test_raises_without_asyncpg(self) -> None:
        b = PostgresBackend(dsn="postgresql://localhost/test")
        with patch.dict(sys.modules, {"asyncpg": None}):
            with pytest.raises(ImportError) as exc_info:
                await b._conn()
            assert "asyncpg" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_tenant_isolation(self) -> None:
        """Two tenants sharing a PG instance cannot see each other's records."""
        shared: dict[tuple, dict] = {}

        async def pg_fetchrow(sql, key, tenant_id, *args):
            return shared.get((key, tenant_id))

        async def pg_execute(sql, *args):
            if "INSERT" in sql:
                key, tenant_id = args[0], args[1]
                import json as _json
                shared[(key, tenant_id)] = {
                    "key": key, "tenant_id": tenant_id,
                    "run_id": args[2], "tool": args[3],
                    "result": args[4], "executed_at": args[5],
                    "call_count": args[6], "expires_at": args[7],
                }

        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(side_effect=pg_fetchrow)
        mock_conn.execute = AsyncMock(side_effect=pg_execute)

        mock_pool = AsyncMock()
        mock_pool.acquire = MagicMock(return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_conn),
            __aexit__=AsyncMock(return_value=None),
        ))

        b_a = PostgresBackend(dsn="pg://", tenant_id="team-a")
        b_b = PostgresBackend(dsn="pg://", tenant_id="team-b")
        b_a._pool = mock_pool
        b_b._pool = mock_pool

        rec = IdempotencyRecord(key="k", run_id="r", tool="t", result="x")
        await b_a.set("k", rec)

        # team-b should NOT see team-a's record
        result = await b_b.get("k")
        assert result is None

    def test_backend_name(self) -> None:
        guard = IdempotencyGuard(backend=PostgresBackend(dsn="pg://"))
        assert guard.backend_name == "PostgresBackend"


# ── Backend protocol conformance ──────────────────────────────────────────────

class TestBackendProtocol:
    def test_memory_implements_protocol(self) -> None:
        from wire.core.idempotency_backends import IdempotencyStore
        b = MemoryBackend()
        assert isinstance(b, IdempotencyStore)

    def test_sqlite_implements_protocol(self, tmp_path: Path) -> None:
        from wire.core.idempotency_backends import IdempotencyStore
        b = SQLiteBackend(str(tmp_path / "x.db"))
        assert isinstance(b, IdempotencyStore)

    def test_redis_implements_protocol(self) -> None:
        from wire.core.idempotency_backends import IdempotencyStore
        b = RedisBackend()
        assert isinstance(b, IdempotencyStore)

    def test_postgres_implements_protocol(self) -> None:
        from wire.core.idempotency_backends import IdempotencyStore
        b = PostgresBackend(dsn="pg://")
        assert isinstance(b, IdempotencyStore)

    def test_all_exported_from_wire(self) -> None:
        import wire
        assert hasattr(wire, "MemoryBackend")
        assert hasattr(wire, "SQLiteBackend")
        assert hasattr(wire, "RedisBackend")
        assert hasattr(wire, "PostgresBackend")
        assert hasattr(wire, "IdempotencyStore")
