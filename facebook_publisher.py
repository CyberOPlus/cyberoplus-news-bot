#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
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
from video_rights import evaluate_video_rights
from story_dedupe import annotate_story, find_duplicate_story


ROOT = Path(__file__).resolve().parent
READY = ROOT / "data" / "ready.jsonl"
EVENTS = ROOT / "data" / "facebook_events.jsonl"
TIMING_CONFIG = ROOT / "config" / "timing_strategy.json"
GRAPH_VERSION = os.environ.get("META_GRAPH_VERSION", "v26.0")
GRAPH_BASE = f"https://graph.facebook.com/{GRAPH_VERSION}"
VIDEO_UPLOAD_TIMEOUT = int(os.environ.get("FACEBOOK_VIDEO_UPLOAD_TIMEOUT_SECONDS", "1800"))


def _load_timing_config() -> dict:
    try:
        return json.loads(TIMING_CONFIG.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"WARNING timing config fallback: {exc}", file=sys.stderr)
        return {}


TIMING = _load_timing_config()


def _configured_min_gap() -> int:
    override = os.environ.get("FACEBOOK_MIN_GAP_MINUTES", "").strip()
    if override:
        try:
            return max(0, int(override))
        except ValueError as exc:
            raise RuntimeError("FACEBOOK_MIN_GAP_MINUTES must be an integer") from exc
    policy = TIMING.get("publishing_policy") or {}
    return max(0, int(policy.get("minimum_gap_minutes", 12)))


MIN_GAP = _configured_min_gap()


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


def terminal_ids() -> set[int]:
    terminal = set()
    for event in read_jsonl(EVENTS):
        if event.get("event") not in {"published", "duplicate_skipped"}:
            continue
        try:
            tid = int(event.get("telegram_id", 0) or 0)
        except (TypeError, ValueError):
            tid = 0
        if tid > 0:
            terminal.add(tid)
    return terminal


def _normalize_delivery_message(text: str) -> str:
    value = re.sub(r"[\u200e\u200f\u202a-\u202e\u2066-\u2069]", "", str(text or ""))
    return re.sub(r"\s+", " ", value).strip().casefold()


def recover_existing_remote_post(page_id: str, token: str, message: str) -> str:
    """Recover a Meta write accepted before timeout/state persistence failed."""
    needle = _normalize_delivery_message(message)
    if not needle:
        return ""
    try:
        payload = api(
            "GET",
            f"{page_id}/posts",
            token,
            params={"fields": "id,message,created_time", "limit": "100"},
            timeout=45,
        )
    except Exception as exc:
        print(f"WARNING Facebook recovery lookup failed: {exc}", file=sys.stderr)
        return ""
    for row in payload.get("data") or []:
        if _normalize_delivery_message(row.get("message") or "") == needle:
            return str(row.get("id") or "")
    return ""


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
            timeout=(30, VIDEO_UPLOAD_TIMEOUT),
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


def _plain_visible(text: str) -> str:
    value = re.sub(r"[\u200e\u200f\u202a-\u202e\u2066-\u2069]", "", str(text or ""))
    return re.sub(r"\s+", " ", value).strip()


def _message_key(text: str) -> str:
    value = _plain_visible(text)
    value = re.sub(r"[^\w\u0600-\u06FF]+", " ", value, flags=re.UNICODE)
    return re.sub(r"\s+", " ", value).strip().casefold()


def _attention_marker(item: dict) -> str:
    editorial = item.get("editorial") or {}
    attention = str(editorial.get("attention_label") or "none")
    content_type = str(editorial.get("content_type") or "general")

    if attention == "breaking":
        return "🚨 عاجل:"
    if attention == "warning":
        return "⚠️ تحذير:"
    if content_type in {"security_alert", "vulnerability"}:
        return "🔐"
    if content_type == "incident":
        return "🛡️"
    if content_type == "product_update":
        return "🆕"
    return ""


def _topic_hashtag(item: dict) -> str:
    policy = TIMING.get("caption_policy") or {}
    if not policy.get("add_topic_hashtag", True):
        return ""
    tags = policy.get("topic_hashtags") or {}
    editorial = item.get("editorial") or {}
    content_type = str(editorial.get("content_type") or "general")
    text = _plain_visible(
        " ".join(
            [
                str(item.get("title") or ""),
                str(item.get("card_title") or ""),
                str(item.get("facebook_post") or ""),
                str(editorial.get("main_fact") or ""),
            ]
        )
    ).casefold()

    if content_type in {"security_alert", "vulnerability", "incident"} or re.search(
        r"\b(?:cve-|malware|ransomware|phishing|exploit|breach|hack|cyber)\b|"
        r"(?:ثغرة|اختراق|برمجية خبيثة|فدية|تصيد|أمن سيبراني|تسريب بيانات)",
        text,
        flags=re.I,
    ):
        return str(tags.get("cybersecurity") or "#الأمن_السيبراني")
    if re.search(
        r"\b(?:openai|anthropic|gemini|claude|chatgpt|llm|gpt[- ]?\w*)\b|"
        r"(?:الذكاء الاصطناعي|نموذج لغوي|نماذج لغوية)",
        text,
        flags=re.I,
    ):
        return str(tags.get("artificial_intelligence") or "#الذكاء_الاصطناعي")
    if re.search(r"\bprivacy\b|(?:الخصوصية|بيانات شخصية)", text, flags=re.I):
        return str(tags.get("privacy") or "#الخصوصية")
    if content_type in {"tool", "product_update", "report", "update", "breaking_news"} or re.search(
        r"\b(?:apple|microsoft|google|android|iphone|windows|linux|software|hardware|tech)\b|"
        r"(?:تقنية|تحديث|برنامج|هاتف|نظام تشغيل)",
        text,
        flags=re.I,
    ):
        return str(tags.get("technology") or "#تقنية")
    return ""


