"""Append-only activity log — the permanent record of everything the agent does.

Every search, every email (with full body), and every call conversation turn
is written to data/activity.jsonl so it can be reviewed later, from any entry
point (web UI, CLI, phone servers). The web UI surfaces it in the History
panel via /api/history.

Logging is best-effort and never raises: losing a log line must not break a
campaign or drop a live call.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List

logger = logging.getLogger(__name__)

ACTIVITY_PATH = Path("data") / "activity.jsonl"
_LOCK = threading.Lock()


def log(event: str, **detail: Any) -> None:
    """Append one event to data/activity.jsonl."""
    entry = {"time": datetime.now(timezone.utc).isoformat(), "event": event, **detail}
    try:
        with _LOCK:
            ACTIVITY_PATH.parent.mkdir(parents=True, exist_ok=True)
            with ACTIVITY_PATH.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    except Exception:  # noqa: BLE001 - logging must not break the pipeline
        logger.exception("Failed to write activity log")


def read(limit: int = 200) -> List[dict]:
    """Most recent activity entries, newest first."""
    if not ACTIVITY_PATH.exists():
        return []
    entries: List[dict] = []
    try:
        for line in ACTIVITY_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except Exception:  # noqa: BLE001
        logger.exception("Failed to read activity log")
    return entries[-limit:][::-1]
