#!/usr/bin/env python3
"""
Process every collected Telegram item that has not yet been prepared for Facebook.

Pipeline:
Telegram inbox -> Gemini Arabic rewrite -> data/ready.jsonl

This script DOES NOT publish to Facebook.
It prepares an idempotent ready queue so the future Facebook publisher can run
without losing or duplicating items.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ai_rewriter import call_gemini, normalize_item

ROOT = Path(__file__).resolve().parent
INBOX_PATH = ROOT / "data" / "inbox.jsonl"
READY_PATH = ROOT / "data" / "ready.jsonl"
AI_STATE_PATH = ROOT / "data" / "ai_state.json"

MAX_RETRIES = 3


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []

    rows: list[dict[str, Any]] = []
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Invalid JSONL in {path} at line {line_number}") from exc
    return rows


def load_ai_state() -> dict[str, Any]:
    if not AI_STATE_PATH.exists():
        return {
            "last_processed_id": 0,
            "processed_count": 0,
            "updated_at": None,
        }

    return json.loads(AI_STATE_PATH.read_text(encoding="utf-8"))


def save_ai_state(last_processed_id: int, processed_count: int) -> None:
    AI_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    AI_STATE_PATH.write_text(
        json.dumps(
            {
                "last_processed_id": last_processed_id,
                "processed_count": processed_count,
                "updated_at": now_iso(),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def existing_ready_ids() -> set[int]:
    ids: set[int] = set()
    for row in read_jsonl(READY_PATH):
        try:
            ids.add(int(row["telegram_id"]))
        except (KeyError, TypeError, ValueError):
            continue
    return ids


def append_ready(row: dict[str, Any]) -> None:
    READY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with READY_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def prepare_one(raw_item: dict[str, Any]) -> dict[str, Any]:
    item = normalize_item(raw_item)

    last_error: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            model, result = call_gemini(item)
            return {
                "telegram_id": int(item["telegram_id"]),
                "source_published_at": item.get("published_at"),
                "prepared_at": now_iso(),
                "status": "ready",
                "language": result.get("language", "ar"),
                "title": result["title"],
                "facebook_post": result["facebook_post"],
                "first_comment": result["first_comment"],
                "source_url": result["source_url"],
                "media": {
                    "has_image": bool(raw_item.get("has_image")),
                    "image_url": raw_item.get("image_url"),
                    "has_video": bool(raw_item.get("has_video")),
                    "has_document": bool(raw_item.get("has_document")),
                },
                "ai_model": model,
            }
        except Exception as exc:
            last_error = exc
            if attempt < MAX_RETRIES:
                time.sleep(attempt * 4)

    raise RuntimeError(
        f"Failed to prepare Telegram item {item.get('telegram_id')} "
        f"after {MAX_RETRIES} attempts: {last_error}"
    )


def main() -> int:
    try:
        api_key = os.environ.get("GEMINI_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY is not available.")

        inbox = read_jsonl(INBOX_PATH)
        if not inbox:
            print(
                json.dumps(
                    {
                        "status": "no_collected_items",
                        "prepared_now": 0,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0

        state = load_ai_state()
        ready_ids = existing_ready_ids()

        ordered = sorted(
            inbox,
            key=lambda row: int(row.get("telegram_id", 0)),
        )

        pending = [
            row
            for row in ordered
            if int(row.get("telegram_id", 0)) > 0
            and int(row["telegram_id"]) not in ready_ids
        ]

        if not pending:
            newest = max(int(row.get("telegram_id", 0)) for row in ordered)
            print(
                json.dumps(
                    {
                        "status": "up_to_date",
                        "prepared_now": 0,
                        "latest_inbox_id": newest,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0

        prepared_ids: list[int] = []
        total_processed = int(state.get("processed_count", 0))

        for raw_item in pending:
            ready_row = prepare_one(raw_item)
            append_ready(ready_row)
            ready_ids.add(int(ready_row["telegram_id"]))
            prepared_ids.append(int(ready_row["telegram_id"]))
            total_processed += 1

        newest_processed = max(ready_ids) if ready_ids else 0
        save_ai_state(newest_processed, total_processed)

        print(
            json.dumps(
                {
                    "status": "prepared",
                    "prepared_now": len(prepared_ids),
                    "ids": prepared_ids,
                    "ready_queue_size": len(ready_ids),
                    "last_processed_id": newest_processed,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
