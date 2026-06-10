"""End-to-end demo. Runs in dry-run mode: discovers and scores real leads via
web search, drafts outreach, but sends nothing.

Requires ANTHROPIC_API_KEY in the environment (or a .env file).

    python examples/run_demo.py
"""

import logging

from sales_agent.config import CampaignConfig, Settings
from sales_agent.models import Product, Targeting
from sales_agent.pipeline import run_campaign

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

settings = Settings(
    product=Product(
        name="RouteWise",
        description=(
            "Scheduling and dispatch app for small home-services contractors. "
            "Cuts admin time and reduces missed appointments."
        ),
        price_point="$79/month per technician",
        ideal_customer="Owner-run HVAC and plumbing companies with 3-25 technicians.",
        value_props=[
            "Automated appointment reminders cut no-shows",
            "Real-time technician tracking",
            "In-app card payments to get paid faster",
        ],
    ),
    targeting=Targeting(location="Austin, Texas", industry="HVAC contractors", max_leads=5),
    campaign=CampaignConfig(min_score_to_contact=60, channels=["email"]),
    dry_run=True,  # nothing is actually sent
)

results = run_campaign(settings)

print("\n=== RESULTS ===")
for ql in results:
    print(f"\n[{ql.best_score}] {ql.lead.company_name} — {ql.lead.email or 'no email'}")
    if ql.score:
        print(f"   reason: {ql.score.reasoning}")
    for o in ql.outreach:
        print(f"   {o.channel.value} ({o.status.value}): {o.subject or o.detail}")
        if o.body:
            print(f"   ---\n   {o.body}\n   ---")
