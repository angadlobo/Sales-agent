"""Free, local speech-to-text and text-to-speech.

STT: faster-whisper (open-source Whisper). Model downloads once, runs on CPU.
TTS: Piper if PIPER_VOICE is set (best quality), otherwise espeak-ng (apt
     install espeak-ng — robotic but dependency-free).

Both are imported/invoked lazily so the rest of the project never needs the
heavy dependencies. Install them with:  pip install -r requirements-selfhosted.txt
"""

from __future__ import annotations

import io
import logging
import os
import subprocess
import wave
from functools import lru_cache

from .audiosocket import SAMPLE_RATE, resample

logger = logging.getLogger(__name__)

WHISPER_MODEL = os.getenv("WHISPER_MODEL", "base")
PIPER_VOICE = os.getenv("PIPER_VOICE")          # path to a .onnx voice model
PIPER_RATE = int(os.getenv("PIPER_RATE", "22050"))
ESPEAK_VOICE = os.getenv("ESPEAK_VOICE", "en-US")


@lru_cache(maxsize=1)
def _whisper():
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "faster-whisper is not installed. Run: pip install -r requirements-selfhosted.txt"
        ) from exc
    logger.info("Loading Whisper model %r (first run downloads it)…", WHISPER_MODEL)
    return WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")


def transcribe(pcm_8k: bytes) -> str:
    """8 kHz 16-bit mono PCM -> text."""
    import numpy as np

    pcm_16k = resample(pcm_8k, SAMPLE_RATE, 16000)
    audio = np.frombuffer(pcm_16k, dtype=np.int16).astype(np.float32) / 32768.0
    segments, _info = _whisper().transcribe(audio, language="en", beam_size=1)
    return " ".join(seg.text.strip() for seg in segments).strip()


def synthesize(text: str) -> bytes:
    """Text -> 8 kHz 16-bit mono PCM ready for AudioSocket."""
    if PIPER_VOICE:
        return _piper(text)
    return _espeak(text)


def _piper(text: str) -> bytes:
    proc = subprocess.run(
        ["piper", "--model", PIPER_VOICE, "--output_raw"],
        input=text.encode(),
        capture_output=True,
        check=True,
    )
    return resample(proc.stdout, PIPER_RATE, SAMPLE_RATE)


def _espeak(text: str) -> bytes:
    try:
        proc = subprocess.run(
            ["espeak-ng", "--stdout", "-v", ESPEAK_VOICE, text],
            capture_output=True,
            check=True,
        )
    except FileNotFoundError as exc:  # pragma: no cover
        raise RuntimeError(
            "No TTS available: set PIPER_VOICE for Piper, or install espeak-ng "
            "(e.g. apt install espeak-ng)."
        ) from exc

    with wave.open(io.BytesIO(proc.stdout)) as wav:
        rate = wav.getframerate()
        channels = wav.getnchannels()
        frames = wav.readframes(wav.getnframes())
    if channels == 2:
        import audioop

        frames = audioop.tomono(frames, 2, 0.5, 0.5)
    return resample(frames, rate, SAMPLE_RATE)
