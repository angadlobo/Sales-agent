# Self-hosted AI phone calls (no Twilio)

Run AI sales calls on your own machine with **only open-source software**.
The single unavoidable cost is the SIP trunk — the bridge into the phone
network. Carriers charge for that no matter what you do; everything else here
is free.

## The honest cost picture

| | Twilio | Self-hosted (this guide) |
|---|---|---|
| Phone number | ~$1.15/mo | ~$0.85–1/mo (trunk provider) |
| Outbound minutes | ~$0.014/min | ~$0.009–0.01/min (trunk only) |
| Speech-to-text | ~$0.02–0.08/min extra | **$0 — local Whisper** |
| Text-to-speech | metered extra | **$0 — local Piper/espeak** |
| Call software | their cloud | **$0 — Asterisk on your box** |

For a 2-minute call, that's roughly **$0.02 self-hosted vs $0.10–0.20 on
Twilio** — and at zero call volume you pay only ~$1/mo for the number.

> Want literally $0? Use the browser-voice demo (`python -m sales_agent.webapp`
> → `/talk`). It's the same conversation brain with no phone network involved —
> the prospect just clicks a link instead of receiving a call.

## Architecture

```
pipeline ──HTTP──▶ voice server (:9091) ──ARI──▶ Asterisk ──SIP trunk──▶ ☎ phone
                        ▲                            │
                        └────── AudioSocket (:9092) ─┘   raw call audio
        Whisper (hear) · Claude (think) · Piper/espeak (speak)
```

- **Asterisk** (free PBX) dials out through your SIP trunk and, when the
  callee answers, streams raw call audio to our server over its simple
  AudioSocket TCP protocol.
- **The voice server** (`sales_agent/outreach/selfhosted/server.py`) detects
  when the person stops talking, transcribes with **faster-whisper**, asks
  **Claude** for the next line, synthesizes it with **Piper** (or espeak-ng),
  and streams the audio back. Same conversation brain as every other channel.

## Setup

### 1. Install Asterisk and the speech stack

```bash
# Debian/Ubuntu
sudo apt install asterisk espeak-ng
pip install -r requirements-selfhosted.txt
```

(Optional, nicer voice: `pip install piper-tts`, download a voice model from
the Piper releases, and set `PIPER_VOICE=/path/to/en_US-voice.onnx`.)

### 2. Get a SIP trunk

Sign up with any SIP trunk provider — [voip.ms](https://voip.ms),
[Telnyx](https://telnyx.com), [Flowroute](https://flowroute.com)… You get SIP
credentials and (optionally) a phone number for caller ID. This is the one
paid piece (~$1/mo + ~$0.01/min).

### 3. Configure Asterisk

Copy the templates and fill in your trunk credentials:

```bash
sudo cp deploy/asterisk/{extensions,pjsip,ari,http}.conf /etc/asterisk/
sudo $EDITOR /etc/asterisk/pjsip.conf   # YOUR_TRUNK_USERNAME etc.
sudo $EDITOR /etc/asterisk/ari.conf     # set a real ARI password
sudo systemctl restart asterisk
```

### 4. Configure the agent

In `.env`:

```bash
VOICE_PROVIDER=selfhosted
SELFHOSTED_VOICE_URL=http://127.0.0.1:9091

# Read by the voice server:
ARI_URL=http://127.0.0.1:8088/ari
ARI_USERNAME=sales-agent
ARI_PASSWORD=<the password from ari.conf>
ARI_ENDPOINT=PJSIP/{to}@trunk
WHISPER_MODEL=base            # tiny = faster, small/medium = more accurate
# PIPER_VOICE=/path/to/en_US-lessac-medium.onnx
```

### 5. Run it

```bash
# Terminal 1 — the voice server (loads Whisper on first call)
python -m sales_agent.outreach.selfhosted.server --config config.yaml

# Terminal 2 — a campaign using the voice channel
python -m sales_agent.cli run --config config.yaml --channels voice --send
```

Everything stays on localhost — no public webhook URL, no ngrok, unlike the
Twilio/SignalWire path.

## Tuning & limitations

- **Latency.** Each turn costs STT + Claude + TTS time (~2–4 s on a laptop
  CPU with `WHISPER_MODEL=base`). Use `tiny` for snappier calls, or run on a
  GPU box for near-instant transcription.
- **No barge-in.** The agent finishes speaking before it listens. Callers who
  talk over it won't be heard until it stops.
- **End-of-speech detection** is energy-based. If it cuts people off or waits
  too long, tune `UtteranceDetector(threshold=, silence_ms=)` in
  `sales_agent/outreach/selfhosted/audiosocket.py`.
- **Legal.** Self-hosting doesn't change the law: automated outbound calls are
  heavily regulated (TCPA, do-not-call registries, mandatory disclosure in
  many places). Read the README's "Legal & ethical use" section. The dry-run
  default and suppression list apply to this channel too.
