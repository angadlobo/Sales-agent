"""Self-hosted voice call server.

Run alongside Asterisk:

    python -m sales_agent.outreach.selfhosted.server --config config.yaml

It does three jobs in one process:
  1. HTTP control API (default :9091) — the campaign pipeline POSTs /call here
     to start an outbound call.
  2. Originates the call through Asterisk's ARI REST API; Asterisk dials out
     via your SIP trunk.
  3. AudioSocket server (default :9092) — when the callee answers, Asterisk
     streams the call audio here; we run the listen → Claude → speak loop
     with local Whisper STT and Piper/espeak TTS.

      pipeline ──HTTP──▶ this server ──ARI──▶ Asterisk ──SIP trunk──▶ phone
                              ▲                   │
                              └── AudioSocket ────┘  (raw call audio)

Known limitation: no barge-in — the agent finishes speaking before it listens.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import os
import threading
import urllib.request
import uuid as uuidlib
from typing import Dict

from flask import Flask, jsonify, request

from ... import activity
from ...config import Settings
from ...models import Lead
from .. import voice_outreach
from . import audiosocket as asock
from . import speech

logger = logging.getLogger(__name__)

# UUID -> Lead, registered when a call is originated, consumed when
# Asterisk connects the matching AudioSocket stream.
_PENDING_CALLS: Dict[str, Lead] = {}

MAX_AGENT_TURNS = 12
GOODBYE_TOKENS = ("goodbye", "have a great day", "take care")


# ── Asterisk ARI (originate the outbound call) ──────────────────────────────

def ari_originate(call_uuid: str, phone: str, audio_host: str, audio_port: int) -> None:
    """Ask Asterisk to dial `phone` and connect the answered call to us."""
    ari_url = os.getenv("ARI_URL", "http://127.0.0.1:8088/ari").rstrip("/")
    user = os.getenv("ARI_USERNAME", "sales-agent")
    password = os.getenv("ARI_PASSWORD", "")
    endpoint_tpl = os.getenv("ARI_ENDPOINT", "PJSIP/{to}@trunk")
    context = os.getenv("ARI_CONTEXT", "sales-agent")

    body = json.dumps(
        {
            "endpoint": endpoint_tpl.format(to=phone),
            "extension": "agent",
            "context": context,
            "priority": 1,
            "variables": {
                "AS_UUID": call_uuid,
                "AS_SERVER": f"{audio_host}:{audio_port}",
            },
        }
    ).encode()
    auth = base64.b64encode(f"{user}:{password}".encode()).decode()
    req = urllib.request.Request(
        f"{ari_url}/channels",
        data=body,
        headers={"Authorization": f"Basic {auth}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        resp.read()
    logger.info("Originated call %s to %s", call_uuid, phone)


# ── Control API (what the campaign pipeline talks to) ───────────────────────

def make_control_app(settings: Settings, audio_host: str, audio_port: int) -> Flask:
    app = Flask(__name__)

    @app.get("/health")
    def health():
        return jsonify({"ok": True, "pending_calls": len(_PENDING_CALLS)})

    @app.post("/call")
    def start_call():
        payload = request.get_json(force=True)
        phone = payload.get("phone")
        if not phone:
            return jsonify({"error": "phone is required"}), 400
        call_uuid = str(uuidlib.uuid4())
        _PENDING_CALLS[call_uuid] = Lead(
            company_name=payload.get("company") or "your business",
            contact_name=payload.get("contact_name"),
            phone=phone,
        )
        try:
            ari_originate(call_uuid, phone, audio_host, audio_port)
        except Exception as exc:  # noqa: BLE001 — report dial failures to the caller
            _PENDING_CALLS.pop(call_uuid, None)
            logger.exception("ARI originate failed")
            return jsonify({"error": f"ARI originate failed: {exc}"}), 502
        return jsonify({"call_id": call_uuid})

    return app


# ── AudioSocket conversation loop ───────────────────────────────────────────

async def _send_audio(writer: asyncio.StreamWriter, pcm: bytes) -> None:
    """Stream PCM to Asterisk in real-time-paced 20 ms frames."""
    step = asock.BYTES_PER_FRAME
    for i in range(0, len(pcm), step):
        chunk = pcm[i : i + step]
        if len(chunk) < step:
            chunk = chunk + b"\x00" * (step - len(chunk))
        writer.write(asock.pack_frame(asock.KIND_AUDIO, chunk))
        await writer.drain()
        await asyncio.sleep(asock.FRAME_MS / 1000)


async def handle_call(
    settings: Settings, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    loop = asyncio.get_running_loop()
    try:
        kind, payload = await asock.read_frame(reader)
        if kind != asock.KIND_UUID:
            logger.warning("First frame was not UUID (kind=%#x); dropping", kind)
            return
        call_uuid = str(uuidlib.UUID(bytes=payload)) if len(payload) == 16 else payload.hex()
        lead = _PENDING_CALLS.pop(call_uuid, Lead(company_name="your business"))
        logger.info("Call %s connected (%s)", call_uuid, lead.company_name)

        opener = await loop.run_in_executor(
            None, voice_outreach.opening_line, settings, lead
        )
        history = [{"role": "assistant", "content": opener}]
        activity.log(
            "call_started", call_sid=call_uuid, company=lead.company_name, opener=opener
        )
        await _send_audio(writer, await loop.run_in_executor(None, speech.synthesize, opener))

        detector = asock.UtteranceDetector()
        agent_turns = 1
        end_reason = "max turns reached"

        while agent_turns < MAX_AGENT_TURNS:
            kind, payload = await asock.read_frame(reader)
            if kind in (asock.KIND_TERMINATE, asock.KIND_ERROR):
                logger.info("Call %s ended by far side", call_uuid)
                end_reason = "caller hung up"
                break
            if kind != asock.KIND_AUDIO:
                continue

            utterance = detector.feed(payload)
            if utterance is None:
                continue

            text = await loop.run_in_executor(None, speech.transcribe, utterance)
            if not text:
                continue
            logger.info("Call %s heard: %r", call_uuid, text)
            history.append({"role": "user", "content": text})

            reply = await loop.run_in_executor(
                None, voice_outreach.next_reply, settings, lead, history
            )
            history.append({"role": "assistant", "content": reply})
            agent_turns += 1
            activity.log(
                "call_turn",
                call_sid=call_uuid,
                company=lead.company_name,
                user_said=text,
                agent_said=reply,
            )

            await _send_audio(
                writer, await loop.run_in_executor(None, speech.synthesize, reply)
            )
            # The detector may have absorbed echo/noise while we spoke.
            detector.reset()

            if any(tok in reply.lower() for tok in GOODBYE_TOKENS):
                end_reason = "agent said goodbye"
                break

        activity.log(
            "call_ended",
            call_sid=call_uuid,
            company=lead.company_name,
            reason=end_reason,
            transcript=history,
        )
        try:
            outcome = await loop.run_in_executor(
                None, voice_outreach.classify_outcome, settings, lead, history
            )
            activity.log(
                "call_outcome",
                call_sid=call_uuid,
                company=lead.company_name,
                outcome=outcome.outcome,
                summary=outcome.summary,
            )
        except Exception:  # noqa: BLE001 - classification is best-effort
            logger.exception("Outcome classification failed for call %s", call_uuid)
        writer.write(asock.pack_frame(asock.KIND_TERMINATE))
        await writer.drain()
    except (asyncio.IncompleteReadError, ConnectionResetError):
        logger.info("Call stream closed")
    finally:
        writer.close()


async def run_audio_server(settings: Settings, host: str, port: int) -> None:
    server = await asyncio.start_server(
        lambda r, w: handle_call(settings, r, w), host, port
    )
    logger.info("AudioSocket server listening on %s:%d", host, port)
    async with server:
        await server.serve_forever()


# ── Entry point ──────────────────────────────────────────────────────────────

def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="Self-hosted AI voice call server")
    parser.add_argument("--config", required=True, help="Path to config.yaml")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--control-port", type=int, default=9091)
    parser.add_argument("--audio-port", type=int, default=9092)
    args = parser.parse_args()

    settings = Settings.from_yaml(args.config)

    control = make_control_app(settings, args.host, args.audio_port)
    threading.Thread(
        target=lambda: control.run(host=args.host, port=args.control_port),
        daemon=True,
    ).start()
    logger.info("Control API listening on %s:%d", args.host, args.control_port)

    asyncio.run(run_audio_server(settings, args.host, args.audio_port))


if __name__ == "__main__":
    main()
