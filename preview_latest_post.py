#!/usr/bin/env python3
"""Generate one complete Cybero Plus dry-run preview without touching Facebook."""

from __future__ import annotations

import html
import json
import shutil
from pathlib import Path
from typing import Any

from ai_rewriter import call_gemini, normalize_item
from image_resolver import (
    build_branded_fallback_asset,
    normalize_for_facebook,
    resolve_post_images,
    telegram_image_candidates,
)
from telegram_collector import fetch_page, parse_messages
from video_processor import prepare_branded_video


ROOT = Path(__file__).resolve().parent
OUT = ROOT / "preview-output"


def unique(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        value = str(value or "").strip()
        if value and value not in result:
            result.append(value)
    return result


def latest_with_context() -> dict[str, Any]:
    messages = sorted(
        parse_messages(fetch_page()),
        key=lambda item: int(item.get("telegram_id", 0) or 0),
    )
    if not messages:
        raise RuntimeError("Telegram preview returned no messages.")

    current = dict(messages[-1])
    by_id = {
        int(row["telegram_id"]): row
        for row in messages
        if int(row.get("telegram_id", 0) or 0) > 0
    }

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
                "media_type": parent.get("media_type") or "unknown",
            }
        )
        try:
            parent_id = int(parent.get("reply_to_id") or 0)
        except (TypeError, ValueError):
            parent_id = 0

    current["reply_context"] = list(reversed(context))
    return current


def prepare_text(raw: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    item = normalize_item(raw)
    if not str(item.get("text") or "").strip():
        return "not_required_media_only", {
            "facebook_post": "",
            "first_comment": "",
            "source_url": "",
            "card_title": "",
            "content_type": "general",
            "attention_label": "none",
            "certainty": "confirmed",
        }
    return call_gemini(item)


def save_image_asset(asset, filename: str) -> dict[str, Any]:
    normalized = normalize_for_facebook(asset)
    target = OUT / filename
    target.write_bytes(normalized.content)
    return {
        "file": target.name,
        "origin": normalized.origin,
        "width": normalized.width,
        "height": normalized.height,
        "bytes": len(normalized.content),
    }


def render_media(raw: dict[str, Any], result: dict[str, Any]) -> tuple[str, list[dict[str, Any]], list[str]]:
    errors: list[str] = []
    artifacts: list[dict[str, Any]] = []
    tid = int(raw["telegram_id"])
    post_url = str(raw.get("telegram_post_url") or "").strip()

    if raw.get("has_video"):
        known_video_urls = unique(
            list(raw.get("video_urls") or [])
            + ([raw.get("video_url")] if raw.get("video_url") else [])
        )
        try:
            asset = prepare_branded_video(known_video_urls, post_url, OUT)
            target = OUT / "preview-video.mp4"
            if asset.path != target:
                shutil.move(str(asset.path), str(target))
            artifacts.append(
                {
                    "file": target.name,
                    "origin": "telegram_video",
                    "width": asset.width,
                    "height": asset.height,
                    "duration": asset.duration,
                    "bytes": target.stat().st_size,
                    "branded": True,
                }
            )
            return "video", artifacts, errors
        except Exception as exc:
            errors.append(f"video: {exc}")

    stored_images = unique(
        list(raw.get("image_urls") or [])
        + ([raw.get("image_url")] if raw.get("image_url") else [])
        + list(raw.get("video_thumbnail_urls") or [])
    )
    fresh_images = telegram_image_candidates(post_url)
    image_urls = unique(fresh_images + stored_images)

    source_url = str(result.get("source_url") or "").strip()
    assets, diagnostics = resolve_post_images(image_urls, source_url, max_images=10)
    if assets:
        for index, asset in enumerate(assets, start=1):
            artifacts.append(save_image_asset(asset, f"preview-image-{index:02d}.jpg"))
        return ("images" if len(artifacts) > 1 else "image"), artifacts, errors

    body = str(result.get("facebook_post") or "").strip()
    card_title = str(result.get("card_title") or body).strip()
    if card_title:
        asset = build_branded_fallback_asset(card_title, variant_key=tid)
        artifacts.append(save_image_asset(asset, "preview-card.jpg"))
        return "generated_card", artifacts, errors

    errors.append("No usable text, image or video was available for preview.")
    return "unsupported", artifacts, errors


def write_summary(
    raw: dict[str, Any],
    model: str,
    result: dict[str, Any],
    media_mode: str,
    media_files: list[dict[str, Any]],
    errors: list[str],
) -> None:
    tid = int(raw["telegram_id"])
    post = str(result.get("facebook_post") or "").strip()
    comment = str(result.get("first_comment") or "").strip()

    manifest = {
        "preview_only": True,
        "facebook_called": False,
        "telegram_id": tid,
        "telegram_post_url": raw.get("telegram_post_url"),
        "reply_to_id": raw.get("reply_to_id"),
        "media_type_detected": raw.get("media_type"),
        "media_mode_preview": media_mode,
        "model": model,
        "editorial": {
            "content_type": result.get("content_type"),
            "attention_label": result.get("attention_label"),
            "certainty": result.get("certainty"),
        },
        "facebook_post": post,
        "first_comment": comment,
        "card_title": result.get("card_title") or "",
        "media_files": media_files,
        "errors": errors,
    }
    (OUT / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (OUT / "post.txt").write_text(post + "\n", encoding="utf-8")
    (OUT / "first-comment.txt").write_text(comment + "\n", encoding="utf-8")

    media_lines = "\n".join(
        f"- \`{row['file']}\` — {row.get('origin','')} "
        f"{row.get('width','')}x{row.get('height','')}"
        for row in media_files
    ) or "- لا يوجد ملف وسائط صالح."

    error_lines = "\n".join(f"- {html.escape(value)}" for value in errors) or "- لا توجد أخطاء."

    summary = f"""# Cybero Plus — Preview فقط، بلا Facebook

**Telegram ID:** {tid}  
**Media detected:** {html.escape(str(raw.get('media_type') or 'unknown'))}  
**Preview media mode:** {html.escape(media_mode)}  
**AI model:** {html.escape(model)}  
**Facebook API called:** **NO**

## النص النهائي
<div dir="rtl"><pre>{html.escape(post or '[منشور ميديا بلا نص]')}</pre></div>

## التعليق الأول
<div dir="rtl"><pre>{html.escape(comment or '[لا يوجد]')}</pre></div>

## الملفات
{media_lines}

## ملاحظات/Fallback
{error_lines}

> لمعاينة الصورة أو الفيديو نفسه: افتح Artifact باسم **cyberoplus-post-preview** من أسفل صفحة التشغيل.
"""
    (OUT / "summary.md").write_text(summary, encoding="utf-8")


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)

    raw = latest_with_context()
    model, result = prepare_text(raw)
    media_mode, media_files, errors = render_media(raw, result)
    write_summary(raw, model, result, media_mode, media_files, errors)

    print(
        json.dumps(
            {
                "status": "preview_only_not_published",
                "facebook_called": False,
                "telegram_id": raw.get("telegram_id"),
                "media_mode": media_mode,
                "model": model,
                "files": [row.get("file") for row in media_files],
                "errors": errors,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
