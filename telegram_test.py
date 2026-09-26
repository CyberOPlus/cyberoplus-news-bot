#!/usr/bin/env python3
"""
Smoke test for reading the latest public post from the Telegram channel
@IntCyberDigest.

No Telegram token, login, AI key, or Facebook credential is required.
This script only reads Telegram's public web preview and prints structured data.
"""

from __future__ import annotations

import json
import re
import sys
from typing import Any
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup


CHANNEL = "IntCyberDigest"
PUBLIC_URL = f"https://t.me/s/{CHANNEL}"
TIMEOUT_SECONDS = 20

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/154.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def is_external_source(url: str) -> bool:
    """Keep real external links and ignore Telegram-internal navigation."""
    if not url:
        return False

    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()

    if parsed.scheme not in {"http", "https"}:
        return False

    telegram_hosts = {
        "t.me",
        "telegram.me",
        "telegram.org",
        "www.telegram.org",
    }
    return host not in telegram_hosts


def extract_photo_url(style: str | None) -> str | None:
    """Extract a background-image URL used by Telegram's public preview."""
    if not style:
        return None

    match = re.search(r"background-image\s*:\s*url\(['\"]?(.*?)['\"]?\)", style)
    return match.group(1) if match else None


def fetch_public_page() -> str:
    response = requests.get(
        PUBLIC_URL,
        headers=HEADERS,
        timeout=TIMEOUT_SECONDS,
    )
    response.raise_for_status()

    if "tgme_widget_message" not in response.text:
        raise RuntimeError(
            "Telegram responded, but no public messages were found in the page."
        )

    return response.text


def parse_messages(html: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    messages: list[dict[str, Any]] = []

    for node in soup.select(".tgme_widget_message[data-post]"):
        data_post = node.get("data-post", "")
        try:
            post_id = int(str(data_post).rsplit("/", 1)[1])
        except (IndexError, ValueError):
            continue

        text_node = node.select_one(".tgme_widget_message_text")
        text = (
            text_node.get_text("\n", strip=True)
            if text_node
            else ""
        )

        time_node = node.select_one("time")
        published_at = time_node.get("datetime") if time_node else None

        links: list[str] = []
        if text_node:
            for anchor in text_node.select("a[href]"):
                href = str(anchor.get("href", "")).strip()
                if is_external_source(href) and href not in links:
                    links.append(href)

        photo_node = node.select_one(".tgme_widget_message_photo_wrap")
        photo_url = (
            extract_photo_url(photo_node.get("style"))
            if photo_node
            else None
        )

        has_video = node.select_one(
            ".tgme_widget_message_video_player, "
            ".tgme_widget_message_video_wrap"
        ) is not None

        has_document = node.select_one(
            ".tgme_widget_message_document"
        ) is not None

        messages.append(
            {
                "id": post_id,
                "published_at": published_at,
                "text": text,
                "source_links": links,
                "has_image": photo_url is not None,
                "image_url": photo_url,
                "has_video": has_video,
                "has_document": has_document,
            }
        )

    return messages


def main() -> int:
    try:
        html = fetch_public_page()
        messages = parse_messages(html)

        if not messages:
            raise RuntimeError("No Telegram posts could be parsed.")

        latest = max(messages, key=lambda item: item["id"])

        result = {
            "channel": f"@{CHANNEL}",
            "fetched_from": PUBLIC_URL,
            "messages_found": len(messages),
            "latest": latest,
        }

        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
