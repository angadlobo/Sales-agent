"""Demo run: exercises the REAL pipeline end-to-end with the Claude layer
simulated (for environments without an ANTHROPIC_API_KEY).

Everything else is real: discovery -> enrichment -> scoring -> compliance ->
email drafting -> activity log -> saved runs -> stats. With a key set, drop
the monkeypatching and the same pipeline does live web research instead.

    python examples/demo_offline.py
"""

from __future__ import annotations

import sales_agent.llm as llm
from sales_agent.config import CampaignConfig, Settings
from sales_agent.models import (
    ConversionPrediction,
    Lead,
    LeadScore,
    Product,
    ScoreBreakdown,
    Targeting,
)

# ── Canned "Claude" responses (what live web research would produce) ────────

DEMO_LEADS = [
    Lead(
        company_name="Hill Country Air & Heat",
        website="https://hillcountryairheat.example.com",
        contact_name="Maria Delgado",
        contact_title="Owner",
        email="maria@hillcountryairheat.example.com",
        phone="+15125550142",
        location="Austin, TX",
        industry="HVAC contractor",
        company_size="~14 technicians",
        rationale="Owner-run HVAC shop still advertising 'call to schedule' with no online booking.",
        opportunity_summary="Growing fast but dispatching by phone and whiteboard; scheduling software directly removes their visible bottleneck.",
        intent_signals=[
            "STRONG: hiring two dispatchers (Indeed posting, May 2026)",
            "STRONG: Google reviews mention double-booked appointments 3x this spring",
            "MEDIUM: website has no online booking, just a phone number",
        ],
        source_url="https://example.com/austin-hvac-directory",
    ),
    Lead(
        company_name="Bluebonnet Plumbing Co.",
        website="https://bluebonnetplumbing.example.com",
        contact_name="James Okafor",
        contact_title="General Manager",
        email="office@bluebonnetplumbing.example.com",
        location="Round Rock, TX",
        industry="Plumbing contractor",
        company_size="~8 technicians",
        rationale="Family plumbing business expanding into a second service area.",
        opportunity_summary="Opening a Georgetown branch; coordinating two territories by phone won't scale.",
        intent_signals=[
            "STRONG: announced second location opening (local news, April 2026)",
            "WEAK: Central Texas construction boom lifting demand",
        ],
        source_url="https://example.com/roundrock-chamber",
    ),
    Lead(
        company_name="Lone Star Electric Services",
        website="https://lonestarelectric.example.com",
        email="info@lonestarelectric.example.com",
        location="Austin, TX",
        industry="Electrical contractor",
        company_size="~25 employees",
        rationale="Mid-size electrical contractor with aging scheduling workflow.",
        opportunity_summary="Job postings reference 'paper work orders' — modernization candidate, though no urgent trigger.",
        intent_signals=["MEDIUM: job posting mentions paper work orders (May 2026)"],
        source_url="https://example.com/austin-business-listings",
    ),
]

DEMO_SCORES = {
    "Hill Country Air & Heat": LeadScore(
        score=84,
        reasoning="Textbook ICP match with two STRONG, recent intent signals: actively hiring dispatchers (they're feeling the scheduling pain enough to spend payroll on it) and customers publicly complaining about double-bookings. 14 techs puts the deal at ~$1,386/mo of value vs ~$99/mo cost per tech — affordable. Owner is named and emailable.",
        buying_signals=["Hiring dispatchers now", "Customer complaints about double-booking", "No online booking on site"],
        risks=["May solve it by just hiring people", "Busy season — short attention window"],
        breakdown=ScoreBreakdown(need=90, buying_intent=88, budget=75, urgency=85, accessibility=80, location_fit=100, competitive_advantage=70),
        prediction=ConversionPrediction(reply_probability=9, meeting_probability=5, conversion_probability=3, estimated_deal_value="$16,632/yr (14 techs × $99/mo)", estimated_sales_cycle="3-6 weeks", confidence="medium"),
        recommended_channel="email — owner's address is published; reference the dispatcher hiring",
    ),
    "Bluebonnet Plumbing Co.": LeadScore(
        score=71,
        reasoning="Second-location expansion is a strong, dated trigger: multi-territory dispatch is exactly when phone-and-paper breaks. Smaller fleet (8 techs) means smaller deal but clear urgency window around the branch opening. Only a general office inbox is published.",
        buying_signals=["Opening second location", "Coordination complexity about to double"],
        risks=["GM may not be the economic buyer", "Office inbox may filter cold email"],
        breakdown=ScoreBreakdown(need=80, buying_intent=75, budget=65, urgency=80, accessibility=55, location_fit=95, competitive_advantage=65),
        prediction=ConversionPrediction(reply_probability=6, meeting_probability=3, conversion_probability=2, estimated_deal_value="$9,504/yr (8 techs × $99/mo)", estimated_sales_cycle="4-8 weeks", confidence="medium"),
        recommended_channel="email — tie the pitch to the Georgetown opening",
    ),
    "Lone Star Electric Services": LeadScore(
        score=52,
        reasoning="Profile fits and the paper-work-orders mention shows outdated process, but there is no active trigger and no named decision-maker — generic info@ inbox only. Worth a light touch, not a priority.",
        buying_signals=["Paper work orders referenced in job post"],
        risks=["No urgency signal", "No named contact — info@ only"],
        breakdown=ScoreBreakdown(need=60, buying_intent=40, budget=70, urgency=35, accessibility=40, location_fit=100, competitive_advantage=60),
        prediction=ConversionPrediction(reply_probability=3, meeting_probability=1, conversion_probability=1, estimated_deal_value="$29,700/yr if all 25 staff licensed", estimated_sales_cycle="2-4 months", confidence="low"),
        recommended_channel="email — low priority, generic inbox",
    ),
}

