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
    # "product", "service", or "product & service" — helps frame the pitch.
    offering_type: Optional[str] = None
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
    # Buyer-intelligence fields (publicly observable only — never invented).
    company_size: Optional[str] = None        # e.g. "~12 employees", "small family business"
    revenue_range: Optional[str] = None       # rough public estimate, if reported anywhere
    opportunity_summary: Optional[str] = None # why there's money on the table here
    # Observed buying-intent signals, each tagged strong/medium/weak,
    # e.g. "STRONG: hiring two dispatchers (job posting, May 2026)".
    intent_signals: List[str] = Field(default_factory=list)


class ScoreBreakdown(BaseModel):
    """Weighted sub-scores behind the headline lead score (each 0-100)."""

    need: int = Field(ge=0, le=100, description="How badly they need this (25% weight)")
    buying_intent: int = Field(ge=0, le=100, description="Active intent signals (25%)")
    budget: int = Field(ge=0, le=100, description="Ability to afford it (15%)")
    urgency: int = Field(ge=0, le=100, description="Likely to act soon (15%)")
    accessibility: int = Field(ge=0, le=100, description="Reachable decision-maker (10%)")
    location_fit: int = Field(ge=0, le=100, description="Geographic fit (5%)")
    competitive_advantage: int = Field(
        ge=0, le=100, description="Our edge over their alternatives (5%)"
    )


class ConversionPrediction(BaseModel):
    """Forward-looking estimates for one lead. Honest guesses, not promises."""

    reply_probability: int = Field(ge=0, le=100, description="% chance they reply at all")
    meeting_probability: int = Field(ge=0, le=100, description="% chance of a meeting")
    conversion_probability: int = Field(ge=0, le=100, description="% chance they buy")
    estimated_deal_value: Optional[str] = None   # e.g. "$1,200/yr (12 techs x $99/mo)"
    estimated_sales_cycle: Optional[str] = None  # e.g. "2-6 weeks"
    confidence: str = Field(
        default="low", description="low | medium | high — how solid the evidence is"
    )


class LeadScore(BaseModel):
    """Claude's qualification of how likely a lead is to buy."""

    score: int = Field(ge=0, le=100, description="0-100 likelihood-to-buy / fit score")
    reasoning: str = Field(description="Why this score, grounded in the lead and the product")
    buying_signals: List[str] = Field(default_factory=list)
    risks: List[str] = Field(default_factory=list)
    # Optional richer analysis (filled by the weighted-rubric scorer).
    breakdown: Optional[ScoreBreakdown] = None
    prediction: Optional[ConversionPrediction] = None
    recommended_channel: Optional[str] = None  # "email" | "voice" + why, in prose


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
    # RFC 5322 Message-ID of a sent email — lets the inbox connector match replies.
    message_id: Optional[str] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class QualifiedLead(BaseModel):
    """A lead plus everything the pipeline learned about it."""

    lead: Lead
    score: Optional[LeadScore] = None
    outreach: List[OutreachResult] = Field(default_factory=list)

    @property
    def best_score(self) -> int:
        return self.score.score if self.score else 0