def _delivery_hashtags(item: dict) -> list[str]:
    policy = TIMING.get("caption_policy") or {}
    maximum = max(1, int(policy.get("max_hashtags", 2)))
    brand = str(policy.get("brand_hashtag") or "#CyberoPlus").strip()
    values = _unique([_topic_hashtag(item), brand])
    return values[:maximum]


def build_delivery_message(item: dict) -> str:
    """Render Facebook-native copy without changing factual editorial wording."""
    title = str(item.get("title") or "").strip()
    body = str(item.get("facebook_post") or "").strip()
    if not title:
        title = str(item.get("card_title") or "").strip()

    marker = _attention_marker(item)
    lead = f"{marker} {title}".strip() if marker and title else (marker or title)

    if lead and body:
        first = next((part.strip() for part in re.split(r"\n\s*\n", body) if part.strip()), "")
        if first and _message_key(first) == _message_key(title):
            parts = [part.strip() for part in re.split(r"\n\s*\n", body) if part.strip()]
            body = "\n\n".join(parts[1:]).strip()

    visible_parts = [part for part in (lead, body) if str(part or "").strip()]
    if not visible_parts:
        raise RuntimeError("Post has no visible Facebook text.")

    hashtags = _delivery_hashtags(item)
    message = "\n\n".join(visible_parts)
    if hashtags:
        message += "\n\n" + " ".join(hashtags)

    if re.search(r"https?://", message, flags=re.I):
        raise RuntimeError("Facebook body unexpectedly contains an external URL.")
    if len(re.findall(r"(?<!\w)#[\w\u0600-\u06FF_]+", message)) > 2:
        raise RuntimeError("Facebook body contains too many hashtags.")
    return message.strip()


def _source_datetime(item: dict) -> datetime | None:
    raw = str(item.get("source_published_at") or "").strip()
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def queue_priority_key(item: dict) -> tuple:
    """Urgent first, then security relevance, with an aging guard for normal news."""
    editorial = item.get("editorial") or {}
    attention = str(editorial.get("attention_label") or "none")
    content_type = str(editorial.get("content_type") or "general")
    attention_rank = {"breaking": 0, "warning": 1, "important": 2, "none": 3}.get(attention, 3)
    source_dt = _source_datetime(item)
    if source_dt and now() - source_dt >= timedelta(hours=int((TIMING.get("publishing_policy") or {}).get("aging_boost_after_hours", 6))):
        attention_rank = min(attention_rank, 2)
    type_rank = {
        "breaking_news": 0,
        "security_alert": 0,
        "vulnerability": 0,
        "incident": 1,
        "product_update": 2,
        "update": 2,
        "tool": 3,
        "report": 3,
        "general": 4,
    }.get(content_type, 4)
    timestamp = source_dt.timestamp() if source_dt else 0.0
    tid = int(item.get("telegram_id", 0) or 0)
    return (attention_rank, type_rank, -timestamp, -tid)


