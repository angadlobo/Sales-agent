# Sales Agent

An AI agent that finds sales leads on the internet and reaches out to them. You
describe the product or service you sell and who you want to reach; the agent
searches the web for matching businesses, scores how likely each is to buy,
finds their published contact details, and then drafts and sends a personalized
email — or places an AI voice call that actually talks to the prospect.

It is powered by **Claude** (`claude-opus-4-8`) and ships in **dry-run mode by
default**, so you can see exactly what it would do before anything goes out.

> ⚠️ **Read [Legal & ethical use](#legal--ethical-use) before contacting anyone.**
> Cold outreach is regulated. This tool gives you mechanical guardrails, but
> staying lawful is your responsibility.

---

## What it does

```
        ┌───────────┐   ┌──────────┐   ┌─────────┐   ┌──────────────────┐
  you   │  DISCOVER │ → │  ENRICH  │ → │  SCORE  │ → │     OUTREACH     │
 give → │ web search│   │  find    │   │ 0-100   │   │  email / AI call │
product │ for fits  │   │  email   │   │ fit     │   │  (dry-run safe)  │
        └───────────┘   └──────────┘   └─────────┘   └──────────────────┘
```

1. **Discover** — Claude uses web search to find real businesses matching your
   target location and industry.
2. **Enrich** — looks up each company's *publicly published* contact details
   (it never guesses or pattern-matches email addresses).
3. **Score** — rates each lead 0–100 on likelihood to buy, with reasoning,
   buying signals, and risks.
4. **Outreach** — for leads above your score threshold:
   - **Email**: drafts a short, specific, human-sounding email and sends it via SMTP.
   - **Voice**: places a Twilio call where Claude holds a live qualifying conversation.

Every run is saved to `data/` as JSON and CSV.

---

## Quick start

```bash
# 1. Install
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. Add your Claude API key
cp .env.example .env          # then edit .env and set ANTHROPIC_API_KEY

# 3. Launch the web UI
python -m sales_agent.webapp
# open http://localhost:8000
```

The web UI is the easiest way in:

- **Dashboard (`/`)** — describe your product and target, hit *Run campaign*,
  and watch scored leads and drafted emails appear. Dry-run unless you tick
  "actually send".
- **Talk to the agent (`/talk`)** — a **100% free voice demo**: your browser
  does the listening and speaking (Web Speech API, Chrome/Edge), Claude does
  the thinking. Same conversation brain as real phone calls, zero telephony
  cost — perfect for hearing and tuning your pitch.

Prefer the terminal? There's a demo script and a CLI too:

```bash
python examples/run_demo.py   # dry run — finds & scores real leads, sends nothing
```

```bash
# One-off, no config file
python -m sales_agent.cli run \
  --product "Acme CRM" \
  --description "Simple CRM for small law firms" \
  --location "Denver" --industry "law firms" --max-leads 10

# From a config file
cp config.yaml.example config.yaml   # edit it
python -m sales_agent.cli run --config config.yaml

# Just discover & score, no outreach
python -m sales_agent.cli discover --config config.yaml
```

The CLI runs **dry-run by default**. Add `--send` to actually send emails / place
calls (and see the safety section first).

---

## Configuration

Two places, merged together (env wins for secrets, config/CLI for campaign):

- **`.env`** — API keys and credentials (copy from `.env.example`).
- **`config.yaml`** — what you sell, who to target, campaign rules (copy from
  `config.yaml.example`).

Key campaign settings:

| Setting | Meaning |
|---|---|
| `min_score_to_contact` | Only leads scoring ≥ this (0–100) get contacted |
| `channels` | `["email"]`, `["voice"]`, or both |
| `daily_send_limit` | Hard cap on outreach attempts per run |
| `DRY_RUN` (env) | `true` = draft only, send nothing |

---

## Email outreach

Set the `SMTP_*` variables in `.env` (any SMTP server — Gmail with an app
password works). Drafts get a CAN-SPAM-style footer with an opt-out line
appended automatically. Run with `--send` to actually send.

## AI voice calls

### Free option (no telephony at all)

`python -m sales_agent.webapp` → open **/talk**. Your browser handles speech
recognition and speech synthesis (free, built into Chrome/Edge); Claude runs
the conversation. This is the same brain used on real calls, so it's the
cheapest way to develop and test your pitch.

### Real phone calls — provider options

There is no truly free way to dial real phone numbers (carriers charge per
minute), but these get you close:

| Provider | Cost to start | Notes |
|---|---|---|
| **Self-hosted** (`VOICE_PROVIDER=selfhosted`) | Software $0; SIP trunk ~$1/mo + ~$0.01/min | Asterisk + local Whisper STT + Piper TTS, all open source — **[full guide](docs/SELF_HOSTED_CALLS.md)**. Cheapest per-minute by far; no public webhook needed |
| [Twilio](https://www.twilio.com) (`VOICE_PROVIDER=twilio`) | Free trial credit | Easiest cloud option; trial calls play a notice & only dial verified numbers |
| [SignalWire](https://signalwire.com) (`VOICE_PROVIDER=signalwire`) | Free trial credit | Same API shape as Twilio; cheaper per-minute |
| [Telnyx](https://telnyx.com) / [Plivo](https://www.plivo.com) / [Vonage](https://www.vonage.com) | Trial credit | Similar cloud offerings; would need a small dialer tweak |

All three named providers work out of the box — pick with `VOICE_PROVIDER` in
`.env`. The cloud ones are called via plain REST (no SDK); the self-hosted one
runs entirely on your machine except the trunk: see
**[docs/SELF_HOSTED_CALLS.md](docs/SELF_HOSTED_CALLS.md)** for the ~15-minute
setup (Asterisk configs are templated in `deploy/asterisk/`).

```bash
# 1. Set VOICE_PROVIDER + VOICE_* (and SIGNALWIRE_SPACE_URL if signalwire) in .env
# 2. Start the conversation webhook server
python -m sales_agent.outreach.voice_server --config config.yaml
# 3. Expose it publicly (dev): ngrok http 5000
#    Put the https URL in VOICE_WEBHOOK_BASE_URL
# 4. Run a campaign with the voice channel
python -m sales_agent.cli run --config config.yaml --channels voice --send
```

On each call Claude speaks an opener, listens via the provider's
speech-to-text, and replies turn by turn until the conversation ends. It
identifies itself as an automated assistant if asked, and ends politely on
any opt-out.

### What's free, summarized

| Piece | Cost |
|---|---|
| Web UI + dashboard | Free (Flask, no build step) |
| Browser-voice demo | Free (browser's own speech engine) |
| Email outreach | Free with Gmail SMTP + app password (within Gmail's daily limits) |
| Lead discovery / scoring | Claude API usage (pay per token; web search included) |
| Real phone calls | Trial credit on Twilio/SignalWire, then per-minute — or self-host: $0 software, ~$0.01/min trunk only |

---

## Legal & ethical use

This tool can contact real people, which is regulated. Before using `--send`:

- **Get authorization.** Only run real outreach for a business you're permitted
  to do outreach for.
- **Email (CAN-SPAM / GDPR / PECR):** include a real physical address, honor
  opt-outs promptly, don't use deceptive subject lines. In the EU/UK, B2B cold
  email may require a lawful basis — check before sending.
- **Calls (TCPA / do-not-call):** AI/automated calls are heavily restricted in
  many jurisdictions and often require prior consent. Scrub against do-not-call
  registries. Many places require disclosure that the call is automated.
- **No fabricated data.** The agent is prompted never to invent emails or phone
  numbers and only to use contact info a business publishes for inbound contact.

Built-in guardrails: dry-run default, a suppression / do-not-contact list
(`--suppression path/to/list.txt`), per-run send caps, email/phone validation,
and an auto-appended opt-out footer. These help you comply — they don't make
you compliant on their own.

---

## Project layout

```
sales_agent/
  config.py            # env + YAML settings
  models.py            # Pydantic data models (also LLM schemas)
  llm.py               # Claude wrapper: web search + structured output
  discovery.py         # find candidate companies
  enrichment.py        # find published contact details
  scoring.py           # 0-100 likelihood-to-buy score
  compliance.py        # suppression list, validation, opt-out footer
  storage.py           # JSON/CSV output
  pipeline.py          # orchestrates the whole campaign
  cli.py               # command-line interface
  outreach/
    email_outreach.py  # draft + SMTP send
    voice_outreach.py  # dialer (Twilio/SignalWire/selfhosted) + conversation brain
    voice_server.py    # Flask webhook for Twilio/SignalWire calls
    selfhosted/        # free stack: Asterisk AudioSocket + Whisper + Piper
  webapp/
    app.py             # web UI backend (dashboard + free browser-voice API)
    templates/         # index.html (dashboard), talk.html (voice demo)
examples/run_demo.py
tests/
```

## Running tests

```bash
pip install pytest
pytest                 # the compliance tests need no API key or network
```

---

## How it uses Claude

- **Discovery & enrichment** use Claude's server-side web search tool — no
  separate search API key needed.
- **Scoring, email drafting, and the live call** use structured outputs and
  short, grounded prompts so results are typed and predictable.
- Models are configurable via `SALES_AGENT_MODEL` (reasoning, default
  `claude-opus-4-8`) and `SALES_AGENT_FAST_MODEL` (cheap classification,
  default `claude-haiku-4-5`).
