#!/usr/bin/env python3
"""Wait only until the configured start-to-start polling interval has elapsed."""
from __future__ import annotations
import json
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TIMING_CONFIG = ROOT / "config" / "timing_strategy.json"
DEFAULT_INTERVAL_SECONDS = 300

def configured_interval_seconds(path: Path = TIMING_CONFIG) -> int:
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
        minutes = float((config.get("publishing_policy") or {}).get("continuous_poll_minutes", 5))
        if not math.isfinite(minutes) or minutes <= 0:
            return DEFAULT_INTERVAL_SECONDS
        return max(60, int(round(minutes * 60)))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return DEFAULT_INTERVAL_SECONDS

def remaining_seconds(run_started_at: str, *, now: datetime | None = None,
                      interval_seconds: int = DEFAULT_INTERVAL_SECONDS) -> int:
    """Return seconds remaining to the target interval; bad timestamps fail safe."""
    interval_seconds = max(0, int(interval_seconds))
    if not str(run_started_at or "").strip():
        return interval_seconds
    try:
        started = datetime.fromisoformat(str(run_started_at).replace("Z", "+00:00"))
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return interval_seconds
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    elapsed = max(0.0, (current - started).total_seconds())
    return max(0, int(math.ceil(interval_seconds - elapsed)))

def main() -> int:
    interval = configured_interval_seconds()
    started = os.environ.get("RUN_STARTED_AT", "")
    wait_seconds = remaining_seconds(started, interval_seconds=interval)
    print(json.dumps({"poll_interval_seconds": interval, "run_started_at_present": bool(started), "wait_seconds": wait_seconds}))
    if wait_seconds:
        time.sleep(wait_seconds)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
