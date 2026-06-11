"""Guardrails that sit between the agent and the outside world.

These are deliberately conservative. Cold outreach is regulated (CAN-SPAM,
GDPR/PECR, TCPA, and local do-not-call rules). This module enforces the
mechanical parts; you remain responsible for using the tool lawfully.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Set

logger = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# E.164-ish: optional +, then 7-15 digits.
_PHONE_RE = re.compile(r"^\+?[1-9]\d{6,14}$")


class ComplianceError(Exception):
    """Raised when an outreach attempt violates a guardrail."""


class Suppression:
    """A do-not-contact list backed by a plain text file (one entry per line)."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else None
        self._entries: Set[str] = set()
        if self.path and self.path.exists():
            for line in self.path.read_text().splitlines():
                entry = line.strip().lower()
                if entry and not entry.startswith("#"):
                    self._entries.add(entry)
        logger.info("Suppression list loaded: %d entries", len(self._entries))

    def contains(self, value: str | None) -> bool:
        return bool(value) and value.strip().lower() in self._entries

    def add(self, value: str) -> None:
        v = value.strip().lower()
        self._entries.add(v)
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a") as fh:
                fh.write(v + "\n")


def normalize_phone(phone: str) -> str:
    return re.sub(r"[^\d+]", "", phone)


def check_email(email: str | None, suppression: Suppression) -> None:
    if not email:
        raise ComplianceError("no email address")
    if not _EMAIL_RE.match(email):
        raise ComplianceError(f"invalid email address: {email!r}")
    if suppression.contains(email):
        raise ComplianceError(f"email is on the suppression list: {email}")


def check_phone(phone: str | None, suppression: Suppression) -> None:
    if not phone:
        raise ComplianceError("no phone number")
    norm = normalize_phone(phone)
    if not _PHONE_RE.match(norm):
        raise ComplianceError(f"invalid phone number: {phone!r}")
    if suppression.contains(norm):
        raise ComplianceError(f"phone is on the suppression list: {phone}")


# Plain-language footer that helps satisfy CAN-SPAM-style requirements.
# You still must use a real physical address and honor opt-outs.
def email_footer(from_name: str, physical_address: str = "") -> str:
    addr = f"\n{physical_address}" if physical_address else ""
    return (
        f"\n\n—\n{from_name}{addr}\n"
        "You received this because your business is publicly listed. "
        "Reply STOP or \"unsubscribe\" and we won't contact you again."
    )