DEMO_EMAILS = {
    "Hill Country Air & Heat": (
        "saw you're hiring dispatchers",
        "Hi Maria,\n\nNoticed Hill Country Air & Heat is hiring two dispatchers — usually a sign "
        "the scheduling board is overflowing (a few of your recent Google reviews mention "
        "double-booked slots too).\n\nRouteWise handles that load instead: automated booking, "
        "SMS reminders, and live tech tracking, built for shops your size. Crews like yours "
        "typically cut no-shows by a third.\n\nWorth a 15-minute call this week to see if it "
        "fits before the new hires start?",
    ),
    "Bluebonnet Plumbing Co.": (
        "congrats on the georgetown branch",
        "Hi James,\n\nSaw the news about Bluebonnet opening in Georgetown — congrats. Running "
        "two service areas off one phone line is usually where the whiteboard stops working.\n\n"
        "RouteWise gives each territory its own schedule with one dispatch view, plus SMS "
        "reminders and card payments in the field.\n\nOpen to a 15-minute call before the "
        "branch opens to see if it'd help the rollout?",
    ),
    "Lone Star Electric Services": (
        "re: paper work orders",
        "Hi there,\n\nYour recent job posting mentioned paper work orders — if digitizing "
        "scheduling and invoicing is on the roadmap, RouteWise does both in one app built "
        "for electrical contractors.\n\nHappy to show you in 15 minutes if useful.",
    ),
}

NOTES = "(simulated web research notes — with an API key this is real web search output)"


def install_mocks() -> None:
    """Replace the Claude calls with canned responses, leaving everything else real."""

    def fake_research(prompt, **kw):
        return NOTES

    def fake_extract(prompt, schema, **kw):
        name = schema.__name__
        if name == "_LeadList":
            return schema(leads=[l.model_copy(deep=True) for l in DEMO_LEADS])
        if name == "_Contact":
            return schema()  # discovery already found published contacts
        if name == "LeadScore":
            for company, score in DEMO_SCORES.items():
                if company in prompt:
                    return score.model_copy(deep=True)
            return LeadScore(score=50, reasoning="(demo default)")
        if name == "_Email":
            for company, (subject, body) in DEMO_EMAILS.items():
                if company in prompt:
                    return schema(subject=subject, body=body)
            return schema(subject="quick intro", body="(demo email)")
        raise AssertionError(f"unmocked schema {name}")

    def fake_complete(prompt, **kw):
        return "Hi, this is Alex calling from RouteWise — do you have thirty seconds?"

    llm.research = fake_research
    llm.extract = fake_extract
    llm.complete = fake_complete


def main() -> None:
    install_mocks()

    settings = Settings(
        product=Product(
            name="RouteWise",
            description="Scheduling and dispatch app for small home-services contractors.",
            offering_type="product",
            price_point="$99/month per technician",
            ideal_customer="Owner-run HVAC/plumbing/electrical companies with 3-25 technicians.",
            value_props=[
                "Automated appointment reminders cut no-shows",
                "Real-time technician tracking",
                "In-app card payments",
            ],
        ),
        targeting=Targeting(location="Austin, Texas", industry="HVAC and plumbing contractors", max_leads=3),
        campaign=CampaignConfig(min_score_to_contact=65, channels=["email"]),
        dry_run=True,
    )

    from sales_agent import pipeline
    from sales_agent.cli import _print_results

    results = pipeline.run_campaign(settings)
    _print_results(results)


if __name__ == "__main__":
    main()
