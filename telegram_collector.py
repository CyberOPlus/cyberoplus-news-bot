#!/usr/bin/env python3
"""
Collect every new public Telegram post from @IntCyberDigest.

Goals:
- Never intentionally skip a new Telegram post.
- Persist every collected post in data/inbox.jsonl.
- Persist the latest seen Telegram ID in data/state.json.
- Keep raw content for internal processing, but also produce a cleaned version
  with International Cyber Digest self-links/branding removed.
- Keep third-party/original-source links for later use in the first Facebook comment.

No Telegram API token, login, AI key, or Facebook credential is required.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup


CHANNEL = "IntCyberDigest"
PUBLIC_URL = f"https://t.me/s/{CHANNEL}"
TIMEOUT_SECONDS = 20
MAX_BACKFILL_PAGES = 30

ROOT = Path(__file__).resolve().parent
STATE_PATH = ROOT / "data" / "state.json"
INBOX_PATH = ROOT / "data" / "inbox.jsonl"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/154.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

SELF_DOMAINS = {
    "internationalcyberdigest.com",
    "www.internationalcyberdigest.com",
}

TELEGRAM_DOMAINS = {
    "t.me",
    "telegram.me",
    "telegram.org",
    "www.telegram.org",
}

SELF_HANDLE_PATTERNS = (
    r"(?i)@IntCyberDigest\b",
    r"(?i)International\s+Cyber\s+Digest",
)

URL_RE = re.compile(r"https?://[^\s<>()\[\]{}\"']+")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {"channel": CHANNEL, "last_seen_id": 0, "updated_at": None}
    return json.loads(STATE_PATH.read_text(encoding="utf-8"))


def save_state(last_seen_id: int) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "channel": CHANNEL,
        "last_seen_id": last_seen_id,
        "updated_at": now_iso(),
    }
    STATE_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def host_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def is_self_url(url: str) -> bool:
    host = host_of(url)
    if host in SELF_DOMAINS:
        return True

    lowered = url.lower()
    if host in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}:
        return "/intcyberdigest" in lowered

    if host in TELEGRAM_DOMAINS:
        return True

    return False


def is_external_source(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return False
    return bool(parsed.hostname) and not is_self_url(url)


def clean_text(text: str) -> str:
    cleaned = text

    # Remove self URLs while preserving third-party URLs.
    for url in URL_RE.findall(cleaned):
        if is_self_url(url):
            cleaned = cleaned.replace(url, "")

    # Remove direct channel/brand mentions from the publish-ready text.
    for pattern in SELF_HANDLE_PATTERNS:
        cleaned = re.sub(pattern, "", cleaned)

    # Remove common self-promotional lines if they become standalone noise.
    lines: list[str] = []
    for raw_line in cleaned.splitlines():
        line = raw_line.strip()
        if not line:
            if lines and lines[-1] != "":
                lines.append("")
            continue

        lowered = line.lower()
        promotional = (
            ("follow" in lowered and ("telegram" in lowered or "twitter" in lowered or "x.com" in lowered))
            or lowered in {"read more", "read more:"}
        )
        if promotional:
            continue

        lines.append(line)

    cleaned = "\n".join(lines)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def extract_photo_url(style: str | None) -> str | None:
    if not style:
        return None
    match = re.search(
        r"background-image\s*:\s*url\(['\"]?(.*?)['\"]?\)",
        style,
    )
    return match.group(1) if match else None


def extract_image_urls(node) -> list[str]:
    """Extract only real photo media attached to the Telegram message."""
    urls: list[str] = []

    # Telegram's public preview uses this class for ordinary photos and for
    # each photo inside grouped albums. Restricting extraction to these nodes
    # avoids avatars, link-preview thumbnails and decorative backgrounds.
    for media_node in node.select(
        ".tgme_widget_message_photo_wrap, .tgme_widget_message_service_photo"
    ):
        style_url = extract_photo_url(str(media_node.get("style", "")))
        if style_url and style_url not in urls:
            urls.append(style_url)

        for image in media_node.select("img[src]"):
            image_url = str(image.get("src", "")).strip()
            if (
                image_url.startswith(("http://", "https://"))
                and image_url not in urls
            ):
                urls.append(image_url)

    return urls


def extract_video_media(node) -> tuple[list[str], list[str]]:
    """Extract direct Telegram video URLs and poster thumbnails."""
    video_urls: list[str] = []
    thumbnail_urls: list[str] = []

    for player in node.select(
        ".tgme_widget_message_video_player, .tgme_widget_message_video_wrap"
    ):
        for video in player.select("video[src]"):
            url = str(video.get("src", "")).strip().replace("&amp;", "&")
            if url.startswith(("http://", "https://")) and url not in video_urls:
                video_urls.append(url)

        thumb = player.select_one(".tgme_widget_message_video_thumb")
        if thumb:
            url = extract_photo_url(str(thumb.get("style", "")))
            if url and url not in thumbnail_urls:
                thumbnail_urls.append(url)

        for image in player.select("img[src]"):
            url = str(image.get("src", "")).strip().replace("&amp;", "&")
            if url.startswith(("http://", "https://")) and url not in thumbnail_urls:
                thumbnail_urls.append(url)

    return video_urls, thumbnail_urls


def classify_media(
    image_urls: list[str],
    has_video: bool,
    has_document: bool,
) -> str:
    if has_video and image_urls:
        return "mixed"
    if has_video:
        return "video"
    if image_urls:
        return "image"
    if has_document:
        return "document"
    return "text"


def fetch_page(before: int | None = None) -> str:
    url = PUBLIC_URL if before is None else f"{PUBLIC_URL}?before={before}"
    response = requests.get(url, headers=HEADERS, timeout=TIMEOUT_SECONDS)
    response.raise_for_status()

    if "tgme_widget_message" not in response.text:
        raise RuntimeError(f"Telegram returned no public messages for {url}")

    return response.text


def parse_messages(html: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    messages: list[dict[str, Any]] = []

    for node in soup.select(".tgme_widget_message[data-post]"):
        data_post = str(node.get("data-post", ""))
        try:
            post_id = int(data_post.rsplit("/", 1)[1])
        except (IndexError, ValueError):
            continue

        text_node = node.select_one(".tgme_widget_message_text")
        raw_text = text_node.get_text("\n", strip=True) if text_node else ""

        time_node = node.select_one("time")
        published_raw = time_node.get("datetime") if time_node else None
        # Never serialize a missing datetime as the truthy string ``"None"``.
        # Freshness policy must be able to distinguish a verified timestamp
        # from missing Telegram metadata.
        published_at = str(published_raw).strip() if published_raw else None

        reply_to_id = None
        reply_node = node.select_one(".tgme_widget_message_reply[href]")
        if reply_node:
            reply_href = str(reply_node.get("href", ""))
            match = re.search(r"/(\d+)(?:\?.*)?$", reply_href)
            if match:
                reply_to_id = int(match.group(1))

        all_links: list[str] = []
        external_links: list[str] = []

        # Scan the full message node, not only the text block, because Telegram
        # often puts the original-source URL inside a link-preview card.
        for anchor in node.select("a[href]"):
            href = str(anchor.get("href", "")).strip()
            if href.startswith(("http://", "https://")) and href not in all_links:
                all_links.append(href)
            if is_external_source(href) and href not in external_links:
                external_links.append(href)

        # Also capture bare URLs Telegram may not have wrapped as anchors.
        for bare_url in URL_RE.findall(raw_text):
            bare_url = bare_url.rstrip(".,;:!?)\"]'")
            if bare_url not in all_links:
                all_links.append(bare_url)
            if is_external_source(bare_url) and bare_url not in external_links:
                external_links.append(bare_url)

        image_urls = extract_image_urls(node)
        image_url = image_urls[0] if image_urls else None

        video_urls, video_thumbnail_urls = extract_video_media(node)
        has_video = bool(video_urls) or node.select_one(
            ".tgme_widget_message_video_player, "
            ".tgme_widget_message_video_wrap"
        ) is not None

        has_document = node.select_one(".tgme_widget_message_document") is not None
        telegram_post_url = f"https://t.me/{CHANNEL}/{post_id}"
        media_type = classify_media(image_urls, has_video, has_document)

        messages.append(
            {
                "telegram_id": post_id,
                "published_at": published_at,
                "reply_to_id": reply_to_id,
                "collected_at": now_iso(),
                "raw_text": raw_text,
                "clean_text": clean_text(raw_text),
                "all_links": all_links,
                "source_links": external_links,
                "has_image": bool(image_urls),
                "image_url": image_url,
                "image_urls": image_urls,
                "has_video": has_video,
                "video_url": video_urls[0] if video_urls else None,
                "video_urls": video_urls,
                "video_thumbnail_urls": video_thumbnail_urls,
                "has_document": has_document,
                "media_type": media_type,
                "telegram_post_url": telegram_post_url,
                "status": "collected",
            }
        )

    return messages


def collect_since(last_seen_id: int) -> list[dict[str, Any]]:
    collected: dict[int, dict[str, Any]] = {}
    before: int | None = None

    for _ in range(MAX_BACKFILL_PAGES):
        html = fetch_page(before=before)
        page_messages = parse_messages(html)

        if not page_messages:
            break

        page_messages.sort(key=lambda item: item["telegram_id"])
        min_id = page_messages[0]["telegram_id"]

        for message in page_messages:
            if message["telegram_id"] > last_seen_id:
                collected[message["telegram_id"]] = message

        # Once the page overlaps our known history, backfill is complete.
        if any(message["telegram_id"] <= last_seen_id for message in page_messages):
            break

        # Prevent loops if Telegram returns the same page.
        next_before = min_id
        if before == next_before:
            break
        before = next_before

    return [collected[key] for key in sorted(collected)]


def append_inbox(messages: list[dict[str, Any]]) -> None:
    if not messages:
        return

    INBOX_PATH.parent.mkdir(parents=True, exist_ok=True)
    with INBOX_PATH.open("a", encoding="utf-8") as handle:
        for message in messages:
            handle.write(json.dumps(message, ensure_ascii=False) + "\n")


def main() -> int:
    try:
        state = load_state()
        last_seen_id = int(state.get("last_seen_id", 0))

        new_messages = collect_since(last_seen_id)

        if not new_messages:
            print(
                json.dumps(
                    {
                        "channel": f"@{CHANNEL}",
                        "last_seen_id": last_seen_id,
                        "new_messages": 0,
                        "status": "up_to_date",
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0

        append_inbox(new_messages)
        newest_id = max(item["telegram_id"] for item in new_messages)
        save_state(newest_id)

        summary = {
            "channel": f"@{CHANNEL}",
            "previous_last_seen_id": last_seen_id,
            "new_last_seen_id": newest_id,
            "new_messages": len(new_messages),
            "ids": [item["telegram_id"] for item in new_messages],
            "items": [
                {
                    "telegram_id": item["telegram_id"],
                    "published_at": item["published_at"],
                    "reply_to_id": item.get("reply_to_id"),
                    "clean_text": item["clean_text"],
                    "source_links": item["source_links"],
                    "has_image": item["has_image"],
                    "has_video": item["has_video"],
                    "has_document": item["has_document"],
                    "media_type": item.get("media_type"),
                    "video_candidates": len(item.get("video_urls") or []),
                }
                for item in new_messages
            ],
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
