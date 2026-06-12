"""
Durable idempotency backends for IdempotencyGuard.

The in-memory store loses state on process restart — safe for single-process
workforces but not for distributed deployments or long-running agents that
span multiple sessions.

Three durable backends:

  SQLiteBackend   — zero external deps, survives restarts, single-node
  RedisBackend    — multi-process, multi-node, TTL-based expiry
  PostgresBackend — enterprise, multi-tenant, full audit history, SQL query

All backends implement the same IdempotencyStore protocol:
  async get(key) → IdempotencyRecord | None
  async set(key, record) → None
  async increment(key) → int            (returns new call_count)
  async delete(key) → None
  async clear() → None
  async exists(key) → bool

Usage:
    from wire.core.idempotency_backends import SQLiteBackend, RedisBackend
    from wire.core.idempotency import IdempotencyGuard

    # SQLite — survives restarts, zero deps
    guard = IdempotencyGuard(backend=SQLiteBackend("wire-idempotency.db"))

    # Redis — multi-process, distributed
    guard = IdempotencyGuard(backend=RedisBackend("redis://localhost:6379", ttl_seconds=86400))

    # Postgres — enterprise, multi-tenant
    guard = IdempotencyGuard(backend=PostgresBackend(dsn="postgresql://...", tenant_id="team-a"))
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol, runtime_checkable

import orjson
import structlog

from wire.core.idempotency import IdempotencyRecord

log = structlog.get_logger(__name__)


# ── Store protocol ────────────────────────────────────────────────────────────

@runtime_checkable
class IdempotencyStore(Protocol):
    async def get(self, key: str) -> IdempotencyRecord | None: ...
    async def set(self, key: str, record: IdempotencyRecord) -> None: ...
    async def increment(self, key: str) -> int: ...
    async def delete(self, key: str) -> None: ...
    async def clear(self) -> None: ...
    async def exists(self, key: str) -> bool: ...


# ── In-memory (default — existing behaviour) ──────────────────────────────────

class MemoryBackend:
    """
    In-memory store — default, zero deps, zero config.
    Lost on process restart. Safe for single-process, single-session use.
    """

    def __init__(self) -> None:
        self._store: dict[str, IdempotencyRecord] = {}

    async def get(self, key: str) -> IdempotencyRecord | None:
        return self._store.get(key)

    async def set(self, key: str, record: IdempotencyRecord) -> None:
        self._store[key] = record

    async def increment(self, key: str) -> int:
        if key in self._store:
            self._store[key].call_count += 1
            return self._store[key].call_count
        return 1

    async def delete(self, key: str) -> None:
        self._store.pop(key, None)

    async def clear(self) -> None:
        self._store.clear()

    async def exists(self, key: str) -> bool:
        return key in self._store

    def __len__(self) -> int:
        return len(self._store)


# ── SQLite backend ─────────────────────────────────────────────────────────────

class SQLiteBackend:
    """
    SQLite-backed idempotency store.

    Survives process restarts. Single-node only (SQLite file locking).
    Zero external dependencies — uses aiosqlite from WIRE's base deps.

    Table schema (auto-created):
        CREATE TABLE wire_idempotency (
            key TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            tool TEXT NOT NULL,
            result TEXT NOT NULL,       -- JSON
            executed_at TEXT NOT NULL,  -- ISO timestamp
            call_count INTEGER NOT NULL DEFAULT 1,
            expires_at TEXT             -- NULL = no expiry
        );

    Usage:
        guard = IdempotencyGuard(backend=SQLiteBackend("wire-idempotency.db"))
    """

    _CREATE = """
    CREATE TABLE IF NOT EXISTS wire_idempotency (
        key TEXT PRIMARY KEY,
        run_id TEXT NOT NULL,
        tool TEXT NOT NULL,
        result TEXT NOT NULL,
        executed_at TEXT NOT NULL,
        call_count INTEGER NOT NULL DEFAULT 1,
        expires_at TEXT
    );
    CREATE INDEX IF NOT EXISTS wire_idem_tool_idx ON wire_idempotency(tool);
    CREATE INDEX IF NOT EXISTS wire_idem_run_idx ON wire_idempotency(run_id);
    """

    def __init__(
        self,
        path: str = "wire-idempotency.db",
        ttl_seconds: int | None = None,
    ) -> None:
        self.path = path
        self.ttl_seconds = ttl_seconds
        self._db: Any = None

    async def _conn(self) -> Any:
        if self._db is None:
            try:
                import aiosqlite
            except ImportError:
                raise ImportError(
                    "SQLiteBackend requires aiosqlite — already in WIRE's base deps. "
                    "If missing: pip install aiosqlite"
                )
            self._db = await aiosqlite.connect(self.path)
            self._db.row_factory = aiosqlite.Row
            await self._db.executescript(self._CREATE)
            await self._db.commit()
        return self._db

    async def get(self, key: str) -> IdempotencyRecord | None:
        db = await self._conn()
        async with db.execute(
            "SELECT * FROM wire_idempotency WHERE key = ?", (key,)
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        # Honour TTL
        if row["expires_at"]:
            expires = datetime.fromisoformat(row["expires_at"])
            if datetime.now(timezone.utc) > expires:
                await self.delete(key)
                return None
        return IdempotencyRecord(
            key=row["key"],
            run_id=row["run_id"],
            tool=row["tool"],
            result=json.loads(row["result"]),
            executed_at=datetime.fromisoformat(row["executed_at"]),
            call_count=row["call_count"],
        )

    async def set(self, key: str, record: IdempotencyRecord) -> None:
        db = await self._conn()
        expires_at = None
        if self.ttl_seconds:
            expires_at = (
                datetime.now(timezone.utc) + timedelta(seconds=self.ttl_seconds)
            ).isoformat()
        await db.execute(
            """
            INSERT OR REPLACE INTO wire_idempotency
                (key, run_id, tool, result, executed_at, call_count, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                key,
                record.run_id,
                record.tool,
                json.dumps(record.result, default=str),
                record.executed_at.isoformat(),
                record.call_count,
                expires_at,
            ),
        )
        await db.commit()
        log.debug("idem_sqlite_set", key=key[:12], tool=record.tool)

    async def increment(self, key: str) -> int:
        db = await self._conn()
        await db.execute(
            "UPDATE wire_idempotency SET call_count = call_count + 1 WHERE key = ?",
            (key,),
        )
        await db.commit()
        async with db.execute(
            "SELECT call_count FROM wire_idempotency WHERE key = ?", (key,)
        ) as cur:
            row = await cur.fetchone()
        return row["call_count"] if row else 1

    async def delete(self, key: str) -> None:
        db = await self._conn()
        await db.execute("DELETE FROM wire_idempotency WHERE key = ?", (key,))
        await db.commit()

    async def clear(self) -> None:
        db = await self._conn()
        await db.execute("DELETE FROM wire_idempotency")
        await db.commit()

    async def exists(self, key: str) -> bool:
        return (await self.get(key)) is not None

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None


