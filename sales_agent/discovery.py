"""Find potential customers on the open web using Claude + web search."""

from __future__ import annotations

import logging
from typing import List

from pydantic import BaseModel

from . import llm
from .config import Settings
from .models import Lead

logger = logging.getLogger(__name__)


class _LeadList(BaseModel):
    """Wrapper so structured-output extraction returns a JSON object, not a bare array."""

    leads: List[Lead]


_RESEARCH_PROMPT = """You are a B2B lead-generation researcher.

I sell this product/service:
- Name: {name}
- Description: {description}
- Ideal customer: {ideal_customer}

Find real businesses that fit this ideal customer profile.
Targeting:
- Location: {location}
- Industry: {industry}
- Number of leads wanted: {max_leads}

Use web search to find real, currently-operating companies. For each one,
gather: company name, website, location, industry, and (if publicly listed on
their site or a directory) a contact name, title, business email, and business
phone. Only report contact details that are genuinely published by the business
for inbound contact — never invent or guess an email or phone number.

For each company, note one specific reason it matches the ideal customer
profile (the "rationale"), citing what you saw.

Write up your findings as a clear list. Include the source URL for each company."""


_EXTRACT_PROMPT = """From the research notes below, extract the companies as structured data.

Rules:
- Only include companies that were actually found in the notes.
- Leave a field null if the notes don't contain it. NEVER fabricate emails or phone numbers.
- Keep `rationale` to one specific sentence.

RESEARCH NOTES:
{notes}"""


def discover_leads(settings: Settings) -> List[Lead]:
    """Return a list of candidate leads matching the targeting criteria."""
    p = settings.product
    t = settings.targeting

    logger.info("Researching leads for %r in %s / %s", p.name, t.location, t.industry)
    notes = llm.research(
        _RESEARCH_PROMPT.format(
            name=p.name,
            description=p.description,
            ideal_customer=p.ideal_customer or "(not specified)",
            location=t.location or "(anywhere)",
            industry=t.industry or "(any industry)",
            max_leads=t.max_leads,
        ),
        model=settings.model,
    )

    result = llm.extract(
        _EXTRACT_PROMPT.format(notes=notes),
        _LeadList,
        model=settings.model,
    )
    leads = result.leads[: t.max_leads]
    logger.info("Discovered %d leads", len(leads))
    return leads
