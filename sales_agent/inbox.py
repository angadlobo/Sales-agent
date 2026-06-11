"""Inbox connector — tracks replies to outreach emails.

Works with Gmail out of the box (IMAP + the same app password you use for
SMTP) and with any other provider that offers IMAP — the host is derived
from your SMTP host automatically (smtp.gmail.com -> imap.gmail.com), or set
IMAP_HOST explicitly.

What a sync does:
  1. Reads the activity log to learn which emails we've sent (address +
     Message-ID of each).
  2. Pulls recent inbox messages over IMAP (read-only — nothing is marked
     seen or moved).
  3. Matches replies to our sends — first by In-Reply-To / References
     headers, then by sender address.
  4. Classifies each reply with Claude (interested / question / follow_up /
     not_interested / opt_out / bounce) and logs it to the activity log,
     where the dashboard and History panel pick it up.
  5. Opt-outs are appended to the shared suppression list automatically so
     those people are never contacted again.
"""

from __future__ import annotations

import email
import email.utils
import imaplib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Literal, Optional, Set, Tuple

from pydantic import BaseModel, Field

from . import activity, llm
from .compliance import Suppression
from .config import DEFAULT_FAST_MODEL

logger = logging.getLogger(__name__)

STATE_PATH = Path("data") / "inbox_seen.json"
SUPPRESSION_PATH = Path("data") / "suppression.txt"
MAX_MESSAGES_PER_SYNC = 200
BODY_SNIPPET_CHARS = 2000

# Common SMTP -> IMAP host mappings; anything else tries s/smtp/imap/.
_IMAP_FOR_SMTP = {
    "smtp.gmail.com": "imap.gmail.com",
    "smtp.office365.com": "outlook.office365.com",
    "smtp-mail.outlook.com": "outlook.office365.com",
    "smtp.mail.yahoo.com": "imap.mail.yahoo.com",
    "smtp.zoho.com": "imap.zoho.com",
    "smtp.fastmail.com": "imap.fastmail.com",
}


def derive_imap_host(smtp_host: Optional[str]) -> Optional[str]:
    if not smtp_host:
        return None
    host = smtp_host.strip().lower()
    if host in _IMAP_FOR_SMTP:
        return _IMAP_FOR_SMTP[host]
    if host.startswith("smtp."):
        return "imap." + host[len("smtp.") :]
    return host  # some providers use one host for both


@dataclass
class InboxConfig:
    """IMAP credentials. Falls back to the SMTP account (true for Gmail)."""

    host: Optional[str] = field(
        default_factory=lambda: os.getenv("IMAP_HOST") or derive_imap_host(os.getenv("SMTP_HOST"))
    )
    port: int = field(default_factory=lambda: int(os.getenv("IMAP_PORT", "993")))
    username: Optional[str] = field(
        default_factory=lambda: os.getenv("IMAP_USERNAME") or os.getenv("SMTP_USERNAME")
    )
    password: Optional[str] = field(
        default_factory=lambda: os.getenv("IMAP_PASSWORD") or os.getenv("SMTP_PASSWORD")
    )

    @property
    def is_configured(self) -> bool:
        return all([self.host, self.username, self.password])


class ReplyClassification(BaseModel):
    """How a prospect's reply reads — feeds the dashboard."""

    classification: Literal[
        "interested",      # positive: wants a call, demo, pricing, more info
        "question",        # asked something, neither yes nor no
        "follow_up",       # "contact me later / next quarter"
        "not_interested",  # clear no
        "opt_out",         # unsubscribe / stop / remove me
        "other",           # auto-reply, out-of-office, unrelated
    ]
    summary: str = Field(description="One sentence: what the reply says")


# ──────────────────────────────────────────────────────────────────────────
# Pure helpers (unit-testable without IMAP)
# ──────────────────────────────────────────────────────────────────────────

def build_sent_index(events: List[dict]) -> Dict[str, dict]:
    """address -> {company, subject, time, message_ids} for every sent email."""
    index: Dict[str, dict] = {}
    for e in events:
        if e.get("event") != "email_outreach" or e.get("status") != "sent":
            continue
        addr = (e.get("to") or "").strip().lower()
        if not addr:
            continue
        entry = index.setdefault(
            addr,
            {"company": e.get("company"), "subject": e.get("subject"),
             "time": e.get("time"), "message_ids": set()},
        )
        if e.get("message_id"):
            entry["message_ids"].add(e["message_id"].strip())
        entry["time"] = e.get("time") or entry["time"]
    return index


def match_reply(
    from_addr: str, references: str, sent_index: Dict[str, dict]
) -> Optional[Tuple[str, dict]]:
    """Match an inbound message to one of our sends.

    `references` is the concatenated In-Reply-To + References header text.
    Returns (matched_address, index_entry) or None.
    """
    ref_ids = set(re.findall(r"<[^<>]+>", references or ""))
    if ref_ids:
        for addr, entry in sent_index.items():
            if entry["message_ids"] & ref_ids:
                return addr, entry
    addr = (from_addr or "").strip().lower()
    if addr in sent_index:
        return addr, sent_index[addr]
    return None


def is_bounce(from_addr: str, subject: str) -> bool:
    sender = (from_addr or "").lower()
    subj = (subject or "").lower()
    return (
        "mailer-daemon" in sender
        or "postmaster" in sender
        or "delivery status" in subj
        or "undeliverable" in subj
        or "delivery has failed" in subj
    )


