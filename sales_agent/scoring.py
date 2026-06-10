"""Score how likely a lead is to buy, given the product."""

from __future__ import annotations

import logging

from . import llm
from .config import Settings
from .models import Lead, LeadScore

logger = logging.getLogger(__name__)

_PROMPT = """Score how likely this lead is to buy the product below, on a 0-100 scale.

PRODUCT
- Name: {name}
- Description: {description}
- Ideal customer: {ideal_customer}
- Price: {price}

LEAD
- Company: {company}
- Industry: {industry}
- Location: {location}
- Contact: {contact} ({title})
- Why it surfaced: {rationale}

Judge fit against the ideal customer profile, apparent need for the product,
and how reachable the decision-maker is. Be calibrated: a generic match is ~50,
a strong fit with a clear need is 75+, a weak/uncertain fit is below 40.
List concrete buying signals and risks you can infer."""


def score_lead(lead: Lead, settings: Settings) -> LeadScore:
    p = settings.product
    prompt = _PROMPT.format(
        name=p.name,
        description=p.description,
        ideal_customer=p.ideal_customer or "(not specified)",
        price=p.price_point or "(not specified)",
        company=lead.company_name,
        industry=lead.industry or "(unknown)",
        location=lead.location or "(unknown)",
        contact=lead.contact_name or "(unknown)",
        title=lead.contact_title or "(unknown)",
        rationale=lead.rationale or "(none)",
    )
    score = llm.extract(prompt, LeadScore, model=settings.model)
    logger.info("Scored %s: %d", lead.company_name, score.score)
    return score
