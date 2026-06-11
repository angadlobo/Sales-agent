"""Flask webhook that runs the live phone conversation with Twilio.

Run it separately from the campaign:

    python -m sales_agent.outreach.voice_server --config config.yaml

Then expose it publicly (e.g. `ngrok http 5000`) and put that URL in
VOICE_WEBHOOK_BASE_URL before placing calls.

Twilio flow per call:
    POST /voice/answer   -> agent speaks its opener, then <Gather> listens
    POST /voice/gather   -> we get the prospect's speech, ask Claude, speak back,
                            then <Gather> again — looping until the call ends.

Conversation state is kept in memory keyed by Twilio's CallSid. For production
you'd move this to Redis so it survives restarts and scales across workers.
"""

from __future__ import annotations

import argparse
import logging
from typing import Dict, List

from flask import Flask, Response, request

from .. import activity
from ..config import Settings
from ..models import Lead
from . import voice_outreach

logger = logging.getLogger(__name__)

# CallSid -> conversation history ([{role, content}, ...])
_CONVERSATIONS: Dict[str, List[Dict[str, str]]] = {}
# CallSid -> Lead being called
_LEADS: Dict[str, Lead] = {}

MAX_TURNS = 12  # hard stop so a call can't loop forever


def _twiml(xml_body: str) -> Response:
    return Response(
        f'<?xml version="1.0" encoding="UTF-8"?><Response>{xml_body}</Response>',
        mimetype="text/xml",
    )


def _gather(prompt_to_say: str) -> str:
    """A <Say> followed by a speech <Gather> that posts to /voice/gather."""
    safe = _escape(prompt_to_say)
    return (
        f'<Gather input="speech" action="/voice/gather" method="POST" '
        f'speechTimeout="auto" language="en-US">'
        f"<Say>{safe}</Say>"
        f"</Gather>"
        # If the caller says nothing, Twilio falls through to here:
        f'<Say>Sorry, I didn\'t catch that. I\'ll let you go — have a great day.</Say><Hangup/>'
    )


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _end_call(call_sid: str, lead: Lead, reason: str) -> None:
    """Persist the full transcript when a call wraps up."""
    history = _CONVERSATIONS.get(call_sid, [])
    activity.log(
        "call_ended",
        call_sid=call_sid,
        company=lead.company_name,
        reason=reason,
        transcript=history,
    )


def create_app(settings: Settings) -> Flask:
    app = Flask(__name__)

    @app.route("/voice/answer", methods=["POST", "GET"])
    def answer() -> Response:
        call_sid = request.values.get("CallSid", "unknown")
        company = request.values.get("company", "your business")
        lead = Lead(company_name=company)
        _LEADS[call_sid] = lead

        opener = voice_outreach.opening_line(settings, lead)
        _CONVERSATIONS[call_sid] = [{"role": "assistant", "content": opener}]
        logger.info("Call %s connected to %s", call_sid, company)
        activity.log("call_started", call_sid=call_sid, company=company, opener=opener)
        return _twiml(_gather(opener))

    @app.route("/voice/gather", methods=["POST"])
    def gather() -> Response:
        call_sid = request.values.get("CallSid", "unknown")
        speech = (request.values.get("SpeechResult") or "").strip()
        history = _CONVERSATIONS.setdefault(call_sid, [])
        lead = _LEADS.get(call_sid, Lead(company_name="your business"))

        if speech:
            history.append({"role": "user", "content": speech})
        logger.info("Call %s heard: %r", call_sid, speech)

        if len([m for m in history if m["role"] == "assistant"]) >= MAX_TURNS:
            _end_call(call_sid, lead, "max turns reached")
            return _twiml("<Say>Thanks for your time. Goodbye!</Say><Hangup/>")

        reply = voice_outreach.next_reply(settings, lead, history)
        history.append({"role": "assistant", "content": reply})
        activity.log(
            "call_turn",
            call_sid=call_sid,
            company=lead.company_name,
            user_said=speech or None,
            agent_said=reply,
        )

        # Let Claude end the call by signalling in its reply.
        if any(tok in reply.lower() for tok in ("goodbye", "have a great day", "take care")):
            _end_call(call_sid, lead, "agent said goodbye")
            return _twiml(f"<Say>{_escape(reply)}</Say><Hangup/>")

        return _twiml(_gather(reply))

    @app.route("/health")
    def health() -> Response:
        return Response("ok", mimetype="text/plain")

    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description="AI voice conversation webhook server")
    parser.add_argument("--config", required=True, help="Path to config.yaml")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=5000)
    args = parser.parse_args()

    settings = Settings.from_yaml(args.config)
    app = create_app(settings)
    app.run(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
