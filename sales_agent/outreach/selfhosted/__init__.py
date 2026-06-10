"""Self-hosted AI phone calls — no Twilio, no SignalWire.

Everything runs on your own machine for free, except the SIP trunk that
connects to the phone network (the one part nobody can make free — carriers
charge per minute, typically ~$0.01/min + ~$1/mo for a number).

Stack (all open source):
  Asterisk      free PBX — handles the SIP call and streams raw audio to us
  Whisper       free local speech-to-text (faster-whisper)
  Piper/espeak  free local text-to-speech
  Claude        the conversation brain (same one used by every other channel)

See docs/SELF_HOSTED_CALLS.md for setup.
"""
