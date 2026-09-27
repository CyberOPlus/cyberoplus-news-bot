#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from image_resolver import (
    build_branded_fallback_asset,
    normalize_for_facebook,
    resolve_post_images,
    telegram_image_candidates,
)
from video_processor import prepare_branded_video


ROOT = Path(__file__).resolve().parent
READY = ROOT / "data" / "ready.jsonl"
EVENTS = ROOT / "data" / "facebook_events.jsonl"
GRAPH_VERSION = os.environ.get("META_GRAPH_VERSION", "v26.0")
GRAPH_BASE = f"https://graph.facebook.com/{GRAPH_VERSION}"
MIN_GAP = int(os.environ.get("FACEBOOK_MIN_GAP_MINUTES", "12"))


def now() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return now().isoformat()


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        if raw.strip():
            rows.append(json.loads(raw))
    return rows


def append_event(row: dict) -> None:
    EVENTS.parent.mkdir(parents=True, exist_ok=True)
    with EVENTS.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def api(method: str, path: str, token: str, data=None, params=None, timeout: int = 45):
    data = dict(data or {})
    params = dict(params or {})
    if method == "GET":
        params["access_token"] = token
    else:
        data["access_token"] = token

    response = requests.request(
        method,
        f"{GRAPH_BASE}/{path.lstrip('/')}",
        data=data if method != "GET" else None,
        params=params if method == "GET" else None,
        timeout=timeout,
    )
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(f"Meta returned non-JSON HTTP {response.status_code}.") from exc

    if not response.ok or "error" in payload:
        error = payload.get("error") or {}
        raise RuntimeError(
            f"{error.get('message') or response.status_code} "
            f"(code={error.get('code')}, subcode={error.get('error_subcode')})"
        )
    return payload


def verify(page_id: str, token: str) -> dict:
    payload = api("GET", page_id, token, params={"fields": "id,name"})
    if str(payload.get("id")) != str(page_id):
        raise RuntimeError("FACEBOOK_PAGE_ID does not match the Page token")
    return payload


def state() -> tuple[dict[int, dict], datetime | None]:
    posts: dict[int, dict] = {}
    last: datetime | None = None
    for event in read_jsonl(EVENTS):
        tid = int(event.get("telegram_id", 0) or 0)
        if event.get("event") == "published" and tid:
            posts[tid] = {
                "post_id": event.get("facebook_post_id"),
                "comment": event.get("first_comment") or "",
                "comment_posted": False,
            }
            try:
                published = datetime.fromisoformat(event.get("published_at"))
                if published.tzinfo is None:
                    published = published.replace(tzinfo=timezone.utc)
                if last is None or published > last:
                    last = published
            except Exception:
                pass
        elif event.get("event") == "comment_posted" and tid in posts:
            posts[tid]["comment_posted"] = True
    return posts, last


def post_comment(post_id: str, token: str, message: str) -> str:
    return api("POST", f"{post_id}/comments", token, data={"message": message}).get("id", "")


def retry_comments(posts: dict[int, dict], token: str) -> None:
    for tid, row in sorted(posts.items()):
        if row["comment"] and not row["comment_posted"]:
            try:
                comment_id = post_comment(row["post_id"], token, row["comment"])
                append_event(
                    {
                        "event": "comment_posted",
                        "telegram_id": tid,
                        "facebook_post_id": row["post_id"],
                        "comment_id": comment_id,
                        "commented_at": now_iso(),
                    }
                )
            except Exception as exc:
                print(f"WARNING comment retry {tid}: {exc}", file=sys.stderr)


def upload_photo_bytes(page_id: str, token: str, asset) -> str:
    normalized = normalize_for_facebook(asset)
    response = requests.post(
        f"{GRAPH_BASE}/{page_id}/photos",
        data={"access_token": token, "published": "false"},
        files={"source": (normalized.filename, normalized.content, normalized.mime)},
        timeout=75,
    )
    payload = response.json()
    if not response.ok or "error" in payload:
        error = payload.get("error") or {}
        raise RuntimeError(
            f"{error.get('message') or response.status_code} "
            f"(code={error.get('code')}, subcode={error.get('error_subcode')})"
        )
    return str(payload.get("id") or "")


