"""Asterisk AudioSocket protocol + audio utilities.

AudioSocket is Asterisk's dead-simple TCP audio protocol: each frame is
  1 byte  kind
  2 bytes big-endian payload length
  N bytes payload
Audio payloads are signed 16-bit little-endian mono PCM at 8 kHz
(20 ms = 320 bytes per frame).

Pure functions/classes here so they're unit-testable without Asterisk.
"""

from __future__ import annotations

import asyncio
import audioop
from typing import Optional, Tuple

# Frame kinds (per Asterisk's AudioSocket spec)
KIND_TERMINATE = 0x00  # hangup
KIND_UUID = 0x01       # first frame: 16-byte call UUID
KIND_AUDIO = 0x10      # 16-bit LE mono PCM @ 8 kHz
KIND_ERROR = 0xFF

SAMPLE_RATE = 8000
SAMPLE_WIDTH = 2  # bytes (16-bit)
FRAME_MS = 20
BYTES_PER_FRAME = SAMPLE_RATE * SAMPLE_WIDTH * FRAME_MS // 1000  # 320


def pack_frame(kind: int, payload: bytes = b"") -> bytes:
    return bytes([kind]) + len(payload).to_bytes(2, "big") + payload


async def read_frame(reader: asyncio.StreamReader) -> Tuple[int, bytes]:
    header = await reader.readexactly(3)
    kind = header[0]
    length = int.from_bytes(header[1:3], "big")
    payload = await reader.readexactly(length) if length else b""
    return kind, payload


def frame_rms(pcm: bytes) -> int:
    """Loudness of a PCM chunk (root mean square of 16-bit samples)."""
    return audioop.rms(pcm, SAMPLE_WIDTH) if pcm else 0


def resample(pcm: bytes, from_rate: int, to_rate: int) -> bytes:
    """Resample 16-bit mono PCM between sample rates."""
    if from_rate == to_rate:
        return pcm
    converted, _ = audioop.ratecv(pcm, SAMPLE_WIDTH, 1, from_rate, to_rate, None)
    return converted


class UtteranceDetector:
    """Energy-based end-of-utterance detection.

    Feed 20 ms audio frames; returns the full utterance once the speaker has
    been quiet for `silence_ms` after speaking (or `max_ms` is hit).
    """

    def __init__(
        self,
        threshold: int = 300,
        silence_ms: int = 700,
        max_ms: int = 15000,
        frame_ms: int = FRAME_MS,
    ):
        self.threshold = threshold
        self.silence_ms = silence_ms
        self.max_ms = max_ms
        self.frame_ms = frame_ms
        self.reset()

    def reset(self) -> None:
        self._buf = bytearray()
        self._started = False
        self._silence = 0

    def feed(self, frame: bytes) -> Optional[bytes]:
        loud = frame_rms(frame) >= self.threshold

        if not self._started:
            if not loud:
                return None
            self._started = True
            self._silence = 0

        self._buf.extend(frame)
        self._silence = 0 if loud else self._silence + self.frame_ms

        duration_ms = len(self._buf) * self.frame_ms // BYTES_PER_FRAME
        if self._silence >= self.silence_ms or duration_ms >= self.max_ms:
            utterance = bytes(self._buf)
            self.reset()
            return utterance
        return None
