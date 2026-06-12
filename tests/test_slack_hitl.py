"""
Tests for the Slack HITL channel.

All tests mock slack-sdk — no real Slack credentials required.
The suite is skipped gracefully if slack-sdk is not installed.
"""

from __future__ import annotations

import asyncio
import importlib
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Skip guard — entire module is skipped if slack-sdk is not available.
# ---------------------------------------------------------------------------
slack_sdk_available = importlib.util.find_spec("slack_sdk") is not None

pytestmark = pytest.mark.skipif(
    not slack_sdk_available,
    reason="slack-sdk not installed (pip install 'wire-ai[slack]')",
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
from wire.core.hitl import (
    HITLAction,
    HITLChannel,
    HITLDecision,
    HITLGate,
    HITLRequest,
    HITLTimeoutError,
    TimeoutAction,
)
from wire.core.models import Risk


def _make_request(
    options: list[str] | None = None,
    timeout_minutes: int = 5,
) -> HITLRequest:
    return HITLRequest(
        run_id="run_test_001",
        role="ops-agent",
        message="Approve database migration on prod?",
        context={"db": "postgres", "rows": "1.2M"},
        risk=Risk.HIGH,
        options=options or ["approve", "reject", "modify"],
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=timeout_minutes),
    )


# ---------------------------------------------------------------------------
# 1. Block Kit message structure
# ---------------------------------------------------------------------------
class TestBlockKitFormatting:
    """Verify the Block Kit payload structure without calling Slack."""

    def test_header_block_present(self) -> None:
        from wire.channels.slack import _build_blocks

        req = _make_request()
        blocks = _build_blocks(req, timeout_minutes=10)
        header_blocks = [b for b in blocks if b.get("type") == "header"]
        assert header_blocks, "Expected at least one header block"
        assert "HITL" in header_blocks[0]["text"]["text"]

    def test_action_buttons_match_options(self) -> None:
        from wire.channels.slack import _build_blocks

        options = ["approve", "reject", "modify"]
        req = _make_request(options=options)
        blocks = _build_blocks(req, timeout_minutes=10)

        action_blocks = [b for b in blocks if b.get("type") == "actions"]
        assert action_blocks, "Expected an actions block with buttons"
        elements = action_blocks[0]["elements"]
        assert len(elements) == len(options)

        button_values = [e["value"] for e in elements]
        for opt in options:
            assert any(opt in v for v in button_values), (
                f"Button for option '{opt}' missing"
            )

    def test_approve_button_has_primary_style(self) -> None:
        from wire.channels.slack import _build_blocks

        req = _make_request()
        blocks = _build_blocks(req, timeout_minutes=10)
        action_blocks = [b for b in blocks if b.get("type") == "actions"]
        elements = action_blocks[0]["elements"]
        approve_btn = next(e for e in elements if "approve" in e["value"])
        assert approve_btn.get("style") == "primary"

    def test_reject_button_has_danger_style(self) -> None:
        from wire.channels.slack import _build_blocks

        req = _make_request()
        blocks = _build_blocks(req, timeout_minutes=10)
        action_blocks = [b for b in blocks if b.get("type") == "actions"]
        elements = action_blocks[0]["elements"]
        reject_btn = next(e for e in elements if "reject" in e["value"])
        assert reject_btn.get("style") == "danger"

    def test_metadata_fields_include_run_id_and_risk(self) -> None:
        from wire.channels.slack import _build_blocks

        req = _make_request()
        blocks = _build_blocks(req, timeout_minutes=10)
        # Flatten all text content
        all_text = str(blocks)
        assert req.run_id in all_text
        assert "HIGH" in all_text

    def test_context_pairs_appear_in_blocks(self) -> None:
        from wire.channels.slack import _build_blocks

        req = _make_request()
        blocks = _build_blocks(req, timeout_minutes=10)
        all_text = str(blocks)
        assert "postgres" in all_text
        assert "1.2M" in all_text

    def test_request_id_encoded_in_button_value(self) -> None:
        from wire.channels.slack import _build_blocks

        req = _make_request()
        blocks = _build_blocks(req, timeout_minutes=10)
        action_blocks = [b for b in blocks if b.get("type") == "actions"]
        elements = action_blocks[0]["elements"]
        # Full request id must appear in at least one button value
        assert any(req.id in e["value"] for e in elements)

    def test_expiry_appears_in_blocks(self) -> None:
        from wire.channels.slack import _build_blocks

        req = _make_request(timeout_minutes=15)
        blocks = _build_blocks(req, timeout_minutes=15)
        all_text = str(blocks)
        # Either the formatted time or the fallback "15m" must appear
        assert "15" in all_text


