#!/usr/bin/env python3
"""
CyberoPlus posting-time foundation.

For now this module uses the Facebook Insights snapshot supplied by the owner.
Later, when Meta API access is connected, the same strategy file can be updated
automatically from real post-performance data without manual monitoring.

Important:
- Every collected news item is still publishable.
- Timing never causes an item to be skipped.
- Freshness wins over waiting for a peak window.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config" / "timing_strategy.json"

# WET is UTC during standard time. We intentionally keep the same label used
# by the supplied Facebook Insights screenshot for the seed configuration.
TZ = ZoneInfo("UTC")


def load_config() -> dict[str, Any]:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def parse_hhmm(value: str) -> tuple[int, int]:
    hour, minute = value.split(":", 1)
    return int(hour), int(minute)


def peak_for_moment(now: datetime, config: dict[str, Any]) -> dict[str, Any] | None:
    weekday = now.strftime("%A")

    for window in config.get("observed_peak_windows", []):
        if window.get("weekday") != weekday:
            continue

        start_h, start_m = parse_hhmm(window["start"])
        end_h, end_m = parse_hhmm(window["end"])

        start = now.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
        end = now.replace(hour=end_h, minute=end_m, second=0, microsecond=0)

        if start <= now <= end:
            return window

    return None


def next_peak_start(now: datetime, config: dict[str, Any]) -> tuple[datetime, dict[str, Any]] | None:
    candidates: list[tuple[datetime, dict[str, Any]]] = []

    for days_ahead in range(0, 8):
        day = now + timedelta(days=days_ahead)
        weekday = day.strftime("%A")

        for window in config.get("observed_peak_windows", []):
            if window.get("weekday") != weekday:
                continue

            start_h, start_m = parse_hhmm(window["start"])
            start = day.replace(
                hour=start_h,
                minute=start_m,
                second=0,
                microsecond=0,
            )

            if start >= now:
                candidates.append((start, window))

    if not candidates:
        return None

    return min(candidates, key=lambda item: item[0])


def recommend_time(now: datetime, config: dict[str, Any]) -> dict[str, Any]:
    policy = config["publishing_policy"]
    max_delay = timedelta(minutes=int(policy["freshness_max_delay_minutes"]))

    current_peak = peak_for_moment(now, config)
    if current_peak:
        return {
            "publish_at": now.isoformat(),
            "reason": "inside_observed_peak_window",
            "peak_window": current_peak,
            "minimum_gap_minutes": int(policy["minimum_gap_peak_minutes"]),
        }

    upcoming = next_peak_start(now, config)
    if upcoming:
        start, window = upcoming
        wait = start - now

        if timedelta(0) <= wait <= max_delay:
            return {
                "publish_at": start.isoformat(),
                "reason": "short_wait_to_observed_peak",
                "wait_minutes": round(wait.total_seconds() / 60, 1),
                "peak_window": window,
                "minimum_gap_minutes": int(policy["minimum_gap_peak_minutes"]),
            }

    return {
        "publish_at": now.isoformat(),
        "reason": "freshness_over_peak_wait",
        "minimum_gap_minutes": int(policy["minimum_gap_off_peak_minutes"]),
    }


def build_summary(config: dict[str, Any]) -> dict[str, Any]:
    audience = config["audience_snapshot"]
    countries = audience["countries_percent"]
    largest_country = max(countries.items(), key=lambda item: item[1])

    ranked = sorted(
        config["observed_peak_windows"],
        key=lambda item: item["active_viewers"],
        reverse=True,
    )

    return {
        "timezone": config["timezone"],
        "largest_audience_country": {
            "country": largest_country[0],
            "percent": largest_country[1],
        },
        "observed_peak_windows_ranked": ranked,
        "policy": config["publishing_policy"],
        "note": (
            "Use the page's measured active-time windows before generic US posting-time advice. "
            "When Meta API is connected, the bot will learn from each published post automatically."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--at",
        help="ISO datetime to test. Example: 2026-09-27T14:40:00+00:00",
    )
    parser.add_argument(
        "--summary",
        action="store_true",
        help="Print the current audience/timing seed summary.",
    )
    args = parser.parse_args()

    config = load_config()

    if args.summary:
        print(json.dumps(build_summary(config), ensure_ascii=False, indent=2))
        return 0

    now = datetime.fromisoformat(args.at) if args.at else datetime.now(TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=TZ)
    now = now.astimezone(TZ)

    result = {
        "input_time": now.isoformat(),
        "recommendation": recommend_time(now, config),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
