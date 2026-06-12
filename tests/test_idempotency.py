"""Tests for IdempotencyGuard — deduplication, key generation, cache behaviour."""

from __future__ import annotations

import pytest

from wire.core.idempotency import IdempotencyGuard


async def _make_fn(value: str, call_log: list[str]):
    """Async factory that records calls and returns a value."""
    async def fn():
        call_log.append(value)
        return value
    return fn


class TestIdempotencyKey:
    def test_same_inputs_produce_same_key(self) -> None:
        k1 = IdempotencyGuard.make_key("jira_create", {"title": "P1", "project": "OPS"})
        k2 = IdempotencyGuard.make_key("jira_create", {"title": "P1", "project": "OPS"})
        assert k1 == k2

    def test_different_tool_produces_different_key(self) -> None:
        k1 = IdempotencyGuard.make_key("jira_create", {"title": "P1"})
        k2 = IdempotencyGuard.make_key("slack_send",  {"title": "P1"})
        assert k1 != k2

    def test_different_args_produce_different_key(self) -> None:
        k1 = IdempotencyGuard.make_key("email_send", {"to": "a@b.com"})
        k2 = IdempotencyGuard.make_key("email_send", {"to": "c@d.com"})
        assert k1 != k2

    def test_arg_order_does_not_affect_key(self) -> None:
        k1 = IdempotencyGuard.make_key("tool", {"b": 2, "a": 1})
        k2 = IdempotencyGuard.make_key("tool", {"a": 1, "b": 2})
        assert k1 == k2

    def test_key_is_64_char_hex(self) -> None:
        k = IdempotencyGuard.make_key("tool", {})
        assert len(k) == 64
        assert all(c in "0123456789abcdef" for c in k)


class TestIdempotencyCall:
    @pytest.mark.asyncio
    async def test_first_call_executes(self) -> None:
        guard = IdempotencyGuard()
        log: list[str] = []
        key = IdempotencyGuard.make_key("tool", {"x": 1})

        result, was_dup = await guard.call(
            key=key, fn=await _make_fn("result_a", log), run_id="r1", tool="tool"
        )
        assert result == "result_a"
        assert was_dup is False
        assert log == ["result_a"]

    @pytest.mark.asyncio
    async def test_second_call_skipped(self) -> None:
        guard = IdempotencyGuard()
        log: list[str] = []
        key = IdempotencyGuard.make_key("tool", {"x": 1})

        await guard.call(key=key, fn=await _make_fn("result_a", log), run_id="r1", tool="tool")
        result, was_dup = await guard.call(
            key=key, fn=await _make_fn("result_b", log), run_id="r1", tool="tool"
        )
        assert result == "result_a"   # original result returned
        assert was_dup is True
        assert log == ["result_a"]    # fn never called second time

    @pytest.mark.asyncio
    async def test_different_keys_both_execute(self) -> None:
        guard = IdempotencyGuard()
        log: list[str] = []
        k1 = IdempotencyGuard.make_key("tool", {"x": 1})
        k2 = IdempotencyGuard.make_key("tool", {"x": 2})

        await guard.call(key=k1, fn=await _make_fn("r1", log), run_id="r", tool="tool")
        await guard.call(key=k2, fn=await _make_fn("r2", log), run_id="r", tool="tool")
        assert log == ["r1", "r2"]

    @pytest.mark.asyncio
    async def test_clear_key_allows_re_execution(self) -> None:
        guard = IdempotencyGuard()
        log: list[str] = []
        key = IdempotencyGuard.make_key("tool", {"x": 1})

        await guard.call(key=key, fn=await _make_fn("first", log), run_id="r", tool="tool")
        guard.clear(key)
        _, was_dup = await guard.call(
            key=key, fn=await _make_fn("second", log), run_id="r", tool="tool"
        )
        assert was_dup is False
        assert log == ["first", "second"]

    def test_is_duplicate_false_before_call(self) -> None:
        guard = IdempotencyGuard()
        key = IdempotencyGuard.make_key("tool", {})
        assert guard.is_duplicate(key) is False

    @pytest.mark.asyncio
    async def test_is_duplicate_true_after_call(self) -> None:
        guard = IdempotencyGuard()
        key = IdempotencyGuard.make_key("tool", {})
        await guard.call(key=key, fn=await _make_fn("x", []), run_id="r", tool="tool")
        assert guard.is_duplicate(key) is True

    def test_call_count_increments(self) -> None:
        guard = IdempotencyGuard()
        assert guard.call_count == 0

    @pytest.mark.asyncio
    async def test_clear_all_resets(self) -> None:
        guard = IdempotencyGuard()
        log: list[str] = []
        for i in range(3):
            key = IdempotencyGuard.make_key("tool", {"i": i})
            await guard.call(key=key, fn=await _make_fn(f"r{i}", log), run_id="r", tool="tool")
        assert guard.call_count == 3
        guard.clear()
        assert guard.call_count == 0