# ---------------------------------------------------------------------------
# 2. Channel name parsing from "slack:#channel-name"
# ---------------------------------------------------------------------------
class TestChannelNameParsing:
    """HITLGate should parse channel names from 'slack:#channel-name' strings."""

    def test_slack_prefix_sets_channel_enum(self) -> None:
        gate = HITLGate(channel="slack:#ops-approvals", timeout_minutes=1)
        assert gate.channel == HITLChannel.SLACK

    def test_slack_prefix_extracts_channel_name(self) -> None:
        gate = HITLGate(channel="slack:#ops-approvals", timeout_minutes=1)
        assert gate._slack_channel == "#ops-approvals"

    def test_explicit_slack_channel_param_wins(self) -> None:
        """slack_channel kwarg takes priority over embedded channel name."""
        gate = HITLGate(
            channel="slack:#default-ch",
            slack_channel="#override-ch",
            timeout_minutes=1,
        )
        assert gate._slack_channel == "#override-ch"

    def test_slack_enum_with_channel_kwarg(self) -> None:
        gate = HITLGate(
            channel=HITLChannel.SLACK,
            slack_channel="#direct-channel",
            timeout_minutes=1,
        )
        assert gate.channel == HITLChannel.SLACK
        assert gate._slack_channel == "#direct-channel"

    @pytest.mark.asyncio
    async def test_missing_slack_channel_raises_on_dispatch(self) -> None:
        """HITLGate with SLACK channel but no channel name raises ValueError."""
        gate = HITLGate(channel=HITLChannel.SLACK, timeout_minutes=1)
        req = _make_request()
        with pytest.raises(ValueError, match="slack_channel is required"):
            await gate._slack_prompt(req)

    def test_channel_name_without_hash_normalised(self) -> None:
        from wire.channels.slack import SlackHITLChannel

        ch = SlackHITLChannel(channel="#ops-approvals", token="xoxb-fake")
        assert ch.channel == "ops-approvals"  # leading # stripped

    def test_cli_channel_unaffected(self) -> None:
        gate = HITLGate(channel=HITLChannel.CLI, timeout_minutes=1)
        assert gate.channel == HITLChannel.CLI
        assert gate._slack_channel is None


# ---------------------------------------------------------------------------
# 3. Approval detection (mock poll returning button click)
# ---------------------------------------------------------------------------
class TestApprovalDetection:
    """SlackHITLChannel.request returns APPROVE when a user clicks approve."""

    @pytest.mark.asyncio
    async def test_approve_via_text_reply(self) -> None:
        from wire.channels.slack import SlackHITLChannel

        req = _make_request()
        approve_message: dict[str, Any] = {
            "ts": "9999999999.000001",
            "user": "U123ABC",
            "text": "approve",
        }

        mock_client = AsyncMock()
        mock_client.chat_postMessage.return_value = {"ts": "1000000000.000000", "ok": True}
        mock_client.auth_test.return_value = {"bot_id": "B_BOT", "ok": True}
        mock_client.conversations_history.return_value = {
            "messages": [approve_message],
            "ok": True,
        }

        with patch("wire.channels.slack.AsyncWebClient", return_value=mock_client):
            ch = SlackHITLChannel(
                channel="#test",
                token="xoxb-fake",
                poll_interval=0.01,
                timeout_minutes=1,
            )
            decision = await ch.request(req, timeout_minutes=1)

        assert decision.action == HITLAction.APPROVE
        assert "U123ABC" in decision.actor

    @pytest.mark.asyncio
    async def test_approve_via_metadata_payload(self) -> None:
        """Approval can also arrive via message metadata (button interaction)."""
        from wire.channels.slack import SlackHITLChannel

        req = _make_request()
        button_message: dict[str, Any] = {
            "ts": "9999999999.000002",
            "user": "U456DEF",
            "text": "",
            "metadata": {
                "event_payload": {
                    "action_value": f"approve|{req.id}",
                }
            },
        }

        mock_client = AsyncMock()
        mock_client.chat_postMessage.return_value = {"ts": "1000000000.000000", "ok": True}
        mock_client.auth_test.return_value = {"bot_id": "B_BOT", "ok": True}
        mock_client.conversations_history.return_value = {
            "messages": [button_message],
            "ok": True,
        }

        with patch("wire.channels.slack.AsyncWebClient", return_value=mock_client):
            ch = SlackHITLChannel(
                channel="#test",
                token="xoxb-fake",
                poll_interval=0.01,
                timeout_minutes=1,
            )
            decision = await ch.request(req, timeout_minutes=1)

        assert decision.action == HITLAction.APPROVE


