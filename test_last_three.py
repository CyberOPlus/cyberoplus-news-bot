#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
import time

from ai_rewriter import call_gemini, normalize_item
from facebook_publisher import (
    publish,
    post_comment,
    upload_photo,
    verify,
)
from telegram_collector import fetch_page, parse_messages


def compose(title: str, body: str) -> str:
    title = (title or "").strip()
    body = (body or "").strip()
    return (title + "\n\n" + body).strip() if title and body else (title or body)


def main() -> int:
    try:
        page_id = os.environ["FACEBOOK_PAGE_ID"].strip()
        token = os.environ["FACEBOOK_PAGE_ACCESS_TOKEN"].strip()
        verify(page_id, token)

        messages = sorted(
            parse_messages(fetch_page()),
            key=lambda item: int(item.get("telegram_id", 0)),
            reverse=True,
        )[:3]

        if len(messages) < 3:
            raise RuntimeError(f"Expected 3 Telegram posts, found {len(messages)}")

        results = []

        for position, raw_item in enumerate(messages, start=1):
            item = normalize_item(raw_item)
            model, rewritten = call_gemini(item)

            image_urls = [
                str(url).strip()
                for url in (raw_item.get("image_urls") or [])
                if str(url).strip()
            ]
            if not image_urls and raw_item.get("image_url"):
                image_urls = [str(raw_item["image_url"]).strip()]

            photo_ids = []
            media_errors = []
            for image_url in image_urls[:10]:
                try:
                    photo_id = upload_photo(page_id, token, image_url)
                    if photo_id:
                        photo_ids.append(photo_id)
                except Exception as exc:
                    media_errors.append(str(exc))

            message = compose(rewritten["title"], rewritten["facebook_post"])
            post_id = publish(page_id, token, message, photo_ids)

            first_comment = str(rewritten.get("first_comment") or "").strip()
            comment_id = ""
            if first_comment:
                try:
                    comment_id = post_comment(post_id, token, first_comment)
                except Exception as exc:
                    media_errors.append(f"comment: {exc}")

            results.append(
                {
                    "test_position": position,
                    "telegram_id": item["telegram_id"],
                    "facebook_post_id": post_id,
                    "title": rewritten["title"],
                    "source_url": rewritten.get("source_url") or "",
                    "images_found": len(image_urls),
                    "images_published": len(photo_ids),
                    "comment_posted": bool(comment_id),
                    "ai_model": model,
                    "warnings": media_errors,
                }
            )

            if position < len(messages):
                time.sleep(3)

        print(json.dumps({"status": "three_test_posts_published", "results": results}, ensure_ascii=False, indent=2))
        return 0

    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
