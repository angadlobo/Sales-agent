"""Find potential BUYERS on the open web using Claude + web search.

The research prompt runs a buyer-intelligence process, not a name-collecting
one: understand the offer, scan the market, hunt for active buying-intent
signals, then collect leads with the evidence attached. Learnings from past
campaign outcomes (sales_agent.learning) are injected so the search gets
better as results come in.
"""

from __future__ import annotations

import logging
from typing import List

from pydantic import BaseModel

from . import learning, llm
from .config import Settings
from .models import Lead

logger = logging.getLogger(__name__)


class _LeadList(BaseModel):
    """Wrapper so structured-output extraction returns a JSON object, not a bare array."""

    leads: List[Lead]


_RESEARCH_PROMPT = """You are an autonomous sales-intelligence and lead-generation researcher.
Your mission: identify the businesses MOST LIKELY TO PURCHASE this offer in the
next 3-12 months — not just businesses that vaguely match. Quality over quantity;
optimize for conversions, not volume.

THE OFFER ({offering_type}):
- Name: {name}
- Description: {description}
- Price: {price_point}
- Ideal customer: {ideal_customer}
- Key value: {value_props}

TARGETING:
- Location: {location}
- Industry: {industry}
- Leads wanted: {max_leads}
{learnings}
PROCESS — work through these phases:

PHASE 1 — UNDERSTAND THE OFFER. Briefly reason about: who needs this, who can
afford it, what pain it removes, and who is most likely to buy SOON.

PHASE 2 — MARKET DISCOVERY. Use web search across public sources: company
websites, business directories, industry associations, local news, review
platforms, job postings.

PHASE 3 — BUYING-INTENT DETECTION. For each candidate, look for signals and
tag each one STRONG / MEDIUM / WEAK with its evidence:
- STRONG: hiring activity, expansion/new locations, funding or growth news,
  technology upgrades, customer complaints about a problem this offer solves,
  visible operational bottlenecks.
- MEDIUM: outdated systems or website, weak online presence, manual processes,
  rising local competition, poor reviews on a relevant dimension.
- WEAK: favorable industry trends, seasonal demand, growth in their area.
Prefer candidates with at least one STRONG or two MEDIUM signals.

PHASE 4 — LEAD COLLECTION. For each business that survives Phase 3, record:
company name, website, location, industry, approximate company size, rough
revenue range IF publicly reported anywhere, decision-maker name and title if
publicly listed, business email and phone ONLY if genuinely published by the
business for inbound contact, a one-sentence rationale, a 1-2 sentence
opportunity summary (why there's money on the table), the tagged intent
signals with evidence, and the source URL.

RULES:
- Real, currently-operating businesses only.
- NEVER invent or pattern-guess contact details, revenue, or any fact.
- If you can't verify something, say so and leave it out.
- Explain every intent signal you claim — cite what you actually saw.

Write your findings as a clear ranked list, strongest prospects first."""


_EXTRACT_PROMPT = """From the research notes below, extract the companies as structured data.

Rules:
- Only include companies that were actually found in the notes.
- Leave a field null if the notes don't contain it. NEVER fabricate emails,
  phone numbers, company sizes, or revenue figures.
- `intent_signals`: copy each tagged signal as one string, keeping its
  STRONG/MEDIUM/WEAK tag and evidence, e.g.
  "STRONG: hiring 2 dispatchers (Indeed posting, May 2026)".
- Keep `rationale` to one specific sentence and `opportunity_summary` to 1-2.
- Preserve the ranking order from the notes (strongest first).

RESEARCH NOTES:
{notes}"""


def discover_leads(settings: Settings) -> List[Lead]:
    """Return a list of candidate leads matching the targeting criteria."""
    p = settings.product
    t = settings.targeting

    insights = learning.get_insights()
    learnings_block = (
        f"\nLEARNINGS FROM PAST CAMPAIGN OUTCOMES (prioritize prospects matching "
        f"these patterns):\n{insights}\n"
        if insights
        else ""
    )

    logger.info("Researching leads for %r in %s / %s", p.name, t.location, t.industry)
    notes = llm.research(
        _RESEARCH_PROMPT.format(
            offering_type=p.offering_type or "product/service",
            name=p.name,
            description=p.description,
            price_point=p.price_point or "(not specified)",
            ideal_customer=p.ideal_customer or "(not specified)",
            value_props="; ".join(p.value_props) or "(not specified)",
            location=t.location or "(anywhere)",
            industry=t.industry or "(any industry)",
            max_leads=t.max_leads,
            learnings=learnings_block,
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
