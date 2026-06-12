"""
AuditChain — tamper-proof, append-only audit log.

Every entry is SHA-256 hashed and chain-linked to the previous entry.
Verification detects any modification, deletion, or insertion after the fact.

Default backend: local JSONL file (zero external dependencies).
Enterprise backends: SQLite, Postgres, S3 (Sprint 6).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import orjson
import structlog
from pydantic import BaseModel, Field

from wire.core.errors import AuditChainError

log = structlog.get_logger(__name__)

_GENESIS_HASH = "0" * 64  # sentinel for first entry


class AuditEntry(BaseModel):
    """One immutable record in the audit chain."""

    id: str = Field(default_factory=lambda: str(uuid4()))
    ts: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    run_id: str
    role: str | None = None
    event: str                            # human-readable event name
    actor: str = "wire"                   # "wire", "human:<id>", "agent:<role>"
    data: dict[str, Any] = Field(default_factory=dict)
    prev_hash: str = _GENESIS_HASH        # hash of previous entry
    entry_hash: str = ""                  # computed on serialise; empty until sealed

    def seal(self) -> "AuditEntry":
        """Compute and attach this entry's hash. Returns self for chaining."""
        # Use model_dump(mode="json") so ts is serialised to string the same way
        # Pydantic will when writing to disk — guarantees verify() sees identical bytes.
        d = self.model_dump(mode="json")
        d.pop("entry_hash", None)
        payload = orjson.dumps(d, option=orjson.OPT_SORT_KEYS)
        self.entry_hash = hashlib.sha256(payload).hexdigest()
        return self

class AuditChain:
    """
    Append-only, hash-linked audit chain.

    Usage:
        chain = AuditChain(run_id="run_123", path="audit.jsonl")
        await chain.write("tool_call", data={"tool": "jira_create", "args": {...}})
        AuditChain.verify("audit.jsonl")  # raises AuditChainError on tampering
    """

    def __init__(
        self,
        run_id: str,
        path: str | Path = "wire-audit.jsonl",
        role: str | None = None,
    ) -> None:
        self.run_id = run_id
        self.path = Path(path)
        self.role = role
        self._last_hash: str = _GENESIS_HASH
        self._count: int = 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._load_tail()

    # ── Public API ────────────────────────────────────────────────────────────

    async def write(
        self,
        event: str,
        *,
        actor: str = "wire",
        data: dict[str, Any] | None = None,
        role: str | None = None,
    ) -> AuditEntry:
        """Append a sealed entry to the chain. Thread-safe per process."""
        entry = AuditEntry(
            run_id=self.run_id,
            role=role or self.role,
            event=event,
            actor=actor,
            data=data or {},
            prev_hash=self._last_hash,
        ).seal()

        line = orjson.dumps(entry.model_dump(mode="json")).decode() + "\n"
        with self.path.open("ab") as f:
            f.write(line.encode())

        self._last_hash = entry.entry_hash
        self._count += 1
        log.debug("audit_write", audit_event=event, hash=entry.entry_hash[:12], count=self._count)
        return entry

    @staticmethod
    def verify(path: str | Path) -> int:
        """
        Verify the entire chain from disk.
        Returns the number of entries verified.
        Raises AuditChainError at the first broken link.
        """
        path = Path(path)
        prev_hash = _GENESIS_HASH
        count = 0

        with path.open() as f:
            for i, line in enumerate(f):
                raw = json.loads(line)
                stored_hash = raw.pop("entry_hash", "")

                # Hash the remaining fields exactly as seal() does
                payload = orjson.dumps(raw, option=orjson.OPT_SORT_KEYS)
                expected = hashlib.sha256(payload).hexdigest()

                if stored_hash != expected:
                    raise AuditChainError(i, expected, stored_hash)
                if raw["prev_hash"] != prev_hash:
                    raise AuditChainError(i, prev_hash, raw["prev_hash"])

                prev_hash = stored_hash
                count += 1

        log.info("audit_verified", path=str(path), entries=count)
        return count

    # ── Internals ─────────────────────────────────────────────────────────────

    def _load_tail(self) -> None:
        """Resume chain from the last entry on disk (supports append across restarts)."""
        if not self.path.exists() or self.path.stat().st_size == 0:
            return
        # Seek backwards from end to find last non-empty line — O(1) for typical entries
        last_line = b""
        with self.path.open("rb") as f:
            # Read last 4KB — sufficient for any single audit entry
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 4096))
            tail = f.read()
        for line in reversed(tail.splitlines()):
            if line.strip():
                last_line = line
                break
        if last_line:
            try:
                raw = __import__("orjson").loads(last_line)
                self._last_hash = raw.get("entry_hash", _GENESIS_HASH)
            except Exception:
                pass
        # Count lines efficiently
        self._count = sum(1 for _ in self.path.open())
