#!/usr/bin/env python3
"""Cheap preflight for the scheduled GitHub Actions pipeline.

Uses only the Python standard library so expensive setup is skipped when the
bot is already up to date.
"""

from __future__ import annotations

import json
import os
import re
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parent
STATE = ROOT / "data" / "state.json"
INBOX = ROOT / "data" / "inbox.jsonl"
READY = ROOT / "data" / "ready.jsonl"
EVENTS = ROOT / "data" / "facebook_events.jsonl"

CHANNEL = "IntCyberDigest"
TELEGRAM_URL = f"https://t.me/s/{CHANNEL}"


def read_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []

    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        value = json.loads(line)
        if isinstance(value, dict):
            rows.append(value)
    return rows


def latest_telegram_id() -> int:
    request = urllib.request.Request(
        TELEGRAM_URL,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/154 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    with urllib.request.urlopen(request, timeout=18) as response:
        body = response.read().decode("utf-8", "replace")

    ids = [
        int(value)
        for value in re.findall(
            rf'data-post=["\']{re.escape(CHANNEL)}/(\d+)["\']',
            body,
        )
    ]
    if not ids:
        raise RuntimeError("Telegram preflight found no message IDs.")
    return max(ids)


def decision() -> tuple[bool, str, dict]:
    state = read_json(STATE, {})
    last_seen = int(state.get("last_seen_id", 0) or 0)

    inbox = read_jsonl(INBOX)
    ready = read_jsonl(READY)
    events = read_jsonl(EVENTS)

    ready_ids = {
        int(row.get("telegram_id", 0) or 0)
        for row in ready
        if int(row.get("telegram_id", 0) or 0) > 0
    }
    published_ids = {
        int(row.get("telegram_id", 0) or 0)
        for row in events
        if row.get("event") == "published"
        and int(row.get("telegram_id", 0) or 0) > 0
    }
    commented_ids = {
        int(row.get("telegram_id", 0) or 0)
        for row in events
        if row.get("event") == "comment_posted"
        and int(row.get("telegram_id", 0) or 0) > 0
    }

    ai_retry_ids = sorted(
        int(row.get("telegram_id", 0) or 0)
        for row in inbox
        if int(row.get("telegram_id", 0) or 0) > 0
        and int(row.get("telegram_id", 0) or 0) not in ready_ids
    )
    if ai_retry_ids:
        return True, "pending_ai_retry", {
            "last_seen": last_seen,
            "pending_ids": ai_retry_ids[:10],
        }

    publish_ids = sorted(
        int(row.get("telegram_id", 0) or 0)
        for row in ready
        if row.get("status") == "ready"
        and int(row.get("telegram_id", 0) or 0) > 0
        and int(row.get("telegram_id", 0) or 0) not in published_ids
    )
    if publish_ids:
        return True, "pending_facebook_queue", {
            "last_seen": last_seen,
            "pending_ids": publish_ids[:10],
        }

    pending_comment_ids = sorted(
        int(row.get("telegram_id", 0) or 0)
        for row in events
        if row.get("event") == "published"
        and str(row.get("first_comment") or "").strip()
        and int(row.get("telegram_id", 0) or 0) not in commented_ids
    )
    if pending_comment_ids:
        return True, "pending_first_comment_retry", {
            "last_seen": last_seen,
            "pending_ids": pending_comment_ids[:10],
        }

    latest = latest_telegram_id()
    if latest > last_seen:
        return True, "new_telegram_content", {
            "last_seen": last_seen,
            "latest_telegram_id": latest,
        }

    return False, "idle_up_to_date", {
        "last_seen": last_seen,
        "latest_telegram_id": latest,
    }


def write_output(name: str, value: str) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT", "").strip()
    if not output_path:
        return
    with open(output_path, "a", encoding="utf-8") as handle:
        handle.write(f"{name}={value}\n")


def main() -> int:
    try:
        needs_run, reason, details = decision()
    except Exception as exc:
        # Never sleep forever because the cheap probe itself had a problem.
        needs_run = True
        reason = "probe_failed_safe_full_run"
        details = {"error": str(exc)}

    write_output("needs_run", "true" if needs_run else "false")
    write_output("reason", reason)

    print(
        json.dumps(
            {
                "needs_run": needs_run,
                "reason": reason,
                **details,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
