"""Place AI voice calls that talk to a prospect and qualify them.

Architecture:
  1. `place_call()` uses the Twilio REST API to dial the lead. Twilio fetches
     TwiML from the webhook server (`voice_server.py`).
  2. The webhook server uses <Gather input="speech"> to transcribe what the
     person says, asks Claude for the next line, and speaks it back with <Say>.
  3. The loop continues until the call ends.

Running real calls needs: Twilio credentials, a phone number, and the webhook
server reachable at a public HTTPS URL (e.g. via ngrok). See README.
"""

from __future__ import annotations

import logging
from typing import Dict, List

from .. import llm
from ..compliance import ComplianceError, Suppression, check_phone, normalize_phone
from ..config import Settings
from ..models import Channel, Lead, LeadScore, OutreachResult, OutreachStatus

logger = logging.getLogger(__name__)


def call_system_prompt(settings: Settings, lead: Lead) -> str:
    """System prompt that defines the voice agent's persona and goal."""
    p = settings.product
    return f"""You are a friendly, concise sales development rep making a cold call on behalf of a company that sells {p.name}.

PRODUCT: {p.description}
VALUE: {"; ".join(p.value_props) or "saves time and money"}
PRICE: {p.price_point or "(share only if asked)"}

YOU ARE CALLING: {lead.contact_name or "the business owner"} at {lead.company_name}.

How to behave on the call:
- Open by identifying yourself and the company, and ask for ~30 seconds.
- Speak in short, natural spoken sentences (one or two at a time). This is a
  phone call, not an email — no bullet points, no markdown.
- Be honest that this is an outreach call. If they're not interested or ask to
  be removed, thank them and end politely.
- Goal: gauge interest and, if there's a fit, book a short follow-up call or
  get permission to email details.
- Never be pushy, never invent facts about the product, never pretend to be human if asked directly whether you're an AI — say you're an automated assistant.
- Keep the whole call under ~2 minutes."""


def opening_line(settings: Settings, lead: Lead) -> str:
    """Generate the first thing the agent says when the call connects."""
    return llm.complete(
        f"Give ONLY the single opening spoken sentence for the call. "
        f"Be warm and brief.\n\n{call_system_prompt(settings, lead)}",
        model=settings.fast_model,
        max_tokens=120,
    ).strip().strip('"')


def next_reply(
    settings: Settings,
    lead: Lead,
    history: List[Dict[str, str]],
) -> str:
    """Given the conversation so far, return the agent's next spoken line.

    `history` is a list of {"role": "user"|"assistant", "content": str}, where
    "user" is the prospect (from speech-to-text) and "assistant" is the agent.
    """
    client = llm.get_client()
    resp = client.messages.create(
        model=settings.model,
        max_tokens=200,
        system=call_system_prompt(settings, lead),
        messages=history or [{"role": "user", "content": "(call connected)"}],
    )
    return llm._text(resp).strip()


def place_call(
    lead: Lead,
    score: LeadScore | None,
    settings: Settings,
    suppression: Suppression,
) -> OutreachResult:
    """Dial the lead (or simulate it in dry-run mode)."""
    try:
        check_phone(lead.phone, suppression)
    except ComplianceError as exc:
        return OutreachResult(
            channel=Channel.VOICE, status=OutreachStatus.SKIPPED, detail=str(exc)
        )

    opener = opening_line(settings, lead)

    if settings.dry_run:
        logger.info("[DRY RUN] would call %s — opener: %r", lead.phone, opener)
        return OutreachResult(
            channel=Channel.VOICE,
            status=OutreachStatus.DRAFTED,
            detail="dry run — not dialed",
            body=opener,
        )

    if not settings.voice.is_configured:
        return OutreachResult(
            channel=Channel.VOICE,
            status=OutreachStatus.FAILED,
            detail="Twilio not configured (set TWILIO_* and VOICE_WEBHOOK_BASE_URL)",
            body=opener,
        )

    try:
        sid = _twilio_dial(settings, lead)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Twilio dial failed for %s", lead.phone)
        return OutreachResult(
            channel=Channel.VOICE,
            status=OutreachStatus.FAILED,
            detail=f"Twilio error: {exc}",
            body=opener,
        )

    return OutreachResult(
        channel=Channel.VOICE,
        status=OutreachStatus.SENT,
        detail=f"call placed (sid={sid})",
        body=opener,
    )


def _twilio_dial(settings: Settings, lead: Lead) -> str:
    from twilio.rest import Client  # imported lazily so email-only users don't need twilio

    v = settings.voice
    client = Client(v.account_sid, v.auth_token)
    # Twilio will GET this URL for TwiML when the callee answers. We pass the
    # lead's company so the server can look up context for the conversation.
    url = f"{v.webhook_base_url.rstrip('/')}/voice/answer?company={lead.company_name}"
    call = client.calls.create(
        to=normalize_phone(lead.phone),
        from_=v.from_number,
        url=url,
        method="POST",
    )
    return call.sid
