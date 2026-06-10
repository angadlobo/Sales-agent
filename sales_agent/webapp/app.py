"""Flask backend for the web UI."""

from __future__ import annotations

import argparse
import logging
import threading
import uuid
from typing import Any, Dict, List

from flask import Flask, jsonify, render_template, request

from .. import pipeline
from ..config import CampaignConfig, Settings
from ..models import Lead, Product, Targeting
from ..outreach import voice_outreach

logger = logging.getLogger(__name__)

# In-memory job store: job_id -> {status, results, error, log}
# Fine for a single-user local tool; use a real queue for multi-user hosting.
_JOBS: Dict[str, Dict[str, Any]] = {}


def _settings_from_payload(payload: dict) -> Settings:
    product = Product(
        name=payload.get("product_name", "").strip() or "My Product",
        description=payload.get("product_description", "").strip(),
        price_point=payload.get("price_point") or None,
        ideal_customer=payload.get("ideal_customer") or None,
        value_props=[v.strip() for v in payload.get("value_props", "").splitlines() if v.strip()],
    )
    targeting = Targeting(
        location=payload.get("location") or None,
        industry=payload.get("industry") or None,
        max_leads=int(payload.get("max_leads") or 10),
    )
    campaign = CampaignConfig(
        min_score_to_contact=int(payload.get("min_score") or 65),
        channels=payload.get("channels") or ["email"],
    )
    settings = Settings(product=product, targeting=targeting, campaign=campaign)
    # The UI is dry-run unless the user explicitly ticks "actually send".
    settings.dry_run = not bool(payload.get("send"))
    return settings


def _run_job(job_id: str, settings: Settings) -> None:
    job = _JOBS[job_id]
    try:
        job["status"] = "running"
        results = pipeline.run_campaign(settings)
        job["results"] = [ql.model_dump(mode="json") for ql in results]
        job["status"] = "done"
    except Exception as exc:  # noqa: BLE001 — surface anything to the UI
        logger.exception("Campaign job %s failed", job_id)
        job["status"] = "error"
        job["error"] = str(exc)


def create_app() -> Flask:
    app = Flask(__name__)

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/talk")
    def talk():
        return render_template("talk.html")

    @app.post("/api/campaign")
    def start_campaign():
        payload = request.get_json(force=True)
        if not payload.get("product_description"):
            return jsonify({"error": "product_description is required"}), 400
        settings = _settings_from_payload(payload)
        job_id = uuid.uuid4().hex[:12]
        _JOBS[job_id] = {"status": "queued", "results": None, "error": None}
        threading.Thread(target=_run_job, args=(job_id, settings), daemon=True).start()
        return jsonify({"job_id": job_id, "dry_run": settings.dry_run})

    @app.get("/api/jobs/<job_id>")
    def job_status(job_id: str):
        job = _JOBS.get(job_id)
        if not job:
            return jsonify({"error": "unknown job"}), 404
        return jsonify(job)

    @app.post("/api/talk")
    def talk_reply():
        """One turn of the voice conversation, for the browser-voice page.

        Body: {product_name, product_description, company, history:[{role,content}]}
        Returns: {reply: "..."}
        """
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
        return jsonify({"reply": reply})

    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="Sales agent web UI")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    app = create_app()
    print(f"\n  Sales agent UI:  http://{args.host}:{args.port}\n")
    app.run(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
