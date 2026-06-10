"""Draft and send personalized cold emails."""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

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
SIGNALS: {signals}

Rules:
- 90 words max. Plain, human, specific to this company — no buzzwords or hype.
- Open with a concrete observation about their business, not about us.
- One clear ask: a 15-minute call. No attachments, no images.
- Do NOT include a signature or unsubscribe line; those are appended separately.
- Subject line: under 7 words, lowercase-ish, looks like a person wrote it."""


def draft_email(lead: Lead, score: LeadScore | None, settings: Settings) -> _Email:
    p = settings.product
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
        signals="; ".join(score.buying_signals) if score else "",
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
        _smtp_send(settings, lead.email, email.subject, full_body)
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
    )


def _smtp_send(settings: Settings, to_email: str, subject: str, body: str) -> None:
    cfg = settings.email
    msg = EmailMessage()
    msg["From"] = f"{cfg.from_name} <{cfg.from_email}>" if cfg.from_name else cfg.from_email
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.set_content(body)

    with smtplib.SMTP(cfg.host, cfg.port) as server:
        server.starttls()
        server.login(cfg.username, cfg.password)
        server.send_message(msg)