# ---------------------------------------------------------------------------
# 4. Rejection detection
# ---------------------------------------------------------------------------
class TestRejectionDetection:
    @pytest.mark.asyncio
    async def test_reject_via_text_reply(self) -> None:
        from wire.channels.slack import SlackHITLChannel

        req = _make_request()
        reject_message: dict[str, Any] = {
            "ts": "9999999999.000003",
            "user": "U789GHI",
            "text": "reject — wrong environment",
        }

        mock_client = AsyncMock()
        mock_client.chat_postMessage.return_value = {"ts": "1000000000.000000", "ok": True}
        mock_client.auth_test.return_value = {"bot_id": "B_BOT", "ok": True}
        mock_client.conversations_history.return_value = {
            "messages": [reject_message],
            "ok": True,
        }

        with patch("wire.channels.slack.AsyncWebClient", return_value=mock_client):
            ch = SlackHITLChannel(
                channel="#test",
                token="xoxb-fake",
                poll_interval=0.01,
                timeout_minutes=1,
            )
            decision = await ch.request(req, timeout_minutes=1)

        assert decision.action == HITLAction.REJECT
        assert "U789GHI" in decision.actor

    @pytest.mark.asyncio
    async def test_reject_via_metadata_payload(self) -> None:
        from wire.channels.slack import SlackHITLChannel

        req = _make_request()
        button_message: dict[str, Any] = {
            "ts": "9999999999.000004",
            "user": "U999ZZZ",
            "text": "",
            "metadata": {
                "event_payload": {
                    "action_value": f"reject|{req.id}",
                }
            },
        }

        mock_client = AsyncMock()
        mock_client.chat_postMessage.return_value = {"ts": "1000000000.000000", "ok": True}
        mock_client.auth_test.return_value = {"bot_id": "B_BOT", "ok": True}
        mock_client.conversations_history.return_value = {
            "messages": [button_message],
            "ok": True,
        }

        with patch("wire.channels.slack.AsyncWebClient", return_value=mock_client):
            ch = SlackHITLChannel(
                channel="#test",
                token="xoxb-fake",
                poll_interval=0.01,
                timeout_minutes=1,
            )
            decision = await ch.request(req, timeout_minutes=1)

        assert decision.action == HITLAction.REJECT

    @pytest.mark.asyncio
    async def test_bot_own_messages_ignored(self) -> None:
        """Messages from the bot itself must not trigger a decision."""
        from wire.channels.slack import SlackHITLChannel

        req = _make_request()
        own_message: dict[str, Any] = {
            "ts": "9999999999.000005",
            "bot_id": "B_BOT",
            "text": "approve",
        }
        human_message: dict[str, Any] = {
            "ts": "9999999999.000006",
            "user": "U_HUMAN",
            "text": "reject",
        }

        mock_client = AsyncMock()
        mock_client.chat_postMessage.return_value = {"ts": "1000000000.000000", "ok": True}
        mock_client.auth_test.return_value = {"bot_id": "B_BOT", "ok": True}
        mock_client.conversations_history.return_value = {
            "messages": [own_message, human_message],
            "ok": True,
        }

        with patch("wire.channels.slack.AsyncWebClient", return_value=mock_client):
            ch = SlackHITLChannel(
                channel="#test",
                token="xoxb-fake",
                poll_interval=0.01,
                timeout_minutes=1,
            )
            decision = await ch.request(req, timeout_minutes=1)

        # Bot said "approve" but we should read the human's "reject"
        assert decision.action == HITLAction.REJECT


