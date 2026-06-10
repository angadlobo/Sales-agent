"""Fill in missing contact details (mainly business email) for a lead.

Only surfaces contact info a business has *published for inbound contact*.
See README's "Legal & ethical use" section before running this against real
companies.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel

from . import llm
from .config import Settings
from .models import Lead

logger = logging.getLogger(__name__)


class _Contact(BaseModel):
    email: str | None = None
    phone: str | None = None
    contact_name: str | None = None
    contact_title: str | None = None


_PROMPT = """Find the publicly listed business contact details for this company.

Company: {company}
Website: {website}
Location: {location}

Use web search. Return ONLY contact details the company itself publishes for
inbound contact (e.g. a "contact us" page, a public business directory, a
listed general inbox like info@ or sales@). Do NOT guess, pattern-match, or
construct an email address. If nothing is publicly published, leave fields null.

Write a short note with what you found and the source URL."""


def enrich_lead(lead: Lead, settings: Settings) -> Lead:
    """Return a copy of the lead with email/phone filled in where found."""
    if lead.email and lead.phone:
        return lead

    notes = llm.research(
        _PROMPT.format(
            company=lead.company_name,
            website=lead.website or "(unknown)",
            location=lead.location or "(unknown)",
        ),
        model=settings.model,
    )
    contact = llm.extract(
        f"Extract the published contact details from these notes. "
        f"Never fabricate. Leave null if absent.\n\nNOTES:\n{notes}",
        _Contact,
        model=settings.fast_model,
    )

    return lead.model_copy(
        update={
            "email": lead.email or contact.email,
            "phone": lead.phone or contact.phone,
            "contact_name": lead.contact_name or contact.contact_name,
            "contact_title": lead.contact_title or contact.contact_title,
        }
    )
