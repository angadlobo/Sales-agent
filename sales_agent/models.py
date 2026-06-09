"""Typed data structures shared across the pipeline.

Pydantic models double as the schemas Claude fills in via structured outputs,
so the same definition validates LLM output and our own code.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field


class Product(BaseModel):
    """The product or service the user wants to sell."""

    name: str
    description: str
    price_point: Optional[str] = None
    ideal_customer: Optional[str] = None
    value_props: List[str] = Field(default_factory=list)


class Targeting(BaseModel):
    """Where and who to look for."""

    location: Optional[str] = None
    industry: Optional[str] = None
    max_leads: int = 15


class Lead(BaseModel):
    """A potential customer discovered on the web."""

    company_name: str
    website: Optional[str] = None
    contact_name: Optional[str] = None
    contact_title: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    location: Optional[str] = None
    industry: Optional[str] = None
    # One-line reason this company looked relevant during discovery.
    rationale: Optional[str] = None
    source_url: Optional[str] = None


class LeadScore(BaseModel):
    """Claude's qualification of how likely a lead is to buy."""

    score: int = Field(ge=0, le=100, description="0-100 likelihood-to-buy / fit score")
    reasoning: str = Field(description="Why this score, grounded in the lead and the product")
    buying_signals: List[str] = Field(default_factory=list)
    risks: List[str] = Field(default_factory=list)


class Channel(str, Enum):
    EMAIL = "email"
    VOICE = "voice"


class OutreachStatus(str, Enum):
    PENDING = "pending"
    DRAFTED = "drafted"
    SENT = "sent"
    SKIPPED = "skipped"
    FAILED = "failed"


class OutreachResult(BaseModel):
    """Outcome of trying to reach one lead on one channel."""

    channel: Channel
    status: OutreachStatus
    detail: str = ""
    # For email: the drafted subject/body. For voice: the call SID / opening line.
    subject: Optional[str] = None
    body: Optional[str] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class QualifiedLead(BaseModel):
    """A lead plus everything the pipeline learned about it."""

    lead: Lead
    score: Optional[LeadScore] = None
    outreach: List[OutreachResult] = Field(default_factory=list)

    @property
    def best_score(self) -> int:
        return self.score.score if self.score else 0
