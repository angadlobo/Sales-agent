"""Score how likely a lead is to buy, using a weighted buying-probability rubric.

Weights: Need 25% · Buying intent 25% · Budget 15% · Urgency 15% ·
Accessibility 10% · Location fit 5% · Competitive advantage 5%.

Also produces forward-looking estimates (reply / meeting / conversion
probability, deal value, sales cycle) with an honesty-first prompt, and folds
in learnings from past campaign outcomes so scoring improves over time.
"""

from __future__ import annotations

import logging

from . import learning, llm
from .config import Settings
from .models import Lead, LeadScore

logger = logging.getLogger(__name__)

_PROMPT = """Score how likely this lead is to BUY the offer below within 3-12 months.

THE OFFER
- Name: {name}
- Description: {description}
- Ideal customer: {ideal_customer}
- Price: {price}
- Key value: {value_props}

THE LEAD
- Company: {company}
- Industry: {industry}
- Location: {location}
- Company size: {company_size}
- Revenue range (public estimate): {revenue_range}
- Contact: {contact} ({title})
- Reachable via: {channels}
- Why it surfaced: {rationale}
- Opportunity summary: {opportunity}
- Observed intent signals:
{signals}
{learnings}
HOW TO SCORE — fill in the breakdown, each factor 0-100:
- need (25% weight): how badly does this business need the offer?
- buying_intent (25%): how strong and recent are the observed signals?
- budget (15%): can they plausibly afford it at this price?
- urgency (15%): anything pushing them to act soon?
- accessibility (10%): is a decision-maker reachable through the published channels?
- location_fit (5%)
- competitive_advantage (5%): our edge vs whatever they use today.

The headline `score` must equal the weighted combination of the breakdown
(rounded). Be calibrated, not optimistic: a generic profile match with no
intent signals is ~40-50; strong fit + a STRONG signal is 70+; reserve 85+ for
multiple strong signals with clear urgency.

Then predict, honestly and conservatively:
- reply_probability, meeting_probability, conversion_probability (cold
  outreach reply rates are typically 1-10%; only go higher with strong evidence)
- estimated_deal_value: compute from the price and their size when possible
- estimated_sales_cycle
- confidence: low unless the evidence is specific and recent
Also recommend the best first-contact channel from the reachable ones, with a
short reason (`recommended_channel`).

List concrete buying_signals and risks grounded in the data above — no
invented facts."""


def score_lead(lead: Lead, settings: Settings) -> LeadScore:
    p = settings.product

    channels = ", ".join(
        ch for ch, present in [("email", lead.email), ("phone", lead.phone)] if present
    ) or "none published"
    signals = "\n".join(f"  - {s}" for s in lead.intent_signals) or "  - (none observed)"

    insights = learning.get_insights()
    learnings_block = (
        f"\nLEARNINGS FROM PAST OUTCOMES (weigh prospects matching winning "
        f"patterns higher, losing patterns lower):\n{insights}\n"
        if insights
        else ""
    )

    prompt = _PROMPT.format(
        name=p.name,
        description=p.description,
        ideal_customer=p.ideal_customer or "(not specified)",
        price=p.price_point or "(not specified)",
        value_props="; ".join(p.value_props) or "(not specified)",
        company=lead.company_name,
        industry=lead.industry or "(unknown)",
        location=lead.location or "(unknown)",
        company_size=lead.company_size or "(unknown)",
        revenue_range=lead.revenue_range or "(unknown)",
        contact=lead.contact_name or "(unknown)",
        title=lead.contact_title or "(unknown)",
        channels=channels,
        rationale=lead.rationale or "(none)",
        opportunity=lead.opportunity_summary or "(none)",
        signals=signals,
        learnings=learnings_block,
    )
    score = llm.extract(prompt, LeadScore, model=settings.model)
    logger.info("Scored %s: %d", lead.company_name, score.score)
    return score
