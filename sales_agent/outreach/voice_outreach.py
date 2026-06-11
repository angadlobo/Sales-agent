"""Place AI voice calls that talk to a prospect and qualify them.

Architecture:
  1. `place_call()` dials the lead through a telephony provider's REST API.
     Supported: Twilio and SignalWire — both speak the same 2010-04-01 API,
     so we call it directly over HTTP (no provider SDK needed).
  2. The provider fetches TwiML/LaML from the webhook server
     (`voice_server.py`), which uses <Gather input="speech"> to transcribe
     what the person says, asks Claude for the next line, and speaks it back.
  3. The loop continues until the call ends.

Real phone calls need provider credentials, a phone number, and the webhook
server reachable at a public HTTPS URL (e.g. via ngrok). For a 100% free way
to talk to the agent, use the browser-voice page in the web UI instead
(`python -m sales_agent.webapp`) — it uses the browser's own speech engine
and no telephony at all.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Literal

from pydantic import BaseModel, Field

from .. import llm
from ..compliance import ComplianceError, Suppression, check_phone, normalize_phone
from ..config import Settings
from ..models import Channel, Lead, LeadScore, OutreachResult, OutreachStatus

logger = logging.getLogger(__name__)


class CallOutcome(BaseModel):
    """How a finished call went — feeds the dashboard funnel."""

    outcome: Literal[
        "converted",       # agreed to buy / signed up / booked a meeting
        "interested",      # positive, wants more info or a follow-up
        "follow_up",       # asked to be called back later / wrong moment
        "not_interested",  # clear no
        "opt_out",         # asked not to be contacted again
        "unclear",         # too short / ambiguous to judge
    ]
    summary: str = Field(description="One sentence: what happened on the call")


def classify_outcome(
    settings: Settings, lead: Lead, history: List[Dict[str, str]]
) -> CallOutcome:
    """Classify a finished call's transcript so the dashboard can count it."""
    transcript = "\n".join(
        f"{'Agent' if m['role'] == 'assistant' else 'Prospect'}: {m['content']}"
        for m in history
    )
    return llm.extract(
        "Classify the outcome of this sales call with "
        f"{lead.company_name}. Judge ONLY from what the prospect actually said — "
        "do not be optimistic. A call is 'converted' only on an explicit yes "
        "(booked meeting, agreed to sign up). If the prospect never spoke or the "
        "call cut off early, use 'unclear'.\n\n"
        f"TRANSCRIPT:\n{transcript}",
        CallOutcome,
        model=settings.fast_model,
    )


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
            detail=(
                "voice provider not configured (set VOICE_PROVIDER plus "
                "VOICE_ACCOUNT_SID / VOICE_AUTH_TOKEN / VOICE_FROM_NUMBER / "
                "VOICE_WEBHOOK_BASE_URL; SignalWire also needs SIGNALWIRE_SPACE_URL; "
                "selfhosted needs SELFHOSTED_VOICE_URL — see docs/SELF_HOSTED_CALLS.md)"
            ),
            body=opener,
        )

    try:
        if settings.voice.provider == "selfhosted":
            sid = _selfhosted_dial(settings, lead)
        else:
            sid = _rest_dial(settings, lead)
    except Exception as exc:  # noqa: BLE001
        logger.exception("%s dial failed for %s", settings.voice.provider, lead.phone)
        return OutreachResult(
            channel=Channel.VOICE,
            status=OutreachStatus.FAILED,
            detail=f"{settings.voice.provider} error: {exc}",
            body=opener,
        )

    return OutreachResult(
        channel=Channel.VOICE,
        status=OutreachStatus.SENT,
        detail=f"call placed (sid={sid})",
        body=opener,
    )


def _selfhosted_dial(settings: Settings, lead: Lead) -> str:
    """Start a call through the self-hosted voice server (see outreach/selfhosted/)."""
    import json
    import urllib.request

    url = settings.voice.selfhosted_url.rstrip("/") + "/call"
    payload = json.dumps(
        {
            "phone": normalize_phone(lead.phone),
            "company": lead.company_name,
            "contact_name": lead.contact_name,
        }
    ).encode()
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode())
    if "error" in data:
        raise RuntimeError(data["error"])
    return data.get("call_id", "unknown")


def _rest_dial(settings: Settings, lead: Lead) -> str:
    """Create an outbound call via the provider's Calls endpoint.

    Twilio and SignalWire share this exact API shape (form-encoded POST with
    basic auth), so one function covers both with no SDK dependency.
    """
    import base64
    import json
    import urllib.parse
    import urllib.request

    v = settings.voice
    # The provider POSTs here for TwiML when the callee answers; we pass the
    # company name so the webhook server has conversation context.
    answer_url = (
        f"{v.webhook_base_url.rstrip('/')}/voice/answer"
        f"?company={urllib.parse.quote(lead.company_name)}"
    )
    endpoint = f"{v.api_base}/Accounts/{v.account_sid}/Calls.json"
    payload = urllib.parse.urlencode(
        {
            "To": normalize_phone(lead.phone),
            "From": v.from_number,
            "Url": answer_url,
            "Method": "POST",
        }
    ).encode()
    auth = base64.b64encode(f"{v.account_sid}:{v.auth_token}".encode()).decode()
    req = urllib.request.Request(
        endpoint,
        data=payload,
        headers={
            "Authorization": f"Basic {auth}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode())
    return data.get("sid", "unknown")
