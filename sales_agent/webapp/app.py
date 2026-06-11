"""Flask backend for the web UI.

The UI is a step-by-step flow:
  1. Describe the product.
  2. Pick targeting (location + a searchable business-type dropdown).
  3. Discover + score leads (no outreach yet).
  4. Select individual or multiple leads and email / call them, or export.

Provider credentials (SMTP for email, Twilio/SignalWire for voice) can be
configured from the UI; they are written to the local .env file so they
persist across restarts. Truly self-hosted telephony (Asterisk/FreeSWITCH)
still has to be set up outside this tool.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import set_key
from flask import Flask, Response, jsonify, render_template, request

from .. import discovery, enrichment, inbox, scoring, stats, storage
from ..business_types import BUSINESS_TYPE_GROUPS, BUSINESS_TYPES
from ..compliance import Suppression
from ..config import CampaignConfig, EmailConfig, Settings, VoiceConfig
from ..models import Channel, Lead, Product, QualifiedLead, Targeting
from ..outreach import email_outreach, voice_outreach

logger = logging.getLogger(__name__)

# Path to the .env file we read/write provider credentials from.
ENV_PATH = Path(__file__).resolve().parents[2] / ".env"

# In-memory job store: job_id -> {status, results, error, qualified, payload, ...}
# Fine for a single-user local tool; use a real queue for multi-user hosting.
_JOBS: Dict[str, Dict[str, Any]] = {}

# Append-only activity log so every search, email and call is on record.
# Shared with the CLI pipeline and the phone-call servers via sales_agent.activity.
from .. import activity as _activity

ACTIVITY_PATH = _activity.ACTIVITY_PATH


def _log_activity(event: str, **detail: Any) -> None:
    """Append one event to data/activity.jsonl. Best-effort, never raises."""
    _activity.log(event, **detail)


def _read_activity(limit: int = 200) -> List[dict]:
    """Most recent activity entries, newest first."""
    return _activity.read(limit)


# ──────────────────────────────────────────────────────────────────────────
# Settings construction
# ──────────────────────────────────────────────────────────────────────────
def _industry_from_payload(payload: dict) -> Optional[str]:
    """Collapse the multi-select industry picker into one targeting string.

    Accepts `industries` (list from the picker, may mix curated and custom
    entries) and falls back to the legacy single `industry` field. "Any" /
    "All industries" anywhere in the selection means no industry filter.
    """
    raw = payload.get("industries")
    if raw is None:
        raw = [payload.get("industry") or ""]
    if isinstance(raw, str):
        raw = [raw]
    picked = [str(v).strip() for v in raw if str(v).strip()]
    if not picked or any(p.lower() in {"any", "all", "any industry", "all industries"} for p in picked):
        return None
    return ", ".join(dict.fromkeys(picked))  # de-dupe, keep order


def _settings_from_payload(payload: dict) -> Settings:
    product = Product(
        name=payload.get("product_name", "").strip() or "My Product",
        description=payload.get("product_description", "").strip(),
        offering_type=payload.get("offering_type") or None,
        price_point=payload.get("price_point") or None,
        ideal_customer=payload.get("ideal_customer") or None,
        value_props=[v.strip() for v in payload.get("value_props", "").splitlines() if v.strip()],
    )
    targeting = Targeting(
        location=payload.get("location") or None,
        industry=_industry_from_payload(payload),
        max_leads=int(payload.get("max_leads") or 10),
    )
    campaign = CampaignConfig(
        min_score_to_contact=int(payload.get("min_score") or 65),
        channels=payload.get("channels") or ["email"],
    )
    settings = Settings(product=product, targeting=targeting, campaign=campaign)
    settings.dry_run = not bool(payload.get("send"))
    return settings


# ──────────────────────────────────────────────────────────────────────────
# Discovery job (discover -> enrich -> score; NO outreach)
# ──────────────────────────────────────────────────────────────────────────
def _run_discovery_job(job_id: str, settings: Settings, payload: dict) -> None:
    job = _JOBS[job_id]
    try:
        job["status"] = "running"
        job["stage"] = "discovering"
        _log_activity(
            "search_started",
            job_id=job_id,
            product=settings.product.name,
            offering_type=settings.product.offering_type or "product/service",
            industries=settings.targeting.industry or "any industry",
            location=settings.targeting.location or "anywhere",
            max_leads=settings.targeting.max_leads,
        )
        leads = discovery.discover_leads(settings)

        qualified: List[QualifiedLead] = []
        total = len(leads) or 1
        for i, lead in enumerate(leads):
            job["stage"] = f"enriching & scoring {i + 1}/{total}"
            lead = enrichment.enrich_lead(lead, settings)
            score = scoring.score_lead(lead, settings)
            qualified.append(QualifiedLead(lead=lead, score=score))

        qualified.sort(key=lambda q: q.best_score, reverse=True)
        try:
            storage.save_run(qualified, settings.ensure_data_dir())
        except Exception:  # noqa: BLE001 - saving is best-effort
            logger.exception("Failed to save run for job %s", job_id)

        job["qualified"] = qualified
        job["results"] = [ql.model_dump(mode="json") for ql in qualified]
        job["stage"] = "done"
        job["status"] = "done"
        _log_activity(
            "search_completed",
            job_id=job_id,
            leads_found=len(qualified),
            top_companies=[q.lead.company_name for q in qualified[:5]],
        )
    except Exception as exc:  # noqa: BLE001 - surface anything to the UI
        logger.exception("Discovery job %s failed", job_id)
        job["status"] = "error"
        job["error"] = str(exc)
        _log_activity("search_failed", job_id=job_id, error=str(exc))


# ──────────────────────────────────────────────────────────────────────────
# Outreach job (email / call a chosen subset of already-discovered leads)
# ──────────────────────────────────────────────────────────────────────────
def _run_outreach_job(
    job_id: str,
    source_job_id: str,
    indexes: List[int],
    channel: str,
    send: bool,
) -> None:
    job = _JOBS[job_id]
    try:
        job["status"] = "running"
        source = _JOBS.get(source_job_id)
        if not source or "qualified" not in source:
            raise ValueError("source campaign not found — run discovery first")

        qualified: List[QualifiedLead] = source["qualified"]
        payload = dict(source.get("payload") or {})
        payload["channels"] = [channel]
        payload["send"] = send
        settings = _settings_from_payload(payload)
        # Shared do-not-contact list; the inbox connector appends opt-outs here.
        suppression = Suppression(Path("data") / "suppression.txt")

        total = len(indexes) or 1
        for n, idx in enumerate(indexes):
            if idx < 0 or idx >= len(qualified):
                continue
            ql = qualified[idx]
            job["stage"] = f"{'sending' if send else 'drafting'} {n + 1}/{total}"
            if channel == Channel.EMAIL.value:
                result = email_outreach.send_email(ql.lead, ql.score, settings, suppression)
            elif channel == Channel.VOICE.value:
                result = voice_outreach.place_call(ql.lead, ql.score, settings, suppression)
            else:
                continue
            # Replace any prior outreach on this channel so re-running is idempotent.
            ql.outreach = [o for o in ql.outreach if o.channel.value != channel]
            ql.outreach.append(result)
            _log_activity(
                "call_placed" if channel == Channel.VOICE.value else "email_outreach",
                job_id=job_id,
                company=ql.lead.company_name,
                contact=ql.lead.contact_name,
                to=ql.lead.phone if channel == Channel.VOICE.value else ql.lead.email,
                status=result.status.value,
                live=send,
                subject=result.subject,
                body=result.body,
                message_id=result.message_id,
                detail=result.detail,
            )

        # Re-save the run so outreach results are on disk too, not just in memory.
        try:
            storage.save_run(qualified, settings.ensure_data_dir())
        except Exception:  # noqa: BLE001 - saving is best-effort
            logger.exception("Failed to save outreach run for job %s", job_id)

        # Return the full (updated) lead list so the UI can re-render cleanly.
        source["results"] = [q.model_dump(mode="json") for q in qualified]
        job["results"] = source["results"]
        job["dry_run"] = settings.dry_run
        job["status"] = "done"
    except Exception as exc:  # noqa: BLE001
        logger.exception("Outreach job %s failed", job_id)
        job["status"] = "error"
        job["error"] = str(exc)


# ──────────────────────────────────────────────────────────────────────────
# Config (provider credentials) read / write
# ──────────────────────────────────────────────────────────────────────────
def _config_view() -> dict:
    """A redacted snapshot of current provider config for the settings panel."""
    email = EmailConfig()
    voice = VoiceConfig()

    def mask(value: Optional[str]) -> str:
        if not value:
            return ""
        if len(value) <= 4:
            return "••••"
        return value[:2] + "••••" + value[-2:]

    return {
        "anthropic_key_set": bool(os.getenv("ANTHROPIC_API_KEY")),
        "email": {
            "host": email.host or "",
            "port": email.port,
            "username": email.username or "",
            "password_set": bool(email.password),
            "from_name": email.from_name or "",
            "from_email": email.from_email or "",
            "is_configured": email.is_configured,
        },
        "voice": {
            "provider": voice.provider,
            "account_sid": mask(voice.account_sid),
            "auth_token_set": bool(voice.auth_token),
            "from_number": voice.from_number or "",
            "signalwire_space": voice.signalwire_space or "",
            "webhook_base_url": voice.webhook_base_url or "",
            "is_configured": voice.is_configured,
        },
    }


def _apply_config(payload: dict) -> None:
    """Write provided (non-empty) values to os.environ and the .env file.

    A blank value means "leave as-is" so the UI can omit secrets it only shows
    masked. Each new Settings()/EmailConfig()/VoiceConfig() reads os.environ,
    so updating it takes effect on the next discovery/outreach run.
    """
    # field name in payload -> env var
    mapping = {
        "anthropic_key": "ANTHROPIC_API_KEY",
        "smtp_host": "SMTP_HOST",
        "smtp_port": "SMTP_PORT",
        "smtp_username": "SMTP_USERNAME",
        "smtp_password": "SMTP_PASSWORD",
        "smtp_from_name": "SMTP_FROM_NAME",
        "smtp_from_email": "SMTP_FROM_EMAIL",
        "voice_provider": "VOICE_PROVIDER",
        "voice_account_sid": "VOICE_ACCOUNT_SID",
        "voice_auth_token": "VOICE_AUTH_TOKEN",
        "voice_from_number": "VOICE_FROM_NUMBER",
        "signalwire_space_url": "SIGNALWIRE_SPACE_URL",
        "voice_webhook_base_url": "VOICE_WEBHOOK_BASE_URL",
    }

    ENV_PATH.touch(exist_ok=True)
    for field, env_name in mapping.items():
        if field not in payload:
            continue
        value = str(payload[field]).strip()
        if value == "":
            continue
        os.environ[env_name] = value
        try:
            set_key(str(ENV_PATH), env_name, value)
        except Exception:  # noqa: BLE001 - persistence is best-effort
            logger.exception("Failed to persist %s to .env", env_name)


# ──────────────────────────────────────────────────────────────────────────
# CSV / JSON export
# ──────────────────────────────────────────────────────────────────────────
def _leads_csv(qualified: List[QualifiedLead]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        ["company", "contact", "title", "email", "phone", "website",
         "location", "industry", "score", "reasoning", "outreach"]
    )
    for ql in qualified:
        channels = "; ".join(f"{o.channel.value}:{o.status.value}" for o in ql.outreach)
        l = ql.lead
        writer.writerow([
            l.company_name, l.contact_name or "", l.contact_title or "",
            l.email or "", l.phone or "", l.website or "",
            l.location or "", l.industry or "",
            ql.best_score, ql.score.reasoning if ql.score else "", channels,
        ])
    return buf.getvalue()


def create_app() -> Flask:
    app = Flask(__name__)
    # Keep curated ordering (e.g. business-type groups) instead of alphabetizing.
    app.json.sort_keys = False

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/talk")
    def talk():
        return render_template("talk.html")

    @app.get("/dashboard")
    def dashboard():
        return render_template("dashboard.html")

    @app.get("/api/stats")
    def api_stats():
        return jsonify(stats.compute_stats(_activity.read(limit=100_000)))

    @app.post("/api/inbox/sync")
    def inbox_sync():
        result = inbox.sync_inbox()
        status = 400 if "error" in result else 200
        return jsonify(result), status

    @app.get("/api/business-types")
    def business_types():
        return jsonify({"types": BUSINESS_TYPES, "groups": BUSINESS_TYPE_GROUPS})

    @app.get("/api/history")
    def history():
        return jsonify({"events": _read_activity()})

    # ── Config ──────────────────────────────────────────────────────────
    @app.get("/api/config")
    def get_config():
        return jsonify(_config_view())

    @app.post("/api/config")
    def post_config():
        payload = request.get_json(force=True) or {}
        _apply_config(payload)
        return jsonify(_config_view())

    # ── Step 1-3: discover + score ─────────────────────────────────────
    @app.post("/api/discover")
    def start_discovery():
        payload = request.get_json(force=True)
        if not payload.get("product_description"):
            return jsonify({"error": "product_description is required"}), 400
        settings = _settings_from_payload(payload)
        job_id = uuid.uuid4().hex[:12]
        _JOBS[job_id] = {
            "status": "queued", "stage": "queued",
            "results": None, "error": None, "payload": payload,
        }
        threading.Thread(
            target=_run_discovery_job, args=(job_id, settings, payload), daemon=True
        ).start()
        return jsonify({"job_id": job_id})

    @app.get("/api/jobs/<job_id>")
    def job_status(job_id: str):
        job = _JOBS.get(job_id)
        if not job:
            return jsonify({"error": "unknown job"}), 404
        # Don't ship the internal QualifiedLead objects to the client.
        return jsonify({k: v for k, v in job.items() if k != "qualified"})

    # ── Step 4: outreach to a chosen subset ────────────────────────────
    @app.post("/api/outreach")
    def start_outreach():
        payload = request.get_json(force=True) or {}
        source_job_id = payload.get("job_id")
        indexes = payload.get("indexes") or []
        channel = payload.get("channel") or "email"
        send = bool(payload.get("send"))
        if not source_job_id or source_job_id not in _JOBS:
            return jsonify({"error": "unknown campaign — run discovery first"}), 400
        if not indexes:
            return jsonify({"error": "select at least one lead"}), 400

        job_id = uuid.uuid4().hex[:12]
        _JOBS[job_id] = {"status": "queued", "stage": "queued", "results": None, "error": None}
        threading.Thread(
            target=_run_outreach_job,
            args=(job_id, source_job_id, [int(i) for i in indexes], channel, send),
            daemon=True,
        ).start()
        return jsonify({"job_id": job_id, "dry_run": not send})

    # ── Export ─────────────────────────────────────────────────────────
    @app.get("/api/export/<job_id>")
    def export(job_id: str):
        job = _JOBS.get(job_id)
        if not job or "qualified" not in job:
            return jsonify({"error": "unknown campaign"}), 404
        qualified: List[QualifiedLead] = job["qualified"]

        idx_param = request.args.get("indexes")
        if idx_param:
            wanted = {int(x) for x in idx_param.split(",") if x.strip().isdigit()}
            qualified = [ql for i, ql in enumerate(qualified) if i in wanted]

        fmt = request.args.get("format", "csv").lower()
        if fmt == "json":
            body = json.dumps([ql.model_dump(mode="json") for ql in qualified], indent=2)
            return Response(
                body, mimetype="application/json",
                headers={"Content-Disposition": f"attachment; filename=leads-{job_id}.json"},
            )
        body = _leads_csv(qualified)
        return Response(
            body, mimetype="text/csv",
            headers={"Content-Disposition": f"attachment; filename=leads-{job_id}.csv"},
        )

    # ── Free browser-voice demo turn ───────────────────────────────────
    @app.post("/api/talk")
    def talk_reply():
        payload = request.get_json(force=True)
        settings = Settings(
            product=Product(
                name=payload.get("product_name") or "our product",
                description=payload.get("product_description") or "",
            ),
            targeting=Targeting(),
            campaign=CampaignConfig(),
        )
        lead = Lead(company_name=payload.get("company") or "your business")
        history: List[Dict[str, str]] = [
            {"role": m["role"], "content": m["content"]}
            for m in payload.get("history", [])
            if m.get("role") in ("user", "assistant") and m.get("content")
        ]
        if not history:
            reply = voice_outreach.opening_line(settings, lead)
        else:
            reply = voice_outreach.next_reply(settings, lead, history)
        _log_activity(
            "voice_demo_turn",
            company=lead.company_name,
            user_said=history[-1]["content"] if history else None,
            agent_said=reply,
        )
        return jsonify({"reply": reply})

    return app


def _inbox_poller(interval: int) -> None:
    """Background loop: pull replies every `interval` seconds when configured."""
    import time

    while True:
        time.sleep(interval)
        try:
            cfg = inbox.InboxConfig()
            if cfg.is_configured:
                result = inbox.sync_inbox(cfg)
                if result.get("new_replies"):
                    logger.info("Inbox poll: %d new repl(ies)", result["new_replies"])
        except Exception:  # noqa: BLE001 - the poller must never die
            logger.exception("Inbox poll failed")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="Sales agent web UI")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--inbox-poll",
        type=int,
        default=int(os.getenv("INBOX_POLL_SECONDS", "300")),
        help="Seconds between automatic reply checks (0 disables)",
    )
    args = parser.parse_args()
    app = create_app()
    if args.inbox_poll > 0 and inbox.InboxConfig().is_configured:
        threading.Thread(target=_inbox_poller, args=(args.inbox_poll,), daemon=True).start()
        logger.info("Inbox poller running every %ds", args.inbox_poll)
    print(f"\n  Sales agent UI:  http://{args.host}:{args.port}\n")
    app.run(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