# ---------------------------------------------------------------------------
# 5. Timeout handling
# ---------------------------------------------------------------------------
class TestTimeoutHandling:
    @pytest.mark.asyncio
    async def test_timeout_escalate_raises(self) -> None:
        """ESCALATE timeout_action raises HITLTimeoutError."""
        from wire.channels.slack import SlackHITLChannel

        req = _make_request()

        mock_client = AsyncMock()
        mock_client.chat_postMessage.return_value = {"ts": "1000000000.000000", "ok": True}
        mock_client.auth_test.return_value = {"bot_id": "B_BOT", "ok": True}
        mock_client.conversations_history.return_value = {"messages": [], "ok": True}

        # Use a 0-second timeout so the loop exits after the very first sleep
        with patch("wire.channels.slack.AsyncWebClient", return_value=mock_client):
            ch = SlackHITLChannel(
                channel="#test",
                token="xoxb-fake",
                poll_interval=0.01,
                timeout_minutes=0,  # zero minutes → deadline already in past
                timeout_action=TimeoutAction.ESCALATE,
            )
            # Override timeout_minutes to 0 so _handle_timeout receives sensible value
            with pytest.raises(HITLTimeoutError):
                await ch.request(
                    req,
                    timeout_minutes=0,
                    timeout_action=TimeoutAction.ESCALATE,
                )

    @pytest.mark.asyncio
    async def test_timeout_auto_approve(self) -> None:
        """APPROVE timeout_action returns an APPROVE decision on timeout."""
        from wire.channels.slack import SlackHITLChannel

        req = _make_request()

        mock_client = AsyncMock()
        mock_client.chat_postMessage.return_value = {"ts": "1000000000.000000", "ok": True}
        mock_client.auth_test.return_value = {"bot_id": "B_BOT", "ok": True}
        mock_client.conversations_history.return_value = {"messages": [], "ok": True}

        with patch("wire.channels.slack.AsyncWebClient", return_value=mock_client):
            ch = SlackHITLChannel(
                channel="#test",
                token="xoxb-fake",
                poll_interval=0.01,
                timeout_minutes=0,
                timeout_action=TimeoutAction.APPROVE,
            )
            decision = await ch.request(
                req,
                timeout_minutes=0,
                timeout_action=TimeoutAction.APPROVE,
            )

        assert decision.action == HITLAction.APPROVE
        assert decision.actor == "wire:timeout"

    @pytest.mark.asyncio
    async def test_timeout_auto_reject(self) -> None:
        """REJECT timeout_action returns a REJECT decision on timeout."""
        from wire.channels.slack import SlackHITLChannel

        req = _make_request()

        mock_client = AsyncMock()
        mock_client.chat_postMessage.return_value = {"ts": "1000000000.000000", "ok": True}
        mock_client.auth_test.return_value = {"bot_id": "B_BOT", "ok": True}
        mock_client.conversations_history.return_value = {"messages": [], "ok": True}

        with patch("wire.channels.slack.AsyncWebClient", return_value=mock_client):
            ch = SlackHITLChannel(
                channel="#test",
                token="xoxb-fake",
                poll_interval=0.01,
                timeout_minutes=0,
                timeout_action=TimeoutAction.REJECT,
            )
            decision = await ch.request(
                req,
                timeout_minutes=0,
                timeout_action=TimeoutAction.REJECT,
            )

        assert decision.action == HITLAction.REJECT
        assert decision.actor == "wire:timeout"


# ---------------------------------------------------------------------------
# 6. HITLGate integration with Slack channel
# ---------------------------------------------------------------------------
class TestHITLGateSlackIntegration:
    """Verify HITLGate correctly delegates to SlackHITLChannel."""

    @pytest.mark.asyncio
    async def test_gate_dispatches_to_slack(self) -> None:
        """HITLGate._slack_prompt must be called when channel=SLACK."""
        gate = HITLGate(
            channel="slack:#ops-approvals",
            timeout_minutes=1,
        )

        req = _make_request()
        expected_decision = HITLDecision(
            request_id=req.id,
            action=HITLAction.APPROVE,
            actor="human:slack:U123",
        )

        with patch("wire.core.hitl.HITLGate._slack_prompt", new_callable=AsyncMock) as mock_slack:
            mock_slack.return_value = expected_decision
            # Call _dispatch directly to avoid the full request() EventBus path
            decision = await gate._dispatch(req)

        mock_slack.assert_called_once_with(req)
        assert decision.action == HITLAction.APPROVE

    @pytest.mark.asyncio
    async def test_cli_channel_not_affected(self) -> None:
        """CLI channel must still go through _cli_prompt, not _slack_prompt."""
        gate = HITLGate(channel=HITLChannel.CLI, timeout_minutes=1)
        req = _make_request()

        with (
            patch("wire.core.hitl.HITLGate._cli_prompt", new_callable=AsyncMock) as mock_cli,
            patch("wire.core.hitl.HITLGate._slack_prompt", new_callable=AsyncMock) as mock_slack,
        ):
            mock_cli.return_value = HITLDecision(
                request_id=req.id, action=HITLAction.APPROVE, actor="human:cli"
            )
            await gate._dispatch(req)

        mock_cli.assert_called_once()
        mock_slack.assert_not_called()


# ---------------------------------------------------------------------------
# 7. Export surface
# ---------------------------------------------------------------------------
class TestExports:
    def test_slack_channel_importable_from_channels(self) -> None:
        from wire.channels import SlackHITLChannel  # noqa: F401

    def test_slack_channel_importable_from_wire(self) -> None:
        from wire import SlackHITLChannel  # noqa: F401
