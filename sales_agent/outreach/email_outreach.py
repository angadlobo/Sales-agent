"""Draft and send personalized cold emails."""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage
from email.utils import make_msgid

from pydantic import BaseModel

from .. import llm
from ..compliance import ComplianceError, Suppression, check_email, email_footer
from ..config import Settings
from ..models import Channel, Lead, LeadScore, OutreachResult, OutreachStatus

logger = logging.getLogger(__name__)


class _Email(BaseModel):
    subject: str
    body: str


_PROMPT = """Write a short, genuine cold outreach email.

FROM: {from_name}, selling {product_name}.
PRODUCT: {product_desc}
KEY VALUE: {value_props}

TO: {contact} at {company} ({industry}, {location})
WHY THEM: {reasoning}
OPPORTUNITY: {opportunity}
OBSERVED SIGNALS (use the most specific one as your opener if it's natural):
{signals}

Rules:
- 90 words max. Plain, human, specific to this company — no buzzwords, hype,
  or generic sales language ("I hope this finds you well", "quick question").
- Open with a concrete observation about THEIR business (a signal above), not about us.
- One clear ask: a 15-minute call. No attachments, no images.
- Do NOT include a signature or unsubscribe line; those are appended separately.
- Subject line: under 7 words, lowercase-ish, looks like a person wrote it."""


def draft_email(lead: Lead, score: LeadScore | None, settings: Settings) -> _Email:
    p = settings.product
    signals = [*lead.intent_signals, *(score.buying_signals if score else [])]
    prompt = _PROMPT.format(
        from_name=settings.email.from_name or "Sales",
        product_name=p.name,
        product_desc=p.description,
        value_props="; ".join(p.value_props) or "(none given)",
        contact=lead.contact_name or "there",
        company=lead.company_name,
        industry=lead.industry or "their industry",
        location=lead.location or "",
        reasoning=score.reasoning if score else "(not scored)",
        opportunity=lead.opportunity_summary or "(not analyzed)",
        signals="\n".join(f"- {s}" for s in signals) or "- (none observed)",
    )
    return llm.extract(prompt, _Email, model=settings.model)


def send_email(
    lead: Lead,
    score: LeadScore | None,
    settings: Settings,
    suppression: Suppression,
) -> OutreachResult:
    """Draft, then send (or simulate sending in dry-run mode)."""
    try:
        check_email(lead.email, suppression)
    except ComplianceError as exc:
        return OutreachResult(
            channel=Channel.EMAIL, status=OutreachStatus.SKIPPED, detail=str(exc)
        )

    email = draft_email(lead, score, settings)
    footer = email_footer(settings.email.from_name or "Sales")
    full_body = email.body + footer

    if settings.dry_run:
        logger.info("[DRY RUN] would email %s: %r", lead.email, email.subject)
        return OutreachResult(
            channel=Channel.EMAIL,
            status=OutreachStatus.DRAFTED,
            detail="dry run — not sent",
            subject=email.subject,
            body=full_body,
        )

    if not settings.email.is_configured:
        return OutreachResult(
            channel=Channel.EMAIL,
            status=OutreachStatus.FAILED,
            detail="SMTP not configured (set SMTP_* env vars)",
            subject=email.subject,
            body=full_body,
        )

    try:
        message_id = _smtp_send(settings, lead.email, email.subject, full_body)
    except Exception as exc:  # noqa: BLE001 — surface any SMTP failure to the result
        logger.exception("SMTP send failed for %s", lead.email)
        return OutreachResult(
            channel=Channel.EMAIL,
            status=OutreachStatus.FAILED,
            detail=f"SMTP error: {exc}",
            subject=email.subject,
            body=full_body,
        )

    return OutreachResult(
        channel=Channel.EMAIL,
        status=OutreachStatus.SENT,
        detail=f"sent to {lead.email}",
        subject=email.subject,
        body=full_body,
        message_id=message_id,
    )


def _smtp_send(settings: Settings, to_email: str, subject: str, body: str) -> str:
    """Send and return the Message-ID, which the inbox connector uses to
    recognize replies (via their In-Reply-To / References headers)."""
    cfg = settings.email
    msg = EmailMessage()
    msg["From"] = f"{cfg.from_name} <{cfg.from_email}>" if cfg.from_name else cfg.from_email
    msg["To"] = to_email
    msg["Subject"] = subject
    domain = cfg.from_email.split("@", 1)[-1] if cfg.from_email and "@" in cfg.from_email else None
    message_id = make_msgid(domain=domain)
    msg["Message-ID"] = message_id
    msg.set_content(body)

    with smtplib.SMTP(cfg.host, cfg.port) as server:
        server.starttls()
        server.login(cfg.username, cfg.password)
        server.send_message(msg)
    return message_id
