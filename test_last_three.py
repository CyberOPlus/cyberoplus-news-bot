#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
import time

from ai_rewriter import call_gemini, normalize_item
from facebook_publisher import (
    api,
    post_comment,
    publish,
    upload_photo_bytes,
    verify,
)
from image_resolver import build_branded_fallback_asset, resolve_post_images
from telegram_collector import fetch_page, parse_messages


OLD_BOT_POST_SUFFIXES = [
    "122218965404353607",
    "122219033858353607",
    "122219035802353607",
    "122219039612353607",
    "122219037008353607",
    "122219037128353607",
    "122219037404353607",
    "122219040992353607",
    "122219041190353607",
    "122219041364353607",
    "122219042180353607",
    "122219042354353607",
    "122219042564353607",
    "122219042942353607",
    "122219043062353607",
    "122219043248353607",
]


def cleanup_known_old_posts(page_id: str, token: str) -> list[dict]:
    results = []
    for suffix in OLD_BOT_POST_SUFFIXES:
        post_id = f"{page_id}_{suffix}"
        try:
            payload = api("DELETE", post_id, token)
            results.append({"post_id": post_id, "deleted": bool(payload.get("success", True))})
        except Exception as exc:
            results.append({"post_id": post_id, "deleted": False, "warning": str(exc)})
    return results


def compose(title: str, body: str) -> str:
    title = (title or "").strip()
    body = (body or "").strip()
    if title and body:
        return f"{title}\n\n{body}"
    return title or body


def telegram_images(raw_item: dict) -> list[str]:
    urls = [
        str(url).strip()
        for url in (raw_item.get("image_urls") or [])
        if str(url).strip()
    ]
    if not urls and str(raw_item.get("image_url") or "").strip():
        urls.append(str(raw_item["image_url"]).strip())
    return urls


def live_verify(post_id: str, token: str) -> dict:
    # Verify the object that Facebook actually stored, not only our request.
    try:
        return api(
            "GET",
            post_id,
            token,
            params={
                "fields": (
                    "id,message,created_time,permalink_url,full_picture,"
                    "attachments{media_type,url,target,media,subattachments}"
                )
            },
        )
    except Exception as exc:
        return {"verification_error": str(exc), "id": post_id}


def main() -> int:
    try:
        page_id = os.environ["FACEBOOK_PAGE_ID"].strip()
        token = os.environ["FACEBOOK_PAGE_ACCESS_TOKEN"].strip()
        page = verify(page_id, token)

        messages = sorted(
            parse_messages(fetch_page()),
            key=lambda item: int(item.get("telegram_id", 0)),
            reverse=True,
        )[:3]

        if len(messages) < 3:
            raise RuntimeError(f"Expected 3 Telegram posts, found {len(messages)}")

        results = []

        # Publish oldest -> newest so Facebook ends in the same chronological order
        # as the Telegram channel.
        for position, raw_item in enumerate(reversed(messages), start=1):
            item = normalize_item(raw_item)
            model, rewritten = call_gemini(item)

            message = compose(
                rewritten.get("title", ""),
                rewritten.get("facebook_post", ""),
            )
            if not message:
                raise RuntimeError(
                    f"Telegram {item['telegram_id']} produced no publishable text."
                )

            source_url = str(rewritten.get("source_url") or "").strip()
            assets, diagnostics = resolve_post_images(
                telegram_images(raw_item),
                source_url,
                max_images=10,
            )

            # User requirement: never publish text-only.
            if not assets:
                assets = [build_branded_fallback_asset(message)]
                diagnostics["generated_fallback_used"] = True
                diagnostics["selected"] = [{
                    "origin": "generated_fallback",
                    "width": assets[0].width,
                    "height": assets[0].height,
                    "url": assets[0].url,
                }]
            else:
                diagnostics["generated_fallback_used"] = False

            photo_ids = []
            warnings = []
            for asset in assets:
                try:
                    photo_id = upload_photo_bytes(page_id, token, asset)
                    if photo_id:
                        photo_ids.append(photo_id)
                except Exception as exc:
                    warnings.append(f"image: {exc}")

            if not photo_ids:
                raise RuntimeError(
                    f"Telegram {item['telegram_id']} has no successfully uploaded image; "
                    "refusing to publish a text-only test post."
                )

            post_id = publish(page_id, token, message, photo_ids)

            first_comment = str(rewritten.get("first_comment") or "").strip()
            comment_id = ""
            if first_comment:
                try:
                    comment_id = post_comment(post_id, token, first_comment)
                except Exception as exc:
                    warnings.append(f"comment: {exc}")

            # Give Graph API a moment, then read back the live Facebook object.
            time.sleep(2)
            live = live_verify(post_id, token)

            results.append({
                "test_position": position,
                "telegram_id": int(item["telegram_id"]),
                "facebook_post_id": post_id,
                "page_name": page.get("name"),
                "ai_model": model,
                "message": message,
                "source_url": source_url,
                "first_comment": first_comment,
                "comment_posted": bool(comment_id) if first_comment else None,
                "images_published": len(photo_ids),
                "image_diagnostics": diagnostics,
                "live_facebook_verification": {
                    "id": live.get("id"),
                    "created_time": live.get("created_time"),
                    "permalink_url": live.get("permalink_url"),
                    "full_picture": live.get("full_picture"),
                    "has_message": bool(live.get("message")),
                    "has_attachments": bool(live.get("attachments")),
                    "verification_error": live.get("verification_error"),
                },
                "warnings": warnings,
            })

            if position < 3:
                time.sleep(3)

        print(json.dumps({
            "status": "three_darija_image_posts_published_and_verified",
            "results": results,
        }, ensure_ascii=False, indent=2))
        return 0

    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
