#!/usr/bin/env python3
"""
Facebook bootstrap timing for CyberoPlus.

Important correction:
- The large audience/peak-time screenshot supplied earlier belongs to Threads.
- Threads timing data is NOT used to schedule Facebook posts.
- Until Facebook post-level data exists, news is published as soon as it is
  ready, with only a small gap between consecutive posts.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config" / "timing_strategy.json"


def load_config() -> dict[str, Any]:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def recommend_time(now: datetime, previous_publish_at: datetime | None, config: dict[str, Any]) -> dict[str, Any]:
    policy = config["publishing_policy"]
    gap = timedelta(minutes=int(policy["minimum_gap_minutes"]))

    if previous_publish_at is None:
        publish_at = now
        reason = "facebook_bootstrap_publish_when_ready"
    else:
        earliest = previous_publish_at + gap
        if now >= earliest:
            publish_at = now
            reason = "minimum_gap_already_satisfied"
        else:
            publish_at = earliest
            reason = "small_gap_between_consecutive_posts"

    return {
        "publish_at": publish_at.isoformat(),
        "reason": reason,
        "minimum_gap_minutes": int(policy["minimum_gap_minutes"]),
        "threads_timing_used": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--at", help="Current ISO time for testing")
    parser.add_argument("--previous", help="Previous Facebook publish time in ISO format")
    parser.add_argument("--summary", action="store_true")
    args = parser.parse_args()

    config = load_config()

    if args.summary:
        print(json.dumps(config, ensure_ascii=False, indent=2))
        return 0

    now = datetime.fromisoformat(args.at) if args.at else datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    previous = None
    if args.previous:
        previous = datetime.fromisoformat(args.previous)
        if previous.tzinfo is None:
            previous = previous.replace(tzinfo=timezone.utc)

    print(
        json.dumps(
            recommend_time(now, previous, config),
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