def publish_images(page_id: str, token: str, message: str, photo_ids: list[str]) -> str:
    data = {"message": message}
    for index, photo_id in enumerate(photo_ids[:10]):
        data[f"attached_media[{index}]"] = json.dumps({"media_fbid": photo_id})
    payload = api("POST", f"{page_id}/feed", token, data=data)
    return str(payload.get("id") or "")


def publish_video_file(page_id: str, token: str, message: str, video_asset) -> str:
    data = {"access_token": token, "description": message}
    with video_asset.path.open("rb") as handle:
        response = requests.post(
            f"{GRAPH_BASE}/{page_id}/videos",
            data=data,
            files={"source": (video_asset.filename, handle, video_asset.mime)},
            timeout=(30, 360),
        )

    payload = response.json()
    if not response.ok or "error" in payload:
        error = payload.get("error") or {}
        raise RuntimeError(
            f"{error.get('message') or response.status_code} "
            f"(code={error.get('code')}, subcode={error.get('error_subcode')})"
        )
    video_id = str(payload.get("id") or "")
    if not video_id:
        raise RuntimeError("Meta video upload returned no video ID.")
    return video_id


def publish_first_comment(tid: int, post_id: str, token: str, first_comment: str) -> str:
    if not first_comment:
        return "none"
    try:
        comment_id = post_comment(post_id, token, first_comment)
        append_event(
            {
                "event": "comment_posted",
                "telegram_id": tid,
                "facebook_post_id": post_id,
                "comment_id": comment_id,
                "commented_at": now_iso(),
            }
        )
        return "posted"
    except Exception as exc:
        print(f"WARNING first comment pending retry: {exc}", file=sys.stderr)
        return "pending_retry"


