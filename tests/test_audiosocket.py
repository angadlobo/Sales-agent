"""Tests for the AudioSocket protocol helpers (no Asterisk/network needed)."""

import asyncio
import struct

from sales_agent.outreach.selfhosted.audiosocket import (
    BYTES_PER_FRAME,
    KIND_AUDIO,
    KIND_UUID,
    UtteranceDetector,
    frame_rms,
    pack_frame,
    read_frame,
    resample,
)


def _loud_frame() -> bytes:
    # Alternating +/-8000 square wave — clearly above any silence threshold.
    return struct.pack("<160h", *([8000, -8000] * 80))


def _silent_frame() -> bytes:
    return b"\x00" * BYTES_PER_FRAME


def test_pack_frame_layout():
    frame = pack_frame(KIND_AUDIO, b"abc")
    assert frame[0] == KIND_AUDIO
    assert int.from_bytes(frame[1:3], "big") == 3
    assert frame[3:] == b"abc"


def test_read_frame_roundtrip():
    payload = b"\x01" * 16

    async def roundtrip():
        reader = asyncio.StreamReader()
        reader.feed_data(pack_frame(KIND_UUID, payload))
        reader.feed_eof()
        return await read_frame(reader)

    kind, got = asyncio.run(roundtrip())
    assert kind == KIND_UUID
    assert got == payload


def test_frame_rms_silence_vs_speech():
    assert frame_rms(_silent_frame()) == 0
    assert frame_rms(_loud_frame()) > 1000


def test_resample_changes_length_proportionally():
    pcm = _loud_frame()  # 20 ms @ 8 kHz
    up = resample(pcm, 8000, 16000)
    assert abs(len(up) - 2 * len(pcm)) <= 4  # ratecv may be off by a sample


def test_utterance_detector_full_cycle():
    det = UtteranceDetector(threshold=300, silence_ms=100, frame_ms=20)

    # Leading silence is ignored.
    assert det.feed(_silent_frame()) is None

    # Speech starts accumulating.
    for _ in range(5):
        assert det.feed(_loud_frame()) is None

    # 100 ms of silence ends the utterance.
    result = None
    for _ in range(5):
        result = det.feed(_silent_frame())
        if result:
            break
    assert result is not None
    assert len(result) >= 5 * BYTES_PER_FRAME  # contains the speech

    # Detector reset cleanly: silence alone doesn't retrigger.
    assert det.feed(_silent_frame()) is None


def test_utterance_detector_max_duration_cap():
    det = UtteranceDetector(threshold=300, silence_ms=10_000, max_ms=200, frame_ms=20)
    result = None
    for _ in range(20):
        result = det.feed(_loud_frame())
        if result:
            break
    assert result is not None  # cap fired even though speaker never paused
