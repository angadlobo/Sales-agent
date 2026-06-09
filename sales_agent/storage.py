"""Persist results to disk as JSON and CSV."""

from __future__ import annotations

import csv
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import List

from .models import QualifiedLead

logger = logging.getLogger(__name__)


def save_run(leads: List[QualifiedLead], data_dir: Path) -> dict[str, Path]:
    """Write a JSON snapshot and a flat CSV. Returns the paths written."""
    data_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")

    json_path = data_dir / f"run-{stamp}.json"
    json_path.write_text(
        json.dumps([ql.model_dump(mode="json") for ql in leads], indent=2)
    )

    csv_path = data_dir / f"run-{stamp}.csv"
    with csv_path.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            ["company", "contact", "email", "phone", "location", "score", "outreach"]
        )
        for ql in leads:
            channels = ", ".join(f"{o.channel.value}:{o.status.value}" for o in ql.outreach)
            writer.writerow(
                [
                    ql.lead.company_name,
                    ql.lead.contact_name or "",
                    ql.lead.email or "",
                    ql.lead.phone or "",
                    ql.lead.location or "",
                    ql.best_score,
                    channels,
                ]
            )

    logger.info("Saved run to %s and %s", json_path, csv_path)
    return {"json": json_path, "csv": csv_path}
