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


SOURCE_NOTE_RE = re.compile(
    r"(?im)^\s*(?:source|via|credit|credits)\s*:\s*(.+?)\s*$"
)


def split_source_note(text: str) -> tuple[str, str]:
    """Move plain-text attribution lines out of the Facebook body."""
    if not text:
        return "", ""
    notes: list[str] = []

    def take(match: re.Match[str]) -> str:
        value = match.group(1).strip()
        if value and value not in notes:
            notes.append(value)
        return ""

    cleaned = SOURCE_NOTE_RE.sub(take, text)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, " | ".join(notes)


def normalize_item(item: dict[str, Any]) -> dict[str, Any]:
    original_text = item.get("clean_text") or item.get("text") or item.get("raw_text") or ""
    text, source_note = split_source_note(str(original_text))
    return {
        "telegram_id": item.get("telegram_id") or item.get("id"),
        "published_at": item.get("published_at"),
        "text": text,
        "source_note": source_note,
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


def enforce_source_policy(
    result: dict[str, Any],
    allowed_links: list[str],
    source_note: str = "",
) -> dict[str, Any]:
    allowed = [str(url) for url in allowed_links if url]
    source_url = str(result.get("source_url") or "").strip()

    if source_url not in allowed:
        source_url = ""

    result["source_url"] = source_url
    note = str(source_note or "").strip()
    if note and source_url:
        result["first_comment"] = f"المصدر: {note}\n{source_url}"
    elif note:
        result["first_comment"] = f"المصدر: {note}"
    elif source_url:
        result["first_comment"] = f"المصدر: {source_url}"
    else:
        result["first_comment"] = ""

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


def _prompt_payload(item: dict[str, Any]) -> tuple[str, str]:
    instructions = PROMPT_PATH.read_text(encoding="utf-8")
    user_payload = {
        "telegram_id": item["telegram_id"],
        "published_at": item["published_at"],
        "text": item["text"],
        "source_links": item["source_links"],
        "source_note": item.get("source_note", ""),
        "media": {
            "has_image": item["has_image"],
            "has_video": item["has_video"],
            "has_document": item["has_document"],
        },
    }
    user_text = (
        "نفّذ التعليمات على هذا العنصر وأرجع JSON صالح فقط.\n\n"
        + json.dumps(user_payload, ensure_ascii=False, indent=2)
    )
    return instructions, user_text


def _call_gemini_provider(item: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not available.")

    model = os.environ.get("GEMINI_MODEL", DEFAULT_MODEL).strip()
    instructions, user_text = _prompt_payload(item)

    body = {
        "systemInstruction": {"parts": [{"text": instructions}]},
        "contents": [{"role": "user", "parts": [{"text": user_text}]}],
        "generationConfig": {
            "temperature": 0.2,
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
        raise RuntimeError(
            f"Gemini HTTP {response.status_code}: {response.text[:800]}"
        )

    generated = parse_json_response(extract_text(response.json()))
    return f"gemini/{model}", enforce_source_policy(
        generated,
        item["source_links"],
        item.get("source_note", ""),
    )


def _call_openai_compatible(
    *,
    provider: str,
    endpoint: str,
    api_key: str,
    model: str,
    item: dict[str, Any],
    extra_headers: dict[str, str] | None = None,
) -> tuple[str, dict[str, Any]]:
    instructions, user_text = _prompt_payload(item)

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    if extra_headers:
        headers.update(extra_headers)

    payload = {
        "model": model,
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content": instructions},
            {"role": "user", "content": user_text},
        ],
    }

    response = requests.post(
        endpoint,
        headers=headers,
        json=payload,
        timeout=TIMEOUT_SECONDS,
    )
    if not response.ok:
        raise RuntimeError(
            f"{provider} HTTP {response.status_code}: {response.text[:800]}"
        )

    data = response.json()
    try:
        text = data["choices"][0]["message"]["content"]
    except Exception as exc:
        raise RuntimeError(f"{provider} returned an unexpected response.") from exc

    generated = parse_json_response(str(text))
    return f"{provider}/{model}", enforce_source_policy(
        generated,
        item["source_links"],
        item.get("source_note", ""),
    )


def _call_groq_provider(item: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    api_key = os.environ.get("GROQ_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GROQ_API_KEY is not available.")

    model = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile").strip()
    return _call_openai_compatible(
        provider="groq",
        endpoint="https://api.groq.com/openai/v1/chat/completions",
        api_key=api_key,
        model=model,
        item=item,
    )


def _call_openrouter_provider(item: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not available.")

    model = os.environ.get(
        "OPENROUTER_MODEL",
        "meta-llama/llama-3.3-70b-instruct:free",
    ).strip()

    return _call_openai_compatible(
        provider="openrouter",
        endpoint="https://openrouter.ai/api/v1/chat/completions",
        api_key=api_key,
        model=model,
        item=item,
        extra_headers={
            "HTTP-Referer": "https://www.cyberoplus.com/",
            "X-Title": "Cybero Plus News Bot",
        },
    )


def call_gemini(item: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Compatibility entry point used by the batch processor.

    Tries each configured AI provider in order. Missing keys are skipped.
    If a provider is rate-limited or temporarily fails, the next one is tried.
    Nothing is lost when all providers fail: the batch processor keeps the
    Telegram item unprepared so a later workflow run can retry it.
    """

    providers = [
        _call_gemini_provider,
        _call_groq_provider,
        _call_openrouter_provider,
    ]

    errors: list[str] = []
    configured = 0

    for provider in providers:
        try:
            if provider is _call_gemini_provider and os.environ.get("GEMINI_API_KEY", "").strip():
                configured += 1
            elif provider is _call_groq_provider and os.environ.get("GROQ_API_KEY", "").strip():
                configured += 1
            elif provider is _call_openrouter_provider and os.environ.get("OPENROUTER_API_KEY", "").strip():
                configured += 1
            else:
                continue

            return provider(item)
        except Exception as exc:
            errors.append(f"{provider.__name__}: {exc}")
            print(
                f"WARNING: AI provider failed, trying fallback: {provider.__name__}: {exc}",
                file=sys.stderr,
            )

    if configured == 0:
        raise RuntimeError("No AI provider API key is configured.")

    raise RuntimeError(
        "All configured AI providers failed: " + " | ".join(errors)
    )


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
