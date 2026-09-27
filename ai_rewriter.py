#!/usr/bin/env python3
"""
Generate a Facebook Arabic preview from the newest Telegram item using Gemini.

This script NEVER publishes to Facebook.
It only prints a structured preview for review.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import requests

from telegram_collector import fetch_page, parse_messages


ROOT = Path(__file__).resolve().parent
INBOX_PATH = ROOT / "data" / "inbox.jsonl"
PROMPT_PATH = ROOT / "prompts" / "facebook_ar.txt"

API_BASE = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_MODEL = "gemini-3-flash-preview"
TIMEOUT_SECONDS = 60
URL_RE = re.compile(r"https?://[^\\s<>()\\[\\]{}\\\"\']+")


def read_latest_item() -> dict[str, Any]:
    if INBOX_PATH.exists():
        lines = [
            line.strip()
            for line in INBOX_PATH.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        if lines:
            return json.loads(lines[-1])

    messages = parse_messages(fetch_page())
    if not messages:
        raise RuntimeError("No Telegram message is available for the AI preview.")
    return max(messages, key=lambda item: item["telegram_id"])


def normalize_item(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "telegram_id": item.get("telegram_id") or item.get("id"),
        "published_at": item.get("published_at"),
        "text": item.get("clean_text") or item.get("text") or item.get("raw_text") or "",
        "source_links": item.get("source_links") or [],
        "has_image": bool(item.get("has_image")),
        "has_video": bool(item.get("has_video")),
        "has_document": bool(item.get("has_document")),
    }


def extract_text(response: dict[str, Any]) -> str:
    candidates = response.get("candidates") or []
    if not candidates:
        raise RuntimeError(
            "Gemini returned no candidate: "
            + json.dumps(response, ensure_ascii=False)[:1000]
        )

    parts = candidates[0].get("content", {}).get("parts", [])
    chunks = [part.get("text", "") for part in parts if part.get("text")]
    if not chunks:
        raise RuntimeError("Gemini returned a candidate without text.")

    return "\n".join(chunks).strip()


def parse_json_response(text: str) -> dict[str, Any]:
    cleaned = text.strip()

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Gemini response was not valid JSON: {cleaned[:1200]}") from exc

    required = {"title", "facebook_post", "first_comment", "language", "source_url"}
    missing = required.difference(data)
    if missing:
        raise RuntimeError(f"Gemini JSON is missing fields: {sorted(missing)}")

    return data


LATIN_RUN_RE = re.compile(r"(?<![\u2068\w])([A-Za-z][A-Za-z0-9._+/#:&()'’\-]*(?:\s+[A-Za-z0-9][A-Za-z0-9._+/#:&()'’\-]*){0,3})(?![\w\u2069])")


def isolate_latin_runs_rtl(text: str) -> str:
    """Keep Latin technical terms visually stable inside Arabic RTL text."""
    if not text:
        return ""

    # Do not touch URLs; they stay in comments, but protect them defensively.
    placeholders: dict[str, str] = {}

    def stash_url(match: re.Match[str]) -> str:
        key = f"__URL_{len(placeholders)}__"
        placeholders[key] = match.group(0)
        return key

    protected = URL_RE.sub(stash_url, text)

    def wrap(match: re.Match[str]) -> str:
        value = match.group(1)
        if not value.strip():
            return value
        return "\u2068" + value + "\u2069"

    protected = LATIN_RUN_RE.sub(wrap, protected)

    for key, value in placeholders.items():
        protected = protected.replace(key, value)

    return protected


def enforce_source_policy(result: dict[str, Any], allowed_links: list[str]) -> dict[str, Any]:
    allowed = [str(url) for url in allowed_links if url]
    source_url = str(result.get("source_url") or "").strip()

    if source_url not in allowed:
        source_url = ""

    result["source_url"] = source_url
    result["first_comment"] = f"المصدر: {source_url}" if source_url else ""

    forbidden_fragments = (
        "internationalcyberdigest.com",
        "t.me/intcyberdigest",
        "x.com/intcyberdigest",
        "twitter.com/intcyberdigest",
        "@intcyberdigest",
        "international cyber digest",
    )

    for field in ("title", "facebook_post"):
        value = str(result.get(field) or "")
        for fragment in forbidden_fragments:
            value = re.sub(re.escape(fragment), "", value, flags=re.I)
        value = re.sub(r"\s{2,}", " ", value).strip()
        if field in ("title", "facebook_post"):
            value = isolate_latin_runs_rtl(value)
        result[field] = value

    return result


def call_gemini(item: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not available.")

    model = os.environ.get("GEMINI_MODEL", DEFAULT_MODEL).strip()
    instructions = PROMPT_PATH.read_text(encoding="utf-8")

    user_payload = {
        "telegram_id": item["telegram_id"],
        "published_at": item["published_at"],
        "text": item["text"],
        "source_links": item["source_links"],
        "media": {
            "has_image": item["has_image"],
            "has_video": item["has_video"],
            "has_document": item["has_document"],
        },
    }

    body = {
        "systemInstruction": {
            "parts": [{"text": instructions}]
        },
        "contents": [
            {
                "role": "user",
                "parts": [
                    {
                        "text": (
                            "أعد صياغة هذا العنصر حسب التعليمات.\n\n"
                            + json.dumps(user_payload, ensure_ascii=False, indent=2)
                        )
                    }
                ],
            }
        ],
        "generationConfig": {
            "temperature": 0.35,
            "responseMimeType": "application/json",
        },
    }

    response = requests.post(
        f"{API_BASE}/models/{model}:generateContent",
        headers={
            "x-goog-api-key": api_key,
            "Content-Type": "application/json",
        },
        json=body,
        timeout=TIMEOUT_SECONDS,
    )

    if not response.ok:
        safe_body = response.text[:2000]
        raise RuntimeError(
            f"Gemini API failed with HTTP {response.status_code}: {safe_body}"
        )

    raw = response.json()
    generated = parse_json_response(extract_text(raw))
    return model, enforce_source_policy(generated, item["source_links"])


def main() -> int:
    try:
        item = normalize_item(read_latest_item())
        model, result = call_gemini(item)

        preview = {
            "status": "preview_only_not_published",
            "model": model,
            "input": item,
            "output": result,
        }

        print(json.dumps(preview, ensure_ascii=False, indent=2))
        return 0

    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
