"""Compute dashboard statistics from the activity log.

Pure functions over the event list so they're easy to test. All numbers are
derived from data/activity.jsonl — the same permanent record the History
panel shows, so dashboard and history always agree.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Dict, List

OUTCOME_LABELS = [
    "converted",
    "interested",
    "follow_up",
    "not_interested",
    "opt_out",
    "unclear",
]


def compute_stats(events: List[dict]) -> Dict[str, Any]:
    """Aggregate the funnel: discovered -> contacted -> engaged -> converted.

    `events` is the activity log (any order). Counting rules:
      - calls "answered" = call_started events (the callee picked up;
        providers only fetch our webhook / connect AudioSocket on answer).
      - "no answer" = live calls placed minus calls answered.
      - outcomes come from call_outcome events (Claude classifies each
        finished call's transcript).
      - email replies are NOT tracked (needs inbox access) — the dashboard
        says so rather than guessing.
    """
    searches = 0
    leads_found = 0
    emails = Counter()           # status -> count (sent / drafted / skipped / failed)
    calls_live = 0               # really dialed
    calls_prepared = 0           # drafted in dry-run
    calls_failed = 0
    calls_answered = 0           # callee picked up
    calls_completed = 0          # conversation ran to an end
    call_turns = 0
    demo_turns = 0
    outcomes = Counter()
    recent_outcomes: List[dict] = []
    companies_contacted = set()
    replies = Counter()          # classification -> count
    recent_replies: List[dict] = []
    inbox_syncs = 0

    for e in events:
        kind = e.get("event")
        if kind == "search_started":
            searches += 1
        elif kind == "search_completed":
            leads_found += int(e.get("leads_found") or 0)
        elif kind == "email_outreach":
            status = e.get("status") or "unknown"
            emails[status] += 1
            if e.get("company"):
                companies_contacted.add(e["company"])
        elif kind == "call_placed":
            status = e.get("status") or "unknown"
            if status == "sent":
                calls_live += 1
            elif status == "drafted":
                calls_prepared += 1
            elif status == "failed":
                calls_failed += 1
            if e.get("company"):
                companies_contacted.add(e["company"])
        elif kind == "call_started":
            calls_answered += 1
        elif kind == "call_turn":
            call_turns += 1
        elif kind == "call_ended":
            calls_completed += 1
        elif kind == "call_outcome":
            outcome = e.get("outcome") or "unclear"
            outcomes[outcome] += 1
            recent_outcomes.append(
                {
                    "time": e.get("time"),
                    "company": e.get("company"),
                    "outcome": outcome,
                    "summary": e.get("summary"),
                }
            )
        elif kind == "voice_demo_turn":
            demo_turns += 1
        elif kind == "email_reply":
            cls = e.get("classification") or "other"
            replies[cls] += 1
            recent_replies.append(
                {
                    "time": e.get("time"),
                    "company": e.get("company") or e.get("from"),
                    "classification": cls,
                    "summary": e.get("summary"),
                }
            )
        elif kind == "inbox_sync":
            inbox_syncs += 1

    emails_sent = emails.get("sent", 0)
    emails_drafted = emails.get("drafted", 0)
    no_answer = max(0, calls_live - calls_answered)
    converted = outcomes.get("converted", 0)
    interested = outcomes.get("interested", 0) + outcomes.get("follow_up", 0)

    contacted = emails_sent + emails_drafted + calls_live + calls_prepared
    classified = sum(outcomes.values())
    replies_total = sum(replies.values())
    # Replies are tracked once the inbox connector has run at least once.
    replies_tracked = inbox_syncs > 0 or replies_total > 0

    return {
        "funnel": {
            "leads_found": leads_found,
            "contacted": contacted,
            # picked up the phone OR replied to an email
            "engaged": calls_answered + replies_total,
            "converted": converted,
        },
        "searches": searches,
        "emails": {
            "sent": emails_sent,
            "drafted": emails_drafted,
            "skipped": emails.get("skipped", 0),
            "failed": emails.get("failed", 0),
            "total": sum(emails.values()),
            "replies_tracked": replies_tracked,
            "replies": replies_total,
            "reply_breakdown": {
                k: replies.get(k, 0)
                for k in ["interested", "question", "follow_up", "not_interested", "opt_out", "other"]
            },
            "reply_rate": round(100 * replies_total / emails_sent) if emails_sent else None,
            "no_response": max(0, emails_sent - replies_total) if replies_tracked else None,
        },
        "calls": {
            "dialed": calls_live,
            "prepared": calls_prepared,
            "failed": calls_failed,
            "answered": calls_answered,
            "no_answer": no_answer,
            "completed": calls_completed,
            "turns": call_turns,
            "answer_rate": round(100 * calls_answered / calls_live) if calls_live else None,
        },
        "outcomes": {label: outcomes.get(label, 0) for label in OUTCOME_LABELS},
        "conversion": {
            "converted": converted,
            "interested": interested,
            "not_interested": outcomes.get("not_interested", 0),
            "opt_out": outcomes.get("opt_out", 0),
            "rate": round(100 * converted / classified) if classified else None,
        },
        "companies_contacted": len(companies_contacted),
        "voice_demo_turns": demo_turns,
        # Most recent classified calls / replies, newest first.
        "recent_outcomes": sorted(
            recent_outcomes, key=lambda r: r.get("time") or "", reverse=True
        )[:20],
        "recent_replies": sorted(
            recent_replies, key=lambda r: r.get("time") or "", reverse=True
        )[:20],
    }