def _unique(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        value = str(value or "").strip()
        if value and value not in result:
            result.append(value)
    return result


def main() -> int:
    try:
        page_id = os.environ["FACEBOOK_PAGE_ID"].strip()
        token = os.environ["FACEBOOK_PAGE_ACCESS_TOKEN"].strip()
        page = verify(page_id, token)
        print(json.dumps({"facebook_connection": "ok", "page_name": page.get("name"), "page_id": page.get("id")}, ensure_ascii=False))

        posts, last = state()
        retry_comments(posts, token)
        posts, last = state()

        ready = read_jsonl(READY)
        pending = [
            row
            for row in sorted(ready, key=lambda value: int(value.get("telegram_id", 0)))
            if row.get("status") == "ready"
            and int(row.get("telegram_id", 0)) not in posts
        ]
        if not pending:
            print('{"status":"queue_up_to_date"}')
            return 0

        if last and now() < last + timedelta(minutes=MIN_GAP):
            print(json.dumps({"status": "waiting_for_gap", "next_allowed_at": (last + timedelta(minutes=MIN_GAP)).isoformat()}))
            return 0

        item = pending[0]
        tid = int(item["telegram_id"])
        title = str(item.get("title") or "").strip()
        body = str(item.get("facebook_post") or "").strip()
        message = (title + "\n\n" + body).strip() if title and body else (title or body)

        media = item.get("media") or {}
        telegram_post_url = str(media.get("telegram_post_url") or "").strip()
        first_comment = str(item.get("first_comment") or "").strip()
        media_errors: list[str] = []
        diagnostics: dict = {
            "declared_media_type": media.get("media_type") or "unknown",
            "video_attempted": False,
            "image_attempted": False,
            "generated_fallback_used": False,
        }

        if media.get("has_video"):
            diagnostics["video_attempted"] = True
            video_urls = _unique(
                list(media.get("video_urls") or [])
                + ([media.get("video_url")] if media.get("video_url") else [])
            )
            try:
                with tempfile.TemporaryDirectory(prefix=f"cyberoplus-{tid}-") as temp_dir:
                    video_asset = prepare_branded_video(video_urls, telegram_post_url, Path(temp_dir))
                    diagnostics["video"] = {
                        "width": video_asset.width,
                        "height": video_asset.height,
                        "duration": video_asset.duration,
                        "bytes": video_asset.size,
                        "branded": video_asset.branded,
                    }
                    post_id = publish_video_file(page_id, token, message, video_asset)

                append_event(
                    {
                        "event": "published",
                        "telegram_id": tid,
                        "facebook_post_id": post_id,
                        "published_at": now_iso(),
                        "first_comment": first_comment,
                        "source_url": item.get("source_url") or "",
                        "media_mode": "video",
                        "media_errors": media_errors,
                        "media_diagnostics": diagnostics,
                    }
                )
                comment_status = publish_first_comment(tid, post_id, token, first_comment)
                print(json.dumps({"status": "published_to_facebook", "telegram_id": tid, "facebook_post_id": post_id, "media_mode": "video", "first_comment_status": comment_status, "media_diagnostics": diagnostics}, ensure_ascii=False, indent=2))
                return 0
            except Exception as exc:
                media_errors.append(f"video: {exc}")
                print(f"WARNING video fallback: {exc}", file=sys.stderr)

        diagnostics["image_attempted"] = True
        stored_image_urls = _unique(
            list(media.get("image_urls") or [])
            + ([media.get("image_url")] if media.get("image_url") else [])
            + list(media.get("video_thumbnail_urls") or [])
        )
        fresh_image_urls = telegram_image_candidates(telegram_post_url)
        telegram_image_urls = _unique(fresh_image_urls + stored_image_urls)

        assets, image_diagnostics = resolve_post_images(telegram_image_urls, "", max_images=10)
        diagnostics["images"] = image_diagnostics

        if not assets:
            if not message:
                raise RuntimeError("Post has no text and no usable Telegram image/video.")
            card_title = str(item.get("card_title") or title or body).strip()
            assets = [build_branded_fallback_asset(card_title, variant_key=tid)]
            diagnostics["generated_fallback_used"] = True
            diagnostics["images"]["selected"] = [{
                "origin": "generated_fallback",
                "width": assets[0].width,
                "height": assets[0].height,
                "url": assets[0].url,
            }]

        photo_ids: list[str] = []
        for asset in assets:
            try:
                photo_id = upload_photo_bytes(page_id, token, asset)
                if photo_id:
                    photo_ids.append(photo_id)
            except Exception as exc:
                media_errors.append(f"image: {exc}")
                print(f"WARNING image upload: {exc}", file=sys.stderr)

        if not photo_ids:
            raise RuntimeError("No image could be uploaded; refusing a text-only post.")

        if assets[0].origin == "generated_fallback":
            media_mode = "generated_card"
        elif len(photo_ids) > 1:
            media_mode = "images"
        else:
            media_mode = "image"

        post_id = publish_images(page_id, token, message, photo_ids)
        append_event(
            {
                "event": "published",
                "telegram_id": tid,
                "facebook_post_id": post_id,
                "published_at": now_iso(),
                "first_comment": first_comment,
                "source_url": item.get("source_url") or "",
                "media_mode": media_mode,
                "media_errors": media_errors,
                "media_diagnostics": diagnostics,
            }
        )

        comment_status = publish_first_comment(tid, post_id, token, first_comment)
        print(json.dumps({"status": "published_to_facebook", "telegram_id": tid, "facebook_post_id": post_id, "media_mode": media_mode, "first_comment_status": comment_status, "media_diagnostics": diagnostics}, ensure_ascii=False, indent=2))
        return 0

    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