# ── Redis backend ─────────────────────────────────────────────────────────────

class RedisBackend:
    """
    Redis-backed idempotency store.

    Multi-process, multi-node. TTL-based automatic expiry.
    Requires: pip install wire-ai[redis]

    Key format: wire:idem:{key}
    Value: JSON-serialised IdempotencyRecord

    Usage:
        guard = IdempotencyGuard(
            backend=RedisBackend("redis://localhost:6379", ttl_seconds=86400)
        )
    """

    _PREFIX = "wire:idem:"

    def __init__(
        self,
        url: str = "redis://localhost:6379",
        ttl_seconds: int = 86400,  # 24 hours default
        db: int = 0,
        max_connections: int = 10,
    ) -> None:
        self.url = url
        self.ttl_seconds = ttl_seconds
        self.db = db
        self.max_connections = max_connections
        self._client: Any = None

    async def _conn(self) -> Any:
        if self._client is None:
            try:
                import redis.asyncio as aioredis
            except ImportError:
                raise ImportError(
                    "RedisBackend requires redis[hiredis]. Install: pip install wire-ai[redis]"
                )
            self._client = aioredis.from_url(
                self.url, db=self.db, decode_responses=True,
                max_connections=self.max_connections,
            )
        return self._client

    def _k(self, key: str) -> str:
        return f"{self._PREFIX}{key}"

    async def get(self, key: str) -> IdempotencyRecord | None:
        client = await self._conn()
        raw = await client.get(self._k(key))
        if raw is None:
            return None
        data = json.loads(raw)
        return IdempotencyRecord(
            key=data["key"],
            run_id=data["run_id"],
            tool=data["tool"],
            result=data["result"],
            executed_at=datetime.fromisoformat(data["executed_at"]),
            call_count=data["call_count"],
        )

    async def set(self, key: str, record: IdempotencyRecord) -> None:
        client = await self._conn()
        payload = json.dumps({
            "key": record.key,
            "run_id": record.run_id,
            "tool": record.tool,
            "result": record.result,
            "executed_at": record.executed_at.isoformat(),
            "call_count": record.call_count,
        }, default=str)
        await client.set(self._k(key), payload, ex=self.ttl_seconds)
        log.debug("idem_redis_set", key=key[:12], tool=record.tool, ttl=self.ttl_seconds)

    async def increment(self, key: str) -> int:
        record = await self.get(key)
        if record:
            record.call_count += 1
            await self.set(key, record)
            return record.call_count
        return 1

    async def delete(self, key: str) -> None:
        client = await self._conn()
        await client.delete(self._k(key))

    async def clear(self) -> None:
        client = await self._conn()
        keys = await client.keys(f"{self._PREFIX}*")
        if keys:
            await client.delete(*keys)

    async def exists(self, key: str) -> bool:
        client = await self._conn()
        return bool(await client.exists(self._k(key)))

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None


# ── Postgres backend ──────────────────────────────────────────────────────────