def extract_text_body(msg: email.message.Message) -> str:
    """Best-effort plain-text body of a parsed email."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and not part.get("Content-Disposition"):
                try:
                    return part.get_payload(decode=True).decode(
                        part.get_content_charset() or "utf-8", errors="replace"
                    )
                except Exception:  # noqa: BLE001
                    continue
        return ""
    try:
        payload = msg.get_payload(decode=True)
        return payload.decode(msg.get_content_charset() or "utf-8", errors="replace") if payload else ""
    except Exception:  # noqa: BLE001
        return ""


# ──────────────────────────────────────────────────────────────────────────
# State (which inbox messages we've already processed)
# ──────────────────────────────────────────────────────────────────────────

def _load_seen() -> Set[str]:
    if STATE_PATH.exists():
        try:
            return set(json.loads(STATE_PATH.read_text()).get("seen", []))
        except Exception:  # noqa: BLE001
            logger.exception("Could not read inbox state; starting fresh")
    return set()


def _save_seen(seen: Set[str]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Keep the file bounded — old ids fall outside the search window anyway.
    STATE_PATH.write_text(json.dumps({"seen": sorted(seen)[-5000:]}))


# ──────────────────────────────────────────────────────────────────────────
# Sync
# ──────────────────────────────────────────────────────────────────────────

def sync_inbox(
    config: Optional[InboxConfig] = None,
    *,
    model: str = DEFAULT_FAST_MODEL,
    lookback_days: int = 30,
) -> dict:
    """One sync pass. Returns {checked, new_replies, replies:[...]} or {error}."""
    cfg = config or InboxConfig()
    if not cfg.is_configured:
        return {
            "error": "Inbox not configured. For Gmail just set SMTP_* (same app "
            "password works for IMAP); otherwise set IMAP_HOST/IMAP_USERNAME/IMAP_PASSWORD."
        }

    events = activity.read(limit=100_000)
    sent_index = build_sent_index(events)
    if not sent_index:
        activity.log("inbox_sync", checked=0, new_replies=0, note="no sent emails yet")
        return {"checked": 0, "new_replies": 0, "replies": []}

    seen = _load_seen()
    suppression = Suppression(SUPPRESSION_PATH)
    since = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).strftime("%d-%b-%Y")

    new_replies: List[dict] = []
    checked = 0
    try:
        with imaplib.IMAP4_SSL(cfg.host, cfg.port) as imap:
            imap.login(cfg.username, cfg.password)
            imap.select("INBOX", readonly=True)
            status, data = imap.search(None, f'(SINCE "{since}")')
            if status != "OK":
                return {"error": f"IMAP search failed: {status}"}
            ids = data[0].split()[-MAX_MESSAGES_PER_SYNC:]

            for num in ids:
                status, parts = imap.fetch(num, "(BODY.PEEK[])")
                if status != "OK" or not parts or parts[0] is None:
                    continue
                raw = parts[0][1]
                msg = email.message_from_bytes(raw)
                msg_id = (msg.get("Message-ID") or f"no-id-{num.decode()}").strip()
                if msg_id in seen:
                    continue
                seen.add(msg_id)
                checked += 1

                from_name, from_addr = email.utils.parseaddr(msg.get("From", ""))
                refs = f"{msg.get('In-Reply-To', '')} {msg.get('References', '')}"
                matched = match_reply(from_addr, refs, sent_index)
                if not matched:
                    continue
                addr, entry = matched

                subject = msg.get("Subject", "")
                body = extract_text_body(msg)[:BODY_SNIPPET_CHARS].strip()
                reply = _classify(from_addr, subject, body, entry, model)

                if reply.classification == "opt_out":
                    suppression.add(addr)

                record = {
                    "from": from_addr,
                    "from_name": from_name or None,
                    "company": entry.get("company"),
                    "subject": subject,
                    "body": body,
                    "classification": reply.classification,
                    "summary": reply.summary,
                    "suppressed": reply.classification == "opt_out",
                }
                activity.log("email_reply", **record)
                new_replies.append(record)
    except Exception as exc:  # noqa: BLE001 — surface IMAP/auth issues cleanly
        logger.exception("Inbox sync failed")
        return {"error": f"Inbox sync failed: {exc}"}
    finally:
        _save_seen(seen)

    activity.log("inbox_sync", checked=checked, new_replies=len(new_replies))

    if new_replies:
        # New outcomes arrived — let the self-improvement loop decide whether
        # it has enough fresh signal to re-derive targeting insights.
        from . import learning

        learning.maybe_refresh_insights()

    return {"checked": checked, "new_replies": len(new_replies), "replies": new_replies}


def _classify(
    from_addr: str, subject: str, body: str, entry: dict, model: str
) -> ReplyClassification:
    if is_bounce(from_addr, subject):
        return ReplyClassification(
            classification="other", summary="Bounce / delivery failure notification."
        )
    try:
        return llm.extract(
            "Classify this reply to a cold sales email. Judge only from the text; "
            "an out-of-office or automated reply is 'other'. 'opt_out' is anything "
            "asking not to be contacted (unsubscribe, stop, remove me).\n\n"
            f"We originally wrote to {entry.get('company') or from_addr} with the "
            f"subject {entry.get('subject')!r}.\n\n"
            f"THEIR REPLY (subject: {subject!r}):\n{body or '(empty body)'}",
            ReplyClassification,
            model=model,
        )
    except Exception:  # noqa: BLE001 — classification is best-effort
        logger.exception("Reply classification failed; recording as unclassified")
        return ReplyClassification(
            classification="other", summary="(could not classify automatically)"
        )
