"""Tests for AuditChain — append, seal, verify, tamper detection, restart resume."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from wire.core.audit import AuditChain, AuditEntry, _GENESIS_HASH
from wire.core.errors import AuditChainError


@pytest.fixture
def audit_path(tmp_path: Path) -> Path:
    return tmp_path / "test-audit.jsonl"


class TestAuditEntry:
    def test_seal_produces_deterministic_hash(self) -> None:
        entry = AuditEntry(run_id="r1", event="test", prev_hash=_GENESIS_HASH)
        entry.seal()
        h1 = entry.entry_hash
        entry2 = AuditEntry(
            id=entry.id, ts=entry.ts, run_id="r1",
            event="test", prev_hash=_GENESIS_HASH
        )
        entry2.seal()
        assert h1 == entry2.entry_hash

    def test_seal_fills_entry_hash(self) -> None:
        entry = AuditEntry(run_id="r1", event="test", prev_hash=_GENESIS_HASH)
        assert entry.entry_hash == ""
        entry.seal()
        assert len(entry.entry_hash) == 64  # SHA-256 hex

    def test_different_data_produces_different_hash(self) -> None:
        e1 = AuditEntry(run_id="r1", event="a", prev_hash=_GENESIS_HASH).seal()
        e2 = AuditEntry(run_id="r1", event="b", prev_hash=_GENESIS_HASH).seal()
        assert e1.entry_hash != e2.entry_hash


class TestAuditChainWrite:
    @pytest.mark.asyncio
    async def test_write_creates_file(self, audit_path: Path) -> None:
        chain = AuditChain(run_id="r1", path=audit_path)
        await chain.write("test_event")
        assert audit_path.exists()

    @pytest.mark.asyncio
    async def test_write_produces_valid_json_lines(self, audit_path: Path) -> None:
        chain = AuditChain(run_id="r1", path=audit_path)
        await chain.write("evt1", data={"k": "v"})
        await chain.write("evt2")
        lines = audit_path.read_text().strip().splitlines()
        assert len(lines) == 2
        for line in lines:
            obj = json.loads(line)
            assert "entry_hash" in obj
            assert "prev_hash" in obj
            assert "event" in obj

    @pytest.mark.asyncio
    async def test_chain_links_correctly(self, audit_path: Path) -> None:
        chain = AuditChain(run_id="r1", path=audit_path)
        await chain.write("first")
        await chain.write("second")
        lines = [json.loads(l) for l in audit_path.read_text().strip().splitlines()]
        assert lines[0]["prev_hash"] == _GENESIS_HASH
        assert lines[1]["prev_hash"] == lines[0]["entry_hash"]

    @pytest.mark.asyncio
    async def test_actor_recorded(self, audit_path: Path) -> None:
        chain = AuditChain(run_id="r1", path=audit_path)
        await chain.write("approved", actor="human:naveen@sap.com")
        obj = json.loads(audit_path.read_text())
        assert obj["actor"] == "human:naveen@sap.com"


class TestAuditChainVerify:
    @pytest.mark.asyncio
    async def test_verify_clean_chain(self, audit_path: Path) -> None:
        chain = AuditChain(run_id="r1", path=audit_path)
        for i in range(10):
            await chain.write(f"event_{i}")
        count = AuditChain.verify(audit_path)
        assert count == 10

    def test_verify_tampered_data_raises(self, audit_path: Path) -> None:
        import asyncio
        asyncio.run(self._write_entry(audit_path))

        # Tamper with the data field
        lines = audit_path.read_text().splitlines()
        obj = json.loads(lines[0])
        obj["event"] = "tampered"
        audit_path.write_text(json.dumps(obj) + "\n")

        with pytest.raises(AuditChainError) as exc_info:
            AuditChain.verify(audit_path)
        assert exc_info.value.entry_index == 0

    @staticmethod
    async def _write_entry(path: Path) -> None:
        chain = AuditChain(run_id="r1", path=path)
        await chain.write("original")

    def test_verify_broken_link_raises(self, audit_path: Path) -> None:
        import asyncio
        asyncio.run(self._write_two(audit_path))

        lines = audit_path.read_text().splitlines()
        obj = json.loads(lines[1])
        obj["prev_hash"] = "0" * 64  # break the link
        audit_path.write_text(lines[0] + "\n" + json.dumps(obj) + "\n")

        with pytest.raises(AuditChainError):
            AuditChain.verify(audit_path)

    @staticmethod
    async def _write_two(path: Path) -> None:
        chain = AuditChain(run_id="r1", path=path)
        await chain.write("e1")
        await chain.write("e2")

    def test_verify_empty_file_returns_zero(self, audit_path: Path) -> None:
        audit_path.write_text("")
        count = AuditChain.verify(audit_path)
        assert count == 0


class TestAuditChainResume:
    @pytest.mark.asyncio
    async def test_resume_continues_chain(self, audit_path: Path) -> None:
        chain1 = AuditChain(run_id="r1", path=audit_path)
        await chain1.write("session1_event1")
        await chain1.write("session1_event2")

        # New instance — simulates process restart
        chain2 = AuditChain(run_id="r1", path=audit_path)
        await chain2.write("session2_event1")

        # Full chain must verify
        count = AuditChain.verify(audit_path)
        assert count == 3
