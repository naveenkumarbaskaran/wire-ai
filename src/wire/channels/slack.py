"""
SlackHITLChannel — Slack delivery for HITLGate approval requests.

Posts a rich Block Kit message to a Slack channel and polls
conversations.history for button interactions or text replies.
No webhook server required — polling every 3 s avoids the need
for a public URL.

Auth: SLACK_BOT_TOKEN env var, or pass token= directly.
Install: pip install "wire-ai[slack]"
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from typing import Any

import structlog

from wire.core.hitl import (
    HITLAction,
    HITLDecision,
    HITLRequest,
    HITLTimeoutError,
    TimeoutAction,
)

log = structlog.get_logger(__name__)

# Optional slack-sdk import — guarded so the module loads even without it.
try:
    from slack_sdk.web.async_client import AsyncWebClient as AsyncWebClient
    _SLACK_SDK_AVAILABLE = True
except ImportError:
    AsyncWebClient = None  # type: ignore[assignment,misc]
    _SLACK_SDK_AVAILABLE = False

# Maps option text → HITLAction (case-insensitive prefix match)
_ACTION_MAP: dict[str, HITLAction] = {
    "approve": HITLAction.APPROVE,
    "reject": HITLAction.REJECT,
    "modify": HITLAction.MODIFY,
}

_RISK_EMOJI = {
    "low": "🟢",
    "medium": "🟡",
    "high": "🔴",
    "critical": "🚨",
}


def _build_blocks(req: HITLRequest, timeout_minutes: int) -> list[dict[str, Any]]:
    """Build Slack Block Kit payload for an HITL approval request."""
    risk_val = req.risk.value if hasattr(req.risk, "value") else str(req.risk)
    emoji = _RISK_EMOJI.get(risk_val.lower(), "⚠️")
    expires_str = (
        req.expires_at.strftime("%H:%M UTC") if req.expires_at else f"{timeout_minutes}m"
    )

    # Header
    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"⏸ HITL Approval Required {emoji}",
                "emoji": True,
            },
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*{req.message}*"},
        },
    ]

    # Metadata fields
    fields: list[dict[str, str]] = [
        {"type": "mrkdwn", "text": f"*Run ID*\n`{req.run_id}`"},
        {"type": "mrkdwn", "text": f"*Risk*\n`{risk_val.upper()}`"},
        {"type": "mrkdwn", "text": f"*Request ID*\n`{req.id[:8]}`"},
        {"type": "mrkdwn", "text": f"*Expires*\n`{expires_str}`"},
    ]
    if req.role:
        fields.append({"type": "mrkdwn", "text": f"*Role*\n`{req.role}`"})

    blocks.append({"type": "section", "fields": fields})

    # Context key/value pairs (up to 8 entries — Slack limit)
    if req.context:
        ctx_items = list(req.context.items())[:8]
        ctx_elements = [
            {"type": "mrkdwn", "text": f"*{k}:* {v}"} for k, v in ctx_items
        ]
        blocks.append({"type": "context", "elements": ctx_elements})

    blocks.append({"type": "divider"})

    # Action buttons — one per option
    button_elements: list[dict[str, Any]] = []
    for option in req.options:
        action_lower = option.lower()
        style: str | None = None
        if action_lower == "approve":
            style = "primary"
        elif action_lower == "reject":
            style = "danger"

        btn: dict[str, Any] = {
            "type": "button",
            "text": {"type": "plain_text", "text": option.capitalize(), "emoji": True},
            # encode request_id in action_id so we can correlate the click
            "action_id": f"hitl_{action_lower}_{req.id[:8]}",
            "value": f"{option}|{req.id}",
        }
        if style:
            btn["style"] = style
        button_elements.append(btn)

    blocks.append({"type": "actions", "elements": button_elements})

    # Footer
    blocks.append({
        "type": "context",
        "elements": [
            {
                "type": "mrkdwn",
                "text": (
                    f"🤖 wire-ai HITL Gate  •  "
                    f"Timeout: {timeout_minutes}m  •  "
                    f"Reply with `approve`, `reject`, or `modify` if buttons are unavailable"
                ),
            }
        ],
    })

    return blocks


def _parse_decision_from_message(
    msg: dict[str, Any],
    req: HITLRequest,
    bot_user_id: str,
) -> HITLDecision | None:
    """
    Inspect a single Slack message dict.
    Returns HITLDecision if it represents a human response, else None.

    Handles:
    - Block Kit interactive button payloads (message subtype bot_message with
      metadata set by the interactivity layer) — Slack posts an updated message
      whose blocks contain the selected action marked as selected/clicked.
    - Plain text replies matching option keywords.
    """
    msg_user = msg.get("user", "")
    msg_bot_id = msg.get("bot_id", "")
    msg_ts = msg.get("ts", "")
    text: str = msg.get("text", "").strip().lower()

    # Skip our own bot messages
    if msg_bot_id and msg_bot_id == bot_user_id:
        return None
    if not msg_user and not text:
        return None

    # Check for button interaction encoded in message metadata
    # When a user clicks a button Slack updates the original message and
    # also posts a new ephemeral/response — we detect it via the metadata
    # field or via a reply message that contains the value.
    metadata = msg.get("metadata", {})
    event_payload = metadata.get("event_payload", {})
    if event_payload:
        action_val: str = event_payload.get("action_value", "")
        if "|" in action_val:
            option, rid = action_val.split("|", 1)
            if rid == req.id:
                action = _resolve_action(option)
                if action:
                    return HITLDecision(
                        request_id=req.id,
                        action=action,
                        actor=f"human:slack:{msg_user or 'unknown'}",
                        notes="",
                    )

    # Plain-text reply matching option keywords
    for option in req.options:
        if option.lower() in text:
            action = _resolve_action(option)
            if action:
                notes = ""
                # Extract trailing notes after the keyword
                after = text[text.index(option.lower()) + len(option):].strip(": -–").strip()
                if after:
                    notes = after
                return HITLDecision(
                    request_id=req.id,
                    action=action,
                    actor=f"human:slack:{msg_user or 'unknown'}",
                    notes=notes,
                )

    return None


def _resolve_action(option: str) -> HITLAction | None:
    """Map an option string to an HITLAction, or None if unrecognised."""
    key = option.lower().strip()
    for prefix, action in _ACTION_MAP.items():
        if key.startswith(prefix):
            return action
    return None


class SlackHITLChannel:
    """
    Slack delivery channel for HITLGate.

    Posts an approval request to a Slack channel and polls
    conversations.history until a human responds or timeout elapses.

    Args:
        channel:         Slack channel ID or name (e.g. "#ops-approvals").
        token:           Bot token. Falls back to SLACK_BOT_TOKEN env var.
        poll_interval:   Seconds between history polls (default 3).
        timeout_minutes: Override; falls back to whatever HITLGate passes.
        timeout_action:  Fallback if gate doesn't pass one explicitly.
    """

    def __init__(
        self,
        channel: str,
        token: str | None = None,
        poll_interval: float = 3.0,
        timeout_minutes: int = 30,
        timeout_action: TimeoutAction = TimeoutAction.ESCALATE,
    ) -> None:
        self.channel = channel.lstrip("#")  # normalise; Slack accepts with or without #
        self._token = token or os.environ.get("SLACK_BOT_TOKEN", "")
        self.poll_interval = poll_interval
        self.timeout_minutes = timeout_minutes
        self.timeout_action = timeout_action

    # ── Public entry point ────────────────────────────────────────────────────

    async def request(
        self,
        req: HITLRequest,
        timeout_minutes: int | None = None,
        timeout_action: TimeoutAction | None = None,
    ) -> HITLDecision:
        """Post the request to Slack and wait for a human response."""
        if not _SLACK_SDK_AVAILABLE or AsyncWebClient is None:
            raise ImportError(
                "slack-sdk is required for Slack HITL. "
                'Install with: pip install "wire-ai[slack]"'
            )

        effective_timeout = timeout_minutes or self.timeout_minutes
        effective_action = timeout_action or self.timeout_action

        client = AsyncWebClient(token=self._token)
        blocks = _build_blocks(req, effective_timeout)

        # Post the approval message
        response = await client.chat_postMessage(
            channel=self.channel,
            text=f"⏸ HITL Approval Required — {req.message}",  # fallback text
            blocks=blocks,
        )
        thread_ts: str = response["ts"]

        log.info(
            "slack_hitl_posted",
            request_id=req.id,
            channel=self.channel,
            thread_ts=thread_ts,
        )

        # Fetch bot's own user ID so we can skip our own messages
        auth_response = await client.auth_test()
        bot_user_id: str = auth_response.get("bot_id", "")

        # Poll for a response
        deadline = datetime.now(timezone.utc).timestamp() + effective_timeout * 60
        oldest = thread_ts  # only look at messages newer than our post

        while datetime.now(timezone.utc).timestamp() < deadline:
            await asyncio.sleep(self.poll_interval)

            history = await client.conversations_history(
                channel=self.channel,
                oldest=oldest,
                limit=20,
            )
            messages: list[dict[str, Any]] = history.get("messages", [])

            for msg in reversed(messages):  # oldest-first scan
                if msg.get("ts", "") == thread_ts:
                    continue  # skip our own original post
                decision = _parse_decision_from_message(msg, req, bot_user_id)
                if decision:
                    log.info(
                        "slack_hitl_decision",
                        request_id=req.id,
                        action=decision.action,
                        actor=decision.actor,
                    )
                    return decision

        # Timeout
        log.warning(
            "slack_hitl_timeout",
            request_id=req.id,
            timeout_minutes=effective_timeout,
            action=effective_action,
        )
        return self._handle_timeout(req, effective_timeout, effective_action)

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _handle_timeout(
        self,
        req: HITLRequest,
        timeout_minutes: int,
        timeout_action: TimeoutAction,
    ) -> HITLDecision:
        if timeout_action == TimeoutAction.ESCALATE:
            raise HITLTimeoutError(req.id, timeout_minutes)
        action = (
            HITLAction.APPROVE
            if timeout_action == TimeoutAction.APPROVE
            else HITLAction.REJECT
        )
        return HITLDecision(
            request_id=req.id,
            action=action,
            actor="wire:timeout",
            notes=f"Auto-{action.value} after {timeout_minutes}m Slack timeout",
        )
