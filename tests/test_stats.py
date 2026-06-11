"""Tests for the dashboard stats computation (pure functions, no network)."""

from sales_agent.stats import compute_stats


def _ev(event, **kw):
    return {"time": "2026-06-11T00:00:00+00:00", "event": event, **kw}


def test_empty_log():
    s = compute_stats([])
    assert s["funnel"] == {"leads_found": 0, "contacted": 0, "engaged": 0, "converted": 0}
    assert s["conversion"]["rate"] is None
    assert s["calls"]["answer_rate"] is None


def test_full_funnel():
    events = [
        _ev("search_started"),
        _ev("search_completed", leads_found=10),
        # 3 emails: 2 sent, 1 skipped
        _ev("email_outreach", status="sent", company="A"),
        _ev("email_outreach", status="sent", company="B"),
        _ev("email_outreach", status="skipped", company="C"),
        # 4 live calls: 2 answered (started), 2 never picked up
        _ev("call_placed", status="sent", company="A"),
        _ev("call_placed", status="sent", company="B"),
        _ev("call_placed", status="sent", company="D"),
        _ev("call_placed", status="sent", company="E"),
        _ev("call_started", call_sid="1", company="A"),
        _ev("call_started", call_sid="2", company="B"),
        _ev("call_turn", call_sid="1", company="A", user_said="hi", agent_said="hello"),
        _ev("call_ended", call_sid="1", company="A", reason="agent said goodbye"),
        _ev("call_ended", call_sid="2", company="B", reason="caller hung up"),
        _ev("call_outcome", call_sid="1", company="A", outcome="converted", summary="Booked a demo"),
        _ev("call_outcome", call_sid="2", company="B", outcome="not_interested", summary="Said no"),
    ]
    s = compute_stats(events)

    assert s["funnel"]["leads_found"] == 10
    assert s["funnel"]["contacted"] == 6  # 2 sent emails + 4 dialed calls
    assert s["funnel"]["engaged"] == 2
    assert s["funnel"]["converted"] == 1

    assert s["calls"]["dialed"] == 4
    assert s["calls"]["answered"] == 2
    assert s["calls"]["no_answer"] == 2
    assert s["calls"]["answer_rate"] == 50
    assert s["calls"]["completed"] == 2

    assert s["emails"]["sent"] == 2
    assert s["emails"]["skipped"] == 1
    assert s["emails"]["replies_tracked"] is False

    assert s["outcomes"]["converted"] == 1
    assert s["outcomes"]["not_interested"] == 1
    assert s["conversion"]["rate"] == 50  # 1 of 2 classified calls

    assert s["companies_contacted"] == 5  # unique set: A B C D E
    assert len(s["recent_outcomes"]) == 2
    assert s["recent_outcomes"][0]["company"] in ("A", "B")


def test_dry_run_counts_as_contacted_but_not_answered():
    events = [
        _ev("email_outreach", status="drafted", company="A"),
        _ev("call_placed", status="drafted", company="B"),
    ]
    s = compute_stats(events)
    assert s["funnel"]["contacted"] == 2
    assert s["calls"]["prepared"] == 1
    assert s["calls"]["dialed"] == 0
    assert s["calls"]["no_answer"] == 0


def test_no_answer_never_negative():
    # An answered inbound/demo call without a matching placed event must not
    # produce a negative "did not pick up".
    s = compute_stats([_ev("call_started", call_sid="x", company="A")])
    assert s["calls"]["no_answer"] == 0
