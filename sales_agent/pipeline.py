"""Orchestrates the full campaign: discover -> enrich -> score -> outreach."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional

from . import activity, discovery, enrichment, scoring, storage
from .compliance import Suppression
from .config import Settings
from .models import Channel, QualifiedLead
from .outreach import email_outreach, voice_outreach

logger = logging.getLogger(__name__)


def run_campaign(
    settings: Settings,
    *,
    suppression_path: Optional[str | Path] = None,
    save: bool = True,
) -> List[QualifiedLead]:
    """Run the end-to-end pipeline and return the qualified leads.

    Honors settings.dry_run — in dry-run mode no email is sent and no call is
    placed; outreach is drafted only.
    """
    suppression = Suppression(suppression_path)
    sent_count = 0

    # 1. Discover candidate companies on the web.
    leads = discovery.discover_leads(settings)

    qualified: List[QualifiedLead] = []
    for lead in leads:
        # 2. Fill in published contact details.
        lead = enrichment.enrich_lead(lead, settings)

        # 3. Score likelihood to buy.
        score = scoring.score_lead(lead, settings)
        ql = QualifiedLead(lead=lead, score=score)

        # 4. Reach out if the lead clears the bar and we're under the daily cap.
        if score.score >= settings.campaign.min_score_to_contact:
            if sent_count >= settings.campaign.daily_send_limit:
                logger.info("Daily send limit reached; skipping outreach for %s", lead.company_name)
            else:
                results = _reach_out(ql, settings, suppression)
                ql.outreach.extend(results)
                if any(r.status.value in {"sent", "drafted"} for r in results):
                    sent_count += 1
        else:
            logger.info(
                "%s scored %d (< %d); not contacting",
                lead.company_name,
                score.score,
                settings.campaign.min_score_to_contact,
            )

        qualified.append(ql)

    qualified.sort(key=lambda q: q.best_score, reverse=True)

    if save:
        storage.save_run(qualified, settings.ensure_data_dir())

    return qualified


def _reach_out(ql: QualifiedLead, settings: Settings, suppression: Suppression):
    results = []
    for channel in settings.campaign.channels:
        if channel == Channel.EMAIL.value:
            result = email_outreach.send_email(ql.lead, ql.score, settings, suppression)
        elif channel == Channel.VOICE.value:
            result = voice_outreach.place_call(ql.lead, ql.score, settings, suppression)
        else:
            logger.warning("Unknown channel %r — skipping", channel)
            continue
        results.append(result)
        # Permanent record of what was said/written, reviewable in the
        # web UI's History panel (same log the webapp writes to).
        activity.log(
            "call_placed" if channel == Channel.VOICE.value else "email_outreach",
            source="cli",
            company=ql.lead.company_name,
            contact=ql.lead.contact_name,
            to=ql.lead.phone if channel == Channel.VOICE.value else ql.lead.email,
            status=result.status.value,
            live=not settings.dry_run,
            subject=result.subject,
            body=result.body,
            detail=result.detail,
        )
    return results
