"""Tests for the inbox connector's pure logic (no IMAP / network needed)."""

import email

from sales_agent.inbox import (
    build_sent_index,
    derive_imap_host,
    extract_text_body,
    is_bounce,
    match_reply,
)


def _sent(to, message_id=None, company="Acme"):
    return {
        "event": "email_outreach",
        "status": "sent",
        "to": to,
        "company": company,
        "subject": "quick question",
        "time": "2026-06-11T00:00:00+00:00",
        "message_id": message_id,
    }


def test_derive_imap_host_gmail_and_generic():
    assert derive_imap_host("smtp.gmail.com") == "imap.gmail.com"
    assert derive_imap_host("smtp.office365.com") == "outlook.office365.com"
    assert derive_imap_host("smtp.example.com") == "imap.example.com"
    assert derive_imap_host("mail.example.com") == "mail.example.com"
    assert derive_imap_host(None) is None


def test_build_sent_index_only_counts_sent():
    events = [
        _sent("owner@acme.com", "<id1@us>"),
        {"event": "email_outreach", "status": "drafted", "to": "x@y.com"},
        {"event": "call_placed", "status": "sent", "to": "+15551234567"},
    ]
    idx = build_sent_index(events)
    assert list(idx) == ["owner@acme.com"]
    assert idx["owner@acme.com"]["message_ids"] == {"<id1@us>"}


def test_match_reply_prefers_message_id_over_address():
    idx = build_sent_index(
        [_sent("owner@acme.com", "<id1@us>"), _sent("boss@other.com", "<id2@us>", "Other")]
    )
    # Reply arrives from a different address (e.g. assistant) but threads to id2.
    matched = match_reply("assistant@other.com", "<id2@us>", idx)
    assert matched is not None and matched[0] == "boss@other.com"


def test_match_reply_falls_back_to_from_address():
    idx = build_sent_index([_sent("owner@acme.com")])
    matched = match_reply("Owner@Acme.com", "", idx)
    assert matched is not None and matched[0] == "owner@acme.com"
    assert match_reply("stranger@nowhere.com", "", idx) is None


def test_is_bounce():
    assert is_bounce("MAILER-DAEMON@google.com", "anything")
    assert is_bounce("postmaster@x.com", "")
    assert is_bounce("a@b.com", "Undeliverable: quick question")
    assert not is_bounce("owner@acme.com", "Re: quick question")


def test_extract_text_body_plain_and_multipart():
    plain = email.message_from_string("Subject: hi\n\nJust the body.")
    assert extract_text_body(plain).strip() == "Just the body."

    multi = email.message_from_string(
        "Subject: hi\nMIME-Version: 1.0\n"
        'Content-Type: multipart/alternative; boundary="B"\n\n'
        "--B\nContent-Type: text/plain\n\nplain part\n"
        "--B\nContent-Type: text/html\n\n<p>html part</p>\n--B--\n"
    )
    assert extract_text_body(multi).strip() == "plain part"


def test_stats_count_replies_and_engagement():
    from sales_agent.stats import compute_stats

    events = [
        {"event": "email_outreach", "status": "sent", "to": "a@x.com", "company": "A",
         "time": "t"},
        {"event": "email_outreach", "status": "sent", "to": "b@y.com", "company": "B",
         "time": "t"},
        {"event": "inbox_sync", "checked": 5, "new_replies": 1, "time": "t"},
        {"event": "email_reply", "from": "a@x.com", "company": "A",
         "classification": "interested", "summary": "Wants a demo", "time": "t"},
    ]
    s = compute_stats(events)
    assert s["emails"]["replies_tracked"] is True
    assert s["emails"]["replies"] == 1
    assert s["emails"]["no_response"] == 1
    assert s["emails"]["reply_rate"] == 50
    assert s["emails"]["reply_breakdown"]["interested"] == 1
    assert s["funnel"]["engaged"] == 1  # the reply counts as engagement
    assert s["recent_replies"][0]["summary"] == "Wants a demo"