def delivery_metadata(item: dict, message: str) -> dict:
    source_dt = _source_datetime(item)
    source_age_minutes = None
    if source_dt:
        source_age_minutes = max(0, int((now() - source_dt).total_seconds() // 60))
    editorial = item.get("editorial") or {}
    return {
        "policy_version": int(TIMING.get("version", 0) or 0),
        "content_type": editorial.get("content_type") or "general",
        "attention_label": editorial.get("attention_label") or "none",
        "hashtags": _delivery_hashtags(item),
        "attention_marker": _attention_marker(item),
        "caption_length": len(_plain_visible(message)),
        "paragraph_count": len([p for p in re.split(r"\n\s*\n", message) if p.strip()]),
        "source_age_minutes": source_age_minutes,
        "minimum_gap_minutes": MIN_GAP,
    }


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
        terminal = terminal_ids()
        pending = [
            row
            for row in ready
            if row.get("status") == "ready"
            and int(row.get("telegram_id", 0)) not in terminal
        ]
        pending = sorted(pending, key=queue_priority_key)

        target_id_raw = os.environ.get("FACEBOOK_TARGET_TELEGRAM_ID", "").strip()
        if target_id_raw:
            try:
                target_id = int(target_id_raw)
            except ValueError as exc:
                raise RuntimeError("FACEBOOK_TARGET_TELEGRAM_ID must be an integer") from exc
            pending = [
                row for row in pending
                if int(row.get("telegram_id", 0) or 0) == target_id
            ]

        if not pending:
            print('{"status":"queue_up_to_date"}')
            return 0

        if last and now() < last + timedelta(minutes=MIN_GAP):
            print(json.dumps({"status": "waiting_for_gap", "next_allowed_at": (last + timedelta(minutes=MIN_GAP)).isoformat()}))
            return 0

        item = annotate_story(pending[0])
        tid = int(item["telegram_id"])

        published_rows = [
            row for row in ready
            if int(row.get("telegram_id", 0) or 0) in posts
        ]
        duplicate = find_duplicate_story(item, published_rows)
        if duplicate:
            append_event({
                "event": "duplicate_skipped",
                "telegram_id": tid,
                "duplicate_of_telegram_id": duplicate["duplicate_of_telegram_id"],
                "duplicate_reason": duplicate["reason"],
                "duplicate_score": duplicate["score"],
                "story_fingerprint": item.get("story_fingerprint") or "",
                "skipped_at": now_iso(),
            })
            print(json.dumps({"status": "duplicate_story_skipped", "telegram_id": tid, **duplicate}, ensure_ascii=False, indent=2))
            return 0

        message = build_delivery_message(item)
        delivery_meta = delivery_metadata(item, message)

        recovered_post_id = recover_existing_remote_post(page_id, token, message)
        if recovered_post_id:
            first_comment = str(item.get("first_comment") or "").strip()
            append_event({
                "event": "published",
                "telegram_id": tid,
                "facebook_post_id": recovered_post_id,
                "published_at": now_iso(),
                "first_comment": first_comment,
                "source_url": item.get("source_url") or "",
                "media_mode": "recovered_existing",
                "recovered": True,
                "delivery": delivery_meta,
                "story_fingerprint": item.get("story_fingerprint") or "",
            })
            comment_status = publish_first_comment(tid, recovered_post_id, token, first_comment)
            print(json.dumps({
                "status": "recovered_existing_facebook_post",
                "telegram_id": tid,
                "facebook_post_id": recovered_post_id,
                "first_comment_status": comment_status,
            }, ensure_ascii=False, indent=2))
            return 0

        media = item.get("media") or {}
        telegram_post_url = str(media.get("telegram_post_url") or "").strip()
        first_comment = str(item.get("first_comment") or "").strip()
        media_errors: list[str] = []
        diagnostics: dict = {
            "declared_media_type": media.get("media_type") or "unknown",
            "video_attempted": False,
            "video_skipped_by_rights": False,
            "image_attempted": False,
            "generated_fallback_used": False,
        }

        rights = evaluate_video_rights(item) if media.get("has_video") else None
        if rights is not None:
            diagnostics["video_rights"] = rights.to_dict()

        if media.get("has_video") and rights is not None and rights.reupload_allowed:
            diagnostics["video_attempted"] = True
            video_urls = _unique(
                list(media.get("video_urls") or [])
                + ([media.get("video_url")] if media.get("video_url") else [])
            )
            try:
                with tempfile.TemporaryDirectory(prefix=f"cyberoplus-{tid}-") as temp_dir:
                    video_asset = prepare_branded_video(
                        video_urls,
                        telegram_post_url,
                        Path(temp_dir),
                        clip_start_seconds=rights.clip_start_seconds,
                        max_clip_seconds=rights.max_clip_seconds,
                    )
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
                        "delivery": delivery_meta,
                        "story_fingerprint": item.get("story_fingerprint") or "",
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
        elif media.get("has_video"):
            diagnostics["video_skipped_by_rights"] = True
            print(
                "INFO video re-upload skipped by rights gate: "
                + json.dumps(diagnostics.get("video_rights") or {}, ensure_ascii=False),
                file=sys.stderr,
            )

        diagnostics["image_attempted"] = True
        # When video re-upload is denied, do not silently republish a frame/poster
        # extracted from that same unverified video. Prefer a separate attached
        # image, the external source's hero image, or the owned fallback template.
        video_thumbnails = (
            list(media.get("video_thumbnail_urls") or [])
            if not media.get("has_video") or (rights is not None and rights.reupload_allowed)
            else []
        )
        stored_image_urls = _unique(
            list(media.get("image_urls") or [])
            + ([media.get("image_url")] if media.get("image_url") else [])
            + video_thumbnails
        )
        fresh_image_urls = telegram_image_candidates(telegram_post_url)
        telegram_image_urls = _unique(fresh_image_urls + stored_image_urls)

        source_image_url = str(item.get("source_url") or "").strip()
        assets, image_diagnostics = resolve_post_images(
            telegram_image_urls,
            source_image_url,
            max_images=10,
        )
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
                "delivery": delivery_meta,
                "story_fingerprint": item.get("story_fingerprint") or "",
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
