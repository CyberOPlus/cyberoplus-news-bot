#!/usr/bin/env python3
"""Preview one known inbox item through the AI chain without calling Facebook."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from ai_batch_processor import enrich_reply_context, read_jsonl
from ai_rewriter import call_gemini, normalize_item
from facebook_publisher import build_delivery_message


ROOT = Path(__file__).resolve().parent
INBOX = ROOT / "data" / "inbox.jsonl"
OUT = ROOT / "preview-output"


def main() -> int:
    telegram_id = int(sys.argv[1]) if len(sys.argv) > 1 else 1693
    rows = read_jsonl(INBOX)
    target = next(
        (row for row in rows if int(row.get("telegram_id", 0) or 0) == telegram_id),
        None,
    )
    if target is None:
        raise RuntimeError(f"Telegram item {telegram_id} not found in inbox.")

    enriched = enrich_reply_context([target], rows)[0]
    item = normalize_item(enriched)
    model, result = call_gemini(item)

    delivery_item = {
        "telegram_id": telegram_id,
        "source_published_at": item.get("published_at"),
        "title": result.get("title") or "",
        "facebook_post": result.get("facebook_post") or "",
        "card_title": result.get("card_title") or "",
        "editorial": {
            "content_type": result.get("content_type") or "general",
            "attention_label": result.get("attention_label") or "none",
            "certainty": result.get("certainty") or "confirmed",
            "main_fact": result.get("main_fact") or "",
        },
    }

    payload: dict[str, Any] = {
        "preview_only": True,
        "facebook_called": False,
        "telegram_id": telegram_id,
        "model": model,
        "title": result.get("title") or "",
        "facebook_post": result.get("facebook_post") or "",
        "delivery_message": build_delivery_message(delivery_item),
        "first_comment": result.get("first_comment") or "",
        "card_title": result.get("card_title") or "",
        "content_type": result.get("content_type") or "general",
        "attention_label": result.get("attention_label") or "none",
        "protected_numbers": result.get("protected_numbers") or [],
        "protected_entities": result.get("protected_entities") or [],
    }

    OUT.mkdir(parents=True, exist_ok=True)
    target_path = OUT / f"regression-{telegram_id}.json"
    target_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
