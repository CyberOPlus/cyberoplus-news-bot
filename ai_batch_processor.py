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
from story_dedupe import annotate_story, find_duplicate_story

ROOT = Path(__file__).resolve().parent
INBOX_PATH = ROOT / "data" / "inbox.jsonl"
READY_PATH = ROOT / "data" / "ready.jsonl"
AI_STATE_PATH = ROOT / "data" / "ai_state.json"

MAX_RETRIES = max(1, int(os.environ.get("AI_ITEM_ATTEMPTS", "1")))


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
            pass
        for value in row.get("telegram_ids") or []:
            try:
                ids.add(int(value))
            except (TypeError, ValueError):
                continue
    return ids


def enrich_reply_context(
    rows: list[dict[str, Any]],
    all_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep every Telegram message separate while attaching parent context."""
    by_id = {
        int(row["telegram_id"]): row
        for row in all_rows
        if int(row.get("telegram_id", 0) or 0) > 0
    }

    enriched: list[dict[str, Any]] = []
    for row in rows:
        current = dict(row)
        tid = int(current.get("telegram_id", 0) or 0)
        current["telegram_ids"] = [tid] if tid else []

        context: list[dict[str, Any]] = []
        seen: set[int] = set()
        try:
            parent_id = int(current.get("reply_to_id") or 0)
        except (TypeError, ValueError):
            parent_id = 0

        for _ in range(3):
            if not parent_id or parent_id in seen:
                break
            seen.add(parent_id)
            parent = by_id.get(parent_id)
            if not parent:
                break

            context.append(
                {
                    "telegram_id": parent_id,
                    "text": str(
                        parent.get("clean_text")
                        or parent.get("raw_text")
                        or ""
                    ).strip()[:4000],
                    "source_links": parent.get("source_links") or [],
                    "media_type": parent.get("media_type") or (
                        "video" if parent.get("has_video")
                        else "image" if parent.get("has_image")
                        else "document" if parent.get("has_document")
                        else "text"
                    ),
                }
            )
            try:
                parent_id = int(parent.get("reply_to_id") or 0)
            except (TypeError, ValueError):
                parent_id = 0

        current["reply_context"] = list(reversed(context))
        enriched.append(current)

    return sorted(enriched, key=lambda row: int(row.get("telegram_id", 0) or 0))



def append_ready(row: dict[str, Any]) -> None:
    READY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with READY_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def prepare_one(raw_item: dict[str, Any]) -> dict[str, Any]:
    item = normalize_item(raw_item)

    if not str(item.get("text") or "").strip():
        supported_media = bool(raw_item.get("has_image") or raw_item.get("has_video"))
        return {
            "telegram_id": int(item["telegram_id"]),
            "telegram_ids": [
                int(value)
                for value in (raw_item.get("telegram_ids") or [item["telegram_id"]])
            ],
            "source_published_at": item.get("published_at"),
            "prepared_at": now_iso(),
            "status": "ready" if supported_media else "unsupported_media",
            "language": "ar",
            "title": "",
            "facebook_post": "",
            "first_comment": "",
            "source_url": "",
            "card_title": "",
            "link_role": "none",
            "media": {
                "has_image": bool(raw_item.get("has_image")),
                "image_url": raw_item.get("image_url"),
                "image_urls": raw_item.get("image_urls") or (
                    [raw_item.get("image_url")] if raw_item.get("image_url") else []
                ),
                "has_video": bool(raw_item.get("has_video")),
                "video_url": raw_item.get("video_url"),
                "video_urls": raw_item.get("video_urls") or (
                    [raw_item.get("video_url")] if raw_item.get("video_url") else []
                ),
                "video_thumbnail_urls": raw_item.get("video_thumbnail_urls") or [],
                "has_document": bool(raw_item.get("has_document")),
                "media_type": raw_item.get("media_type") or (
                    "video" if raw_item.get("has_video")
                    else "image" if raw_item.get("has_image")
                    else "document" if raw_item.get("has_document")
                    else "text"
                ),
                "telegram_post_url": raw_item.get("telegram_post_url") or "",
            },
            "ai_model": "not_required",
        }

    last_error: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            model, result = call_gemini(item)
            return {
                "telegram_id": int(item["telegram_id"]),
                "telegram_ids": [
                    int(value)
                    for value in (raw_item.get("telegram_ids") or [item["telegram_id"]])
                ],
                "source_published_at": item.get("published_at"),
                "prepared_at": now_iso(),
                "status": "ready",
                "language": result.get("language", "ar"),
                "title": result["title"],
                "facebook_post": result["facebook_post"],
                "first_comment": result["first_comment"],
                "source_url": result["source_url"],
                "card_title": result.get("card_title", ""),
                "link_role": result.get("link_role", "none"),
                "editorial": {
                    "content_type": result.get("content_type", "general"),
                    "attention_label": result.get("attention_label", "none"),
                    "attention_evidence": result.get("attention_evidence", ""),
                    "certainty": result.get("certainty", "confirmed"),
                    "main_fact": result.get("main_fact", ""),
                    "supporting_facts": result.get("supporting_facts", []),
                    "protected_entities": result.get("protected_entities", []),
                    "protected_numbers": result.get("protected_numbers", []),
                },
                "media": {
                    "has_image": bool(raw_item.get("has_image")),
                    "image_url": raw_item.get("image_url"),
                    "image_urls": raw_item.get("image_urls") or (
                        [raw_item.get("image_url")] if raw_item.get("image_url") else []
                    ),
                    "has_video": bool(raw_item.get("has_video")),
                    "video_url": raw_item.get("video_url"),
                    "video_urls": raw_item.get("video_urls") or (
                        [raw_item.get("video_url")] if raw_item.get("video_url") else []
                    ),
                    "video_thumbnail_urls": raw_item.get("video_thumbnail_urls") or [],
                    "has_document": bool(raw_item.get("has_document")),
                    "media_type": raw_item.get("media_type") or (
                        "video" if raw_item.get("has_video")
                        else "image" if raw_item.get("has_image")
                        else "document" if raw_item.get("has_document")
                        else "text"
                    ),
                    "telegram_post_url": raw_item.get("telegram_post_url") or "",
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
        available_ai_keys = [
            os.environ.get("GEMINI_API_KEY", "").strip(),
            os.environ.get("GROQ_API_KEY", "").strip(),
            os.environ.get("OPENROUTER_API_KEY", "").strip(),
        ]
        if not any(available_ai_keys):
            raise RuntimeError("No AI provider API key is available.")

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
        ready_rows = read_jsonl(READY_PATH)

        ordered = sorted(
            inbox,
            key=lambda row: int(row.get("telegram_id", 0)),
        )

        unprocessed = [
            row
            for row in ordered
            if int(row.get("telegram_id", 0)) > 0
            and int(row["telegram_id"]) not in ready_ids
        ]
        pending = enrich_reply_context(unprocessed, ordered)

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
        failed_ids: list[int] = []
        total_processed = int(state.get("processed_count", 0))

        for raw_item in pending:
            try:
                ready_row = prepare_one(raw_item)
            except Exception as exc:
                failed_id = int(raw_item.get("telegram_id", 0) or 0)
                failed_ids.append(failed_id)
                print(
                    f"WARNING: keeping Telegram item {failed_id} pending for retry: {exc}",
                    file=sys.stderr,
                )
                continue

            ready_row = annotate_story(ready_row)
            duplicate = find_duplicate_story(ready_row, ready_rows)
            if duplicate:
                ready_row["status"] = "duplicate"
                ready_row["duplicate_of_telegram_id"] = duplicate["duplicate_of_telegram_id"]
                ready_row["duplicate_reason"] = duplicate["reason"]
                ready_row["duplicate_score"] = duplicate["score"]
                print(
                    "INFO duplicate story suppressed before Facebook: "
                    + json.dumps({"telegram_id": ready_row["telegram_id"], **duplicate}, ensure_ascii=False)
                )

            append_ready(ready_row)
            ready_rows.append(ready_row)
            for value in ready_row.get("telegram_ids") or [ready_row["telegram_id"]]:
                ready_ids.add(int(value))
            prepared_ids.append(int(ready_row["telegram_id"]))
            total_processed += 1

        newest_processed = max(ready_ids) if ready_ids else 0
        save_ai_state(newest_processed, total_processed)

        print(
            json.dumps(
                {
                    "status": "prepared_with_failures" if failed_ids else "prepared",
                    "prepared_now": len(prepared_ids),
                    "ids": prepared_ids,
                    "failed_pending_ids": failed_ids,
                    "ready_queue_size": len(ready_ids),
                    "last_processed_id": newest_processed,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        # AI outages must not block publishing items that are already ready.
        # Failed items remain absent from ready.jsonl and are retried next run.
        return 0

    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
