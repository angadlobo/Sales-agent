"""Configuration: merges environment variables and an optional config.yaml."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import yaml
from dotenv import load_dotenv

from .models import Product, Targeting

load_dotenv()  # pull .env into os.environ if present

# Defaults follow the claude-api skill guidance: Opus for reasoning,
# Haiku for cheap, high-volume classification.
DEFAULT_MODEL = os.getenv("SALES_AGENT_MODEL", "claude-opus-4-8")
DEFAULT_FAST_MODEL = os.getenv("SALES_AGENT_FAST_MODEL", "claude-haiku-4-5")


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class EmailConfig:
    host: Optional[str] = field(default_factory=lambda: os.getenv("SMTP_HOST"))
    port: int = field(default_factory=lambda: int(os.getenv("SMTP_PORT", "587")))
    username: Optional[str] = field(default_factory=lambda: os.getenv("SMTP_USERNAME"))
    password: Optional[str] = field(default_factory=lambda: os.getenv("SMTP_PASSWORD"))
    from_name: Optional[str] = field(default_factory=lambda: os.getenv("SMTP_FROM_NAME"))
    from_email: Optional[str] = field(default_factory=lambda: os.getenv("SMTP_FROM_EMAIL"))

    @property
    def is_configured(self) -> bool:
        return all([self.host, self.username, self.password, self.from_email])


@dataclass
class VoiceConfig:
    account_sid: Optional[str] = field(default_factory=lambda: os.getenv("TWILIO_ACCOUNT_SID"))
    auth_token: Optional[str] = field(default_factory=lambda: os.getenv("TWILIO_AUTH_TOKEN"))
    from_number: Optional[str] = field(default_factory=lambda: os.getenv("TWILIO_FROM_NUMBER"))
    webhook_base_url: Optional[str] = field(
        default_factory=lambda: os.getenv("VOICE_WEBHOOK_BASE_URL")
    )

    @property
    def is_configured(self) -> bool:
        return all([self.account_sid, self.auth_token, self.from_number, self.webhook_base_url])


@dataclass
class CampaignConfig:
    min_score_to_contact: int = 65
    channels: List[str] = field(default_factory=lambda: ["email"])
    daily_send_limit: int = 50


@dataclass
class Settings:
    product: Product
    targeting: Targeting
    campaign: CampaignConfig
    model: str = DEFAULT_MODEL
    fast_model: str = DEFAULT_FAST_MODEL
    dry_run: bool = field(default_factory=lambda: _env_bool("DRY_RUN", True))
    email: EmailConfig = field(default_factory=EmailConfig)
    voice: VoiceConfig = field(default_factory=VoiceConfig)
    data_dir: Path = field(default_factory=lambda: Path("data"))

    @classmethod
    def from_yaml(cls, path: str | Path) -> "Settings":
        raw = yaml.safe_load(Path(path).read_text()) or {}
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: dict) -> "Settings":
        product = Product(**raw["product"])
        targeting = Targeting(**raw.get("targeting", {}))
        campaign = CampaignConfig(**raw.get("campaign", {}))
        return cls(product=product, targeting=targeting, campaign=campaign)

    def ensure_data_dir(self) -> Path:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        return self.data_dir
