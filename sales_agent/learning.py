"""Self-improvement loop: learn from past outcomes, refine future targeting.

Analyzes the permanent activity record (call outcomes, email replies, scores)
and distills patterns — which industries, sizes, locations, signals, and
channels actually converted vs flopped. The distilled insights are cached in
data/insights.md and automatically injected into the discovery and scoring
prompts, so every campaign learns from the ones before it.

Refresh manually:           python -m sales_agent.cli learn
Refreshes automatically:    after enough new outcomes accumulate (see
                            maybe_refresh_insights).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from . import activity
from .config import DEFAULT_MODEL

logger = logging.getLogger(__name__)

INSIGHTS_PATH = Path("data") / "insights.md"
STATE_PATH = Path("data") / "insights_state.json"
# Refresh automatically once this many new outcomes have arrived.
AUTO_REFRESH_EVERY = 5
# Don't bother analyzing until there's at least this much signal.
MIN_OUTCOMES = 3

_PROMPT = """You are analyzing the outcomes of past sales outreach to improve future targeting.

Below is the outcome history: each entry is a contacted prospect with what we
knew about them and how it ended (call outcome or email reply classification).

OUTCOME HISTORY:
{history}

Analyze wins (converted / interested / follow_up) versus losses (not_interested
/ opt_out / no answer) and identify patterns across: industry, company size,
location, decision-maker role, outreach channel, lead score, and the
buying-intent signals that were present.

Write a SHORT brief (under 250 words) for the lead researcher and scorer:
1. PRIORITIZE: 3-5 concrete traits of prospects that responded well.
2. AVOID/DOWNRANK: traits that consistently went nowhere.
3. CHANNEL & APPROACH: which first-touch worked better and any timing notes.

Rules: only claim a pattern if it appears at least twice; with thin data, say
"limited data" and keep claims tentative. No fluff — every line must change
how the next search or score is done."""


def get_insights() -> str:
    """Cached insights brief, or '' when none has been generated yet."""
    try:
        if INSIGHTS_PATH.exists():
            return INSIGHTS_PATH.read_text(encoding="utf-8").strip()
    except Exception:  # noqa: BLE001
        logger.exception("Could not read insights file")
    return ""


def _outcome_history(events: list[dict]) -> list[dict]:
    """Flatten outcome-bearing events into compact records for analysis."""
    # company -> context picked up from outreach events
    context: dict[str, dict] = {}
    history: list[dict] = []
    for e in events:
        kind = e.get("event")
        company = e.get("company")
        if kind in ("email_outreach", "call_placed") and company:
            context.setdefault(company, {})["channel"] = (
                "call" if kind == "call_placed" else "email"
            )
        elif kind == "call_outcome" and company:
            history.append(
                {
                    "company": company,
                    "channel": "call",
                    "result": e.get("outcome"),
                    "summary": e.get("summary"),
                }
            )
        elif kind == "email_reply" and company:
            history.append(
                {
                    "company": company,
                    "channel": "email",
                    "result": e.get("classification"),
                    "summary": e.get("summary"),
                }
            )
    return history


def _enrich_history_from_runs(history: list[dict]) -> list[dict]:
    """Attach lead details (industry, size, location, score) from saved runs."""
    leads: dict[str, dict] = {}
    data_dir = Path("data")
    if data_dir.exists():
        for run in sorted(data_dir.glob("run-*.json")):
            try:
                for ql in json.loads(run.read_text(encoding="utf-8")):
                    lead = ql.get("lead", {})
                    name = lead.get("company_name")
                    if not name:
                        continue
                    leads[name] = {
                        "industry": lead.get("industry"),
                        "location": lead.get("location"),
                        "company_size": lead.get("company_size"),
                        "contact_title": lead.get("contact_title"),
                        "intent_signals": lead.get("intent_signals"),
                        "score": (ql.get("score") or {}).get("score"),
                    }
            except Exception:  # noqa: BLE001
                continue
    for h in history:
        h.update({k: v for k, v in leads.get(h["company"], {}).items() if v})
    return history


def refresh_insights(model: str = DEFAULT_MODEL) -> dict:
    """Re-analyze all outcomes and rewrite data/insights.md.

    Returns {"outcomes": n, "insights": text} or {"error"/"note": ...}.
    """
    from . import llm  # local import keeps module import cheap

    events = activity.read(limit=100_000)
    history = _enrich_history_from_runs(_outcome_history(events))
    if len(history) < MIN_OUTCOMES:
        return {
            "note": f"Only {len(history)} outcome(s) recorded so far — need at "
            f"least {MIN_OUTCOMES} before patterns mean anything.",
            "outcomes": len(history),
        }

    text = llm.complete(
        _PROMPT.format(history=json.dumps(history, indent=1, default=str)),
        model=model,
        max_tokens=1500,
    ).strip()

    INSIGHTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    INSIGHTS_PATH.write_text(text, encoding="utf-8")
    STATE_PATH.write_text(json.dumps({"outcomes_at_refresh": len(history)}))
    activity.log("insights_refreshed", outcomes_analyzed=len(history))
    logger.info("Insights refreshed from %d outcomes", len(history))
    return {"outcomes": len(history), "insights": text}


def maybe_refresh_insights(model: str = DEFAULT_MODEL) -> None:
    """Refresh automatically once enough NEW outcomes have accumulated."""
    try:
        events = activity.read(limit=100_000)
        count = len(_outcome_history(events))
        last = 0
        if STATE_PATH.exists():
            last = int(json.loads(STATE_PATH.read_text()).get("outcomes_at_refresh", 0))
        if count >= MIN_OUTCOMES and count - last >= AUTO_REFRESH_EVERY:
            refresh_insights(model)
    except Exception:  # noqa: BLE001 - learning must never break a campaign
        logger.exception("Auto insight refresh failed")
