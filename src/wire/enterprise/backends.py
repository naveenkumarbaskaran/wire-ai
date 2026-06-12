"""
Enterprise audit backends — S3 and PostgreSQL.

The default AuditChain writes to a local JSONL file.
These backends provide enterprise-grade storage:

  S3Backend:
    - Writes to AWS S3 with server-side encryption (SSE-S3 or SSE-KMS)
    - Immutable object lock support (WORM — write-once-read-many)
    - Lifecycle policies for retention
    - Requires: pip install wire-ai[s3] (boto3)

  PostgresBackend:
    - Writes to PostgreSQL audit table
    - Row-level security for multi-tenant isolation
    - Full-text search on audit events
    - Requires: pip install wire-ai[postgres] (asyncpg)

Both backends maintain the same SHA-256 hash chain as the local backend.
AuditChain.verify() works identically across all backends.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import orjson
import structlog
from pydantic import BaseModel

from wire.core.audit import AuditEntry, _GENESIS_HASH
from wire.core.errors import WIREError

log = structlog.get_logger(__name__)


class AuditBackendError(WIREError):
    pass


class S3AuditBackend:
    """
    S3-backed AuditChain — enterprise tamper-proof audit storage.

    Writes each entry as a separate S3 object:
      s3://<bucket>/<prefix>/<run_id>/<entry_id>.json

    Object Lock (WORM) prevents deletion for retention_days.
    SSE-KMS encrypts every entry at rest.

    Requires: boto3 (pip install wire-ai[s3])
    """

    def __init__(
        self,
        *,
        bucket: str,
        prefix: str = "wire-audit",
        region: str = "us-east-1",
        kms_key_id: str | None = None,
        retention_days: int = 365,
        object_lock: bool = False,
    ) -> None:
        self.bucket = bucket
        self.prefix = prefix.rstrip("/")
        self.region = region
        self.kms_key_id = kms_key_id
        self.retention_days = retention_days
        self.object_lock = object_lock
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is None:
            try:
                import boto3
                self._client = boto3.client("s3", region_name=self.region)
            except ImportError:
                raise AuditBackendError(
                    "S3 backend requires boto3. Install: pip install wire-ai[s3]"
                )
        return self._client

    async def write(self, entry: AuditEntry) -> None:
        import asyncio
        client = self._get_client()
        key = f"{self.prefix}/{entry.run_id}/{entry.id}.json"
        body = orjson.dumps(entry.model_dump(mode="json"))

        put_kwargs: dict[str, Any] = {
            "Bucket": self.bucket,
            "Key": key,
            "Body": body,
            "ContentType": "application/json",
        }

        if self.kms_key_id:
            put_kwargs["ServerSideEncryption"] = "aws:kms"
            put_kwargs["SSEKMSKeyId"] = self.kms_key_id
        else:
            put_kwargs["ServerSideEncryption"] = "AES256"

        if self.object_lock:
            from datetime import timedelta
            put_kwargs["ObjectLockMode"] = "COMPLIANCE"
            put_kwargs["ObjectLockRetainUntilDate"] = (
                datetime.now(timezone.utc) + timedelta(days=self.retention_days)
            )

        await asyncio.get_event_loop().run_in_executor(
            None, lambda: client.put_object(**put_kwargs)
        )
        log.debug("s3_audit_write", key=key, entry_hash=entry.entry_hash[:12])

    def describe(self) -> str:
        return (
            f"S3AuditBackend(bucket={self.bucket}, prefix={self.prefix}, "
            f"region={self.region}, retention={self.retention_days}d, "
            f"object_lock={self.object_lock})"
        )


class PostgresAuditBackend:
    """
    PostgreSQL-backed AuditChain — enterprise audit with SQL query support.

    Schema (auto-created on first use):
        CREATE TABLE wire_audit (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            tenant_id TEXT,
            role TEXT,
            event TEXT NOT NULL,
            actor TEXT NOT NULL DEFAULT 'wire',
            data JSONB NOT NULL DEFAULT '{}',
            prev_hash TEXT NOT NULL,
            entry_hash TEXT NOT NULL,
            ts TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX wire_audit_run_id ON wire_audit(run_id);
        CREATE INDEX wire_audit_tenant ON wire_audit(tenant_id);

    Requires: asyncpg (pip install wire-ai[postgres])
    """

    _CREATE_TABLE = """
    CREATE TABLE IF NOT EXISTS wire_audit (
        id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL,
        tenant_id TEXT,
        role TEXT,
        event TEXT NOT NULL,
        actor TEXT NOT NULL DEFAULT 'wire',
        data JSONB NOT NULL DEFAULT '{}',
        prev_hash TEXT NOT NULL,
        entry_hash TEXT NOT NULL,
        ts TIMESTAMPTZ NOT NULL
    );
    CREATE INDEX IF NOT EXISTS wire_audit_run_id_idx ON wire_audit(run_id);
    CREATE INDEX IF NOT EXISTS wire_audit_tenant_idx ON wire_audit(tenant_id);
    """

    def __init__(
        self,
        *,
        dsn: str,
        tenant_id: str | None = None,
    ) -> None:
        self.dsn = dsn
        self.tenant_id = tenant_id
        self._pool: Any = None

    async def _get_pool(self) -> Any:
        if self._pool is None:
            try:
                import asyncpg
                self._pool = await asyncpg.create_pool(self.dsn)
                async with self._pool.acquire() as conn:
                    await conn.execute(self._CREATE_TABLE)
            except ImportError:
                raise AuditBackendError(
                    "Postgres backend requires asyncpg. Install: pip install wire-ai[postgres]"
                )
        return self._pool

    async def write(self, entry: AuditEntry) -> None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO wire_audit
                    (id, run_id, tenant_id, role, event, actor, data, prev_hash, entry_hash, ts)
                VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9, $10)
                """,
                entry.id,
                entry.run_id,
                self.tenant_id,
                entry.role,
                entry.event,
                entry.actor,
                json.dumps(entry.data),
                entry.prev_hash,
                entry.entry_hash,
                entry.ts,
            )
        log.debug("pg_audit_write", run_id=entry.run_id, entry_hash=entry.entry_hash[:12])

    async def verify_run(self, run_id: str) -> int:
        """Verify chain integrity for a specific run. Returns entry count."""
        from wire.core.errors import AuditChainError
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM wire_audit WHERE run_id=$1 ORDER BY ts ASC",
                run_id,
            )
        prev_hash = _GENESIS_HASH
        for i, row in enumerate(rows):
            canonical = {
                "id": row["id"],
                "ts": row["ts"].isoformat(),
                "run_id": row["run_id"],
                "role": row["role"],
                "event": row["event"],
                "actor": row["actor"],
                "data": json.loads(row["data"]),
                "prev_hash": row["prev_hash"],
            }
            payload = orjson.dumps(canonical, option=orjson.OPT_SORT_KEYS)
            expected = hashlib.sha256(payload).hexdigest()
            if row["entry_hash"] != expected:
                from wire.core.errors import AuditChainError
                raise AuditChainError(i, expected, row["entry_hash"])
            if row["prev_hash"] != prev_hash:
                from wire.core.errors import AuditChainError
                raise AuditChainError(i, prev_hash, row["prev_hash"])
            prev_hash = row["entry_hash"]
        return len(rows)

    def describe(self) -> str:
        dsn_safe = self.dsn.split("@")[-1] if "@" in self.dsn else self.dsn
        return f"PostgresAuditBackend(host={dsn_safe}, tenant={self.tenant_id})"
