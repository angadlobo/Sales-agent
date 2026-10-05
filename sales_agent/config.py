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

# ── AI provider ─────────────────────────────────────────────────────────────
# The default brain is Anthropic Claude. Any OpenAI-compatible /chat/completions
# endpoint also works: GitHub Models, OpenRouter, OpenAI, or a custom base URL
# (Groq, Together, Ollama, …). Select with LLM_PROVIDER; see .env.example.

# provider -> (reasoning model, cheap/fast model). Anthropic defaults follow
# the claude-api skill guidance: Opus for reasoning, Haiku for cheap
# classification.
_MODEL_PRESETS = {
    "anthropic": ("claude-opus-4-8", "claude-haiku-4-5"),
    # gpt-5 is listed in the GitHub Models catalog but rejected on free
    # personal accounts ("unavailable_model"); gpt-4.1 works everywhere.
    "github": ("openai/gpt-4.1", "openai/gpt-4.1-mini"),
    "openrouter": ("openai/gpt-5", "openai/gpt-5-mini"),
    "openai": ("gpt-5", "gpt-5-mini"),
}

_PROVIDER_BASE_URLS = {
    "github": "https://models.github.ai/inference",
    "openrouter": "https://openrouter.ai/api/v1",
    "openai": "https://api.openai.com/v1",
}

# Besides the generic LLM_API_KEY, each provider has a conventional env var.
_PROVIDER_KEY_ENVS = {
    "github": ("LLM_API_KEY", "GITHUB_TOKEN"),
    "openrouter": ("LLM_API_KEY", "OPENROUTER_API_KEY"),
    "openai": ("LLM_API_KEY", "OPENAI_API_KEY"),
}


def llm_provider() -> str:
    return os.getenv("LLM_PROVIDER", "anthropic").strip().lower()


def llm_base_url() -> Optional[str]:
    return os.getenv("LLM_BASE_URL") or _PROVIDER_BASE_URLS.get(llm_provider())


def llm_api_key() -> Optional[str]:
    for name in _PROVIDER_KEY_ENVS.get(llm_provider(), ("LLM_API_KEY",)):
        value = os.getenv(name)
        if value:
            return value
    return None


def preset_models() -> tuple[str, str]:
    return _MODEL_PRESETS.get(llm_provider(), _MODEL_PRESETS["anthropic"])


def default_model() -> str:
    """Provider-aware default, resolved at call time so the settings panel
    can switch providers without a restart."""
    return os.getenv("SALES_AGENT_MODEL") or preset_models()[0]


def default_fast_model() -> str:
    return os.getenv("SALES_AGENT_FAST_MODEL") or preset_models()[1]


# Import-time snapshots, kept for callers that use them as function defaults.
DEFAULT_MODEL = default_model()
DEFAULT_FAST_MODEL = default_fast_model()


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


def _env_first(*names: str) -> Optional[str]:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return None


@dataclass
class VoiceConfig:
    """Telephony provider for AI phone calls.

    Supported providers:
      - twilio      https://api.twilio.com (2010-04-01 REST API)
      - signalwire  https://<your-space>.signalwire.com (same API shape;
                    set SIGNALWIRE_SPACE_URL)
      - selfhosted  your own Asterisk + Whisper + Piper stack — free software,
                    only a SIP trunk costs money. See docs/SELF_HOSTED_CALLS.md.

    No phone provider is needed for the free browser-voice mode in the web UI.
    """

    provider: str = field(default_factory=lambda: os.getenv("VOICE_PROVIDER", "twilio").lower())
    account_sid: Optional[str] = field(
        default_factory=lambda: _env_first("VOICE_ACCOUNT_SID", "TWILIO_ACCOUNT_SID")
    )
    auth_token: Optional[str] = field(
        default_factory=lambda: _env_first("VOICE_AUTH_TOKEN", "TWILIO_AUTH_TOKEN")
    )
    from_number: Optional[str] = field(
        default_factory=lambda: _env_first("VOICE_FROM_NUMBER", "TWILIO_FROM_NUMBER")
    )
    signalwire_space: Optional[str] = field(
        default_factory=lambda: os.getenv("SIGNALWIRE_SPACE_URL")
    )
    webhook_base_url: Optional[str] = field(
        default_factory=lambda: os.getenv("VOICE_WEBHOOK_BASE_URL")
    )
    selfhosted_url: str = field(
        default_factory=lambda: os.getenv("SELFHOSTED_VOICE_URL", "http://127.0.0.1:9091")
    )

    @property
    def api_base(self) -> Optional[str]:
        if self.provider == "twilio":
            return "https://api.twilio.com/2010-04-01"
        if self.provider == "signalwire":
            if not self.signalwire_space:
                return None
            space = self.signalwire_space.replace("https://", "").rstrip("/")
            return f"https://{space}/api/laml/2010-04-01"
        return None

    @property
    def is_configured(self) -> bool:
        if self.provider == "selfhosted":
            # The self-hosted server holds the ARI/trunk credentials itself.
            return bool(self.selfhosted_url)
        return all(
            [self.account_sid, self.auth_token, self.from_number, self.webhook_base_url, self.api_base]
        )


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
    model: str = field(default_factory=default_model)
    fast_model: str = field(default_factory=default_fast_model)
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