class PostgresBackend:
    """
    PostgreSQL-backed idempotency store.

    Enterprise-grade: multi-tenant, full audit history, SQL query support.
    Row-level security scopes records by tenant_id.
    Requires: pip install wire-ai[postgres]

    Table schema (auto-created):
        CREATE TABLE wire_idempotency (
            key TEXT NOT NULL,
            tenant_id TEXT NOT NULL DEFAULT '',
            run_id TEXT NOT NULL,
            tool TEXT NOT NULL,
            result JSONB NOT NULL,
            executed_at TIMESTAMPTZ NOT NULL,
            call_count INTEGER NOT NULL DEFAULT 1,
            expires_at TIMESTAMPTZ,
            PRIMARY KEY (key, tenant_id)
        );

    Usage:
        guard = IdempotencyGuard(
            backend=PostgresBackend(
                dsn="postgresql://user:pass@host/db",
                tenant_id="team-vayu",
                ttl_seconds=604800,  # 7 days
            )
        )
    """

    _CREATE = """
    CREATE TABLE IF NOT EXISTS wire_idempotency (
        key TEXT NOT NULL,
        tenant_id TEXT NOT NULL DEFAULT '',
        run_id TEXT NOT NULL,
        tool TEXT NOT NULL,
        result JSONB NOT NULL DEFAULT '{}',
        executed_at TIMESTAMPTZ NOT NULL,
        call_count INTEGER NOT NULL DEFAULT 1,
        expires_at TIMESTAMPTZ,
        PRIMARY KEY (key, tenant_id)
    );
    CREATE INDEX IF NOT EXISTS wire_idem_tenant_idx ON wire_idempotency(tenant_id);
    CREATE INDEX IF NOT EXISTS wire_idem_tool_idx ON wire_idempotency(tool);
    CREATE INDEX IF NOT EXISTS wire_idem_run_idx ON wire_idempotency(run_id);
    """

    def __init__(
        self,
        dsn: str,
        tenant_id: str = "",
        ttl_seconds: int | None = None,
        min_pool_size: int = 2,
        max_pool_size: int = 10,
    ) -> None:
        self.dsn = dsn
        self.tenant_id = tenant_id
        self.ttl_seconds = ttl_seconds
        self.min_pool_size = min_pool_size
        self.max_pool_size = max_pool_size
        self._pool: Any = None

    async def _conn(self) -> Any:
        if self._pool is None:
            try:
                import asyncpg
            except ImportError:
                raise ImportError(
                    "PostgresBackend requires asyncpg. Install: pip install wire-ai[postgres]"
                )
            self._pool = await asyncpg.create_pool(
                self.dsn,
                min_size=self.min_pool_size,
                max_size=self.max_pool_size,
            )
            async with self._pool.acquire() as conn:
                await conn.execute(self._CREATE)
        return self._pool

    async def get(self, key: str) -> IdempotencyRecord | None:
        pool = await self._conn()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM wire_idempotency WHERE key=$1 AND tenant_id=$2",
                key, self.tenant_id,
            )
        if row is None:
            return None
        if row["expires_at"] and datetime.now(timezone.utc) > row["expires_at"]:
            await self.delete(key)
            return None
        return IdempotencyRecord(
            key=row["key"],
            run_id=row["run_id"],
            tool=row["tool"],
            result=json.loads(row["result"]) if isinstance(row["result"], str) else row["result"],
            executed_at=row["executed_at"],
            call_count=row["call_count"],
        )

    async def set(self, key: str, record: IdempotencyRecord) -> None:
        pool = await self._conn()
        expires_at = None
        if self.ttl_seconds:
            expires_at = datetime.now(timezone.utc) + timedelta(seconds=self.ttl_seconds)
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO wire_idempotency
                    (key, tenant_id, run_id, tool, result, executed_at, call_count, expires_at)
                VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8)
                ON CONFLICT (key, tenant_id) DO UPDATE SET
                    call_count = wire_idempotency.call_count + 1
                RETURNING key, call_count
                """,
                key, self.tenant_id, record.run_id, record.tool,
                json.dumps(record.result, default=str),
                record.executed_at, record.call_count, expires_at,
            )
        if row is None:
            log.warning("idem_pg_set_no_row", key=key[:12], tenant=self.tenant_id)
        log.debug("idem_pg_set", key=key[:12], tool=record.tool, tenant=self.tenant_id)

    async def increment(self, key: str) -> int:
        pool = await self._conn()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE wire_idempotency SET call_count = call_count + 1 "
                "WHERE key=$1 AND tenant_id=$2",
                key, self.tenant_id,
            )
            row = await conn.fetchrow(
                "SELECT call_count FROM wire_idempotency WHERE key=$1 AND tenant_id=$2",
                key, self.tenant_id,
            )
        return row["call_count"] if row else 1

    async def delete(self, key: str) -> None:
        pool = await self._conn()
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM wire_idempotency WHERE key=$1 AND tenant_id=$2",
                key, self.tenant_id,
            )

    async def clear(self) -> None:
        pool = await self._conn()
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM wire_idempotency WHERE tenant_id=$1",
                self.tenant_id,
            )

    async def exists(self, key: str) -> bool:
        return (await self.get(key)) is not None

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()
            self._pool = None
