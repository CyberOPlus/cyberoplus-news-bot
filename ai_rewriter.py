#!/usr/bin/env python3
"""Prepare a faithful Moroccan-Darija Facebook post using resilient free AI fallbacks.

The public entry points are intentionally kept stable because ai_batch_processor.py
imports normalize_item() and call_gemini().

Provider order:
1) Gemini free-tier models
2) Groq free-plan models
3) OpenRouter free router

The first successful, valid structured result wins. Temporary rate limits and
provider outages never delete a Telegram item; the batch processor retries later.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

import requests

from telegram_collector import fetch_page, parse_messages


ROOT = Path(__file__).resolve().parent
INBOX_PATH = ROOT / "data" / "inbox.jsonl"
PROMPT_PATH = ROOT / "prompts" / "facebook_ar.txt"

GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"
GROQ_ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"
OPENROUTER_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"

DEFAULT_GEMINI_MODELS = (
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.1-flash-lite",
)
DEFAULT_GROQ_MODELS = (
    "qwen/qwen3.8-27b",
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
)
DEFAULT_OPENROUTER_MODELS = ("openrouter/free",)

TIMEOUT_SECONDS = 60
MAX_HTTP_ATTEMPTS = 2
TRANSIENT_HTTP_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}

URL_RE = re.compile(r"https?://[^\\s<>()\\[\\]{}\\\"']+")
SOURCE_NOTE_RE = re.compile(
    r"(?im)^\\s*(?:source|via|credit|credits)\\s*:\\s*(.+?)\\s*$"
)
TITLE_SIGNAL_RE = re.compile(
    r"(?im)^\\s*(?:‼️\\s*)?(?:BREAKING|ALERT|URGENT|EXCLUSIVE)\\s*:"
)
LATIN_RUN_RE = re.compile(
    r"(?<![\\u2068\\w])"
    r"([A-Za-z][A-Za-z0-9._+/#:&()'’\\-]*"
    r"(?:\\s+[A-Za-z0-9][A-Za-z0-9._+/#:&()'’\\-]*){0,3})"
    r"(?![\\w\\u2069])"
)

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "facebook_post": {"type": "string"},
        "first_comment": {"type": "string"},
        "language": {"type": "string"},
        "source_url": {"type": "string"},
    },
    "required": [
        "title",
        "facebook_post",
        "first_comment",
        "language",
        "source_url",
    ],
    "additionalProperties": False,
}


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


def split_source_note(text: str) -> tuple[str, str]:
    """Move Source:/Via:/Credit: lines out of the Facebook body."""
    if not text:
        return "", ""

    notes: list[str] = []

    def take(match: re.Match[str]) -> str:
        value = match.group(1).strip()
        if value and value not in notes:
            notes.append(value)
        return ""

    cleaned = SOURCE_NOTE_RE.sub(take, text)
    cleaned = re.sub(r"\\n{3,}", "\\n\\n", cleaned).strip()
    return cleaned, " | ".join(notes)


def normalize_item(item: dict[str, Any]) -> dict[str, Any]:
    original_text = (
        item.get("clean_text")
        or item.get("text")
        or item.get("raw_text")
        or ""
    )
    text, source_note = split_source_note(str(original_text))

    return {
        "telegram_id": item.get("telegram_id") or item.get("id"),
        "published_at": item.get("published_at"),
        "text": text,
        "source_note": source_note,
        "has_explicit_title": bool(TITLE_SIGNAL_RE.search(text)),
        "source_links": item.get("source_links") or [],
        "has_image": bool(item.get("has_image")),
        "has_video": bool(item.get("has_video")),
        "has_document": bool(item.get("has_document")),
    }


def _model_list(
    plural_env: str,
    singular_env: str,
    defaults: tuple[str, ...],
) -> list[str]:
    """Read comma-separated model fallbacks while remaining backward-compatible."""
    plural = os.environ.get(plural_env, "").strip()
    singular = os.environ.get(singular_env, "").strip()

    raw: list[str] = []
    if plural:
        raw.extend(part.strip() for part in plural.split(","))
    elif singular:
        raw.append(singular)

    raw.extend(defaults)

    models: list[str] = []
    for model in raw:
        if model and model not in models:
            models.append(model)
    return models


def _retry_delay(response: requests.Response, attempt: int) -> float:
    value = response.headers.get("retry-after", "").strip()
    if value:
        try:
            return min(max(float(value), 0.5), 8.0)
        except ValueError:
            pass
    return min(2.0 ** attempt, 6.0)


def _post_json(
    url: str,
    *,
    headers: dict[str, str],
    payload: dict[str, Any],
    provider_label: str,
) -> dict[str, Any]:
    """POST JSON with a short retry for transient provider failures."""
    last_status = 0
    last_body = ""

    for attempt in range(MAX_HTTP_ATTEMPTS):
        try:
            response = requests.post(
                url,
                headers=headers,
                json=payload,
                timeout=TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            if attempt + 1 < MAX_HTTP_ATTEMPTS:
                time.sleep(min(2.0 ** attempt, 4.0))
                continue
            raise RuntimeError(f"{provider_label} network error: {exc}") from exc

        last_status = response.status_code
        last_body = response.text[:700]

        if response.ok:
            try:
                return response.json()
            except ValueError as exc:
                raise RuntimeError(
                    f"{provider_label} returned non-JSON HTTP response."
                ) from exc

        if (
            response.status_code in TRANSIENT_HTTP_STATUS
            and attempt + 1 < MAX_HTTP_ATTEMPTS
        ):
            time.sleep(_retry_delay(response, attempt))
            continue

        break

    raise RuntimeError(
        f"{provider_label} HTTP {last_status}: {last_body}"
    )


def _extract_gemini_text(response: dict[str, Any]) -> str:
    candidates = response.get("candidates") or []
    if not candidates:
        raise RuntimeError(
            "Gemini returned no candidate: "
            + json.dumps(response, ensure_ascii=False)[:900]
        )

    parts = candidates[0].get("content", {}).get("parts", [])
    chunks = [str(part.get("text") or "") for part in parts if part.get("text")]
    if not chunks:
        raise RuntimeError("Gemini returned a candidate without text.")

    return "\\n".join(chunks).strip()


def parse_json_response(text: str) -> dict[str, Any]:
    """Accept strict JSON and defensively recover JSON from code fences."""
    cleaned = str(text or "").strip()

    if cleaned.startswith("```"):
        cleaned = re.sub(r"^\`\`\`(?:json)?\\s*", "", cleaned, flags=re.I)
        cleaned = re.sub(r"\\s*\`\`\`$", "", cleaned)

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start < 0 or end <= start:
            raise RuntimeError(
                f"AI response was not valid JSON: {cleaned[:1000]}"
            )
        try:
            data = json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"AI response was not valid JSON: {cleaned[:1000]}"
            ) from exc

    if not isinstance(data, dict):
        raise RuntimeError("AI response JSON must be an object.")

    required = set(OUTPUT_SCHEMA["required"])
    missing = required.difference(data)
    if missing:
        raise RuntimeError(f"AI JSON is missing fields: {sorted(missing)}")

    for key in required:
        if not isinstance(data.get(key), str):
            data[key] = str(data.get(key) or "")

    return data


def isolate_latin_runs_rtl(text: str) -> str:
    """Keep Latin technical terms visually stable inside Arabic RTL text."""
    if not text:
        return ""

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


def _canonical_source_link(allowed_links: list[str]) -> str:
    """The model never gets to invent or rewrite the source URL."""
    for url in allowed_links:
        value = str(url or "").strip()
        if value.startswith(("http://", "https://")):
            return value
    return ""


def enforce_source_policy(
    result: dict[str, Any],
    allowed_links: list[str],
    source_note: str = "",
    has_explicit_title: bool = False,
) -> dict[str, Any]:
    source_url = _canonical_source_link(allowed_links)
    note = str(source_note or "").strip()

    result["source_url"] = source_url
    if note and source_url:
        result["first_comment"] = f"المصدر: {note}\\n{source_url}"
    elif note:
        result["first_comment"] = f"المصدر: {note}"
    elif source_url:
        result["first_comment"] = f"المصدر: {source_url}"
    else:
        result["first_comment"] = ""

    if not has_explicit_title:
        result["title"] = ""

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

        # Original source URLs belong in the first comment, never in the body.
        for url in allowed_links:
            if url:
                value = value.replace(str(url), "")

        value = re.sub(r"[ \\t]{2,}", " ", value)
        value = re.sub(r"\\n{3,}", "\\n\\n", value).strip()
        result[field] = isolate_latin_runs_rtl(value)

    result["language"] = "ary"
    return result


def _prompt_payload(item: dict[str, Any]) -> tuple[str, str]:
    instructions = PROMPT_PATH.read_text(encoding="utf-8")
    user_payload = {
        "telegram_id": item["telegram_id"],
        "published_at": item["published_at"],
        "text": item["text"],
        "source_links": item["source_links"],
        "source_note": item.get("source_note", ""),
        "has_explicit_title": bool(item.get("has_explicit_title")),
        "media": {
            "has_image": item["has_image"],
            "has_video": item["has_video"],
            "has_document": item["has_document"],
        },
    }
    user_text = (
        "طبق التعليمات على هاد المنشور ورجع JSON صالح فقط.\\n\\n"
        + json.dumps(user_payload, ensure_ascii=False, indent=2)
    )
    return instructions, user_text


def _finalize(
    generated: dict[str, Any],
    item: dict[str, Any],
) -> dict[str, Any]:
    return enforce_source_policy(
        generated,
        item["source_links"],
        item.get("source_note", ""),
        bool(item.get("has_explicit_title")),
    )


def _call_one_gemini(
    item: dict[str, Any],
    api_key: str,
    model: str,
) -> tuple[str, dict[str, Any]]:
    instructions, user_text = _prompt_payload(item)

    body = {
        "systemInstruction": {"parts": [{"text": instructions}]},
        "contents": [{"role": "user", "parts": [{"text": user_text}]}],
        "generationConfig": {
            "temperature": 0.15,
            "maxOutputTokens": 2048,
            "responseMimeType": "application/json",
            "thinkingConfig": {"thinkingLevel": "low"},
        },
    }

    response = _post_json(
        f"{GEMINI_API_BASE}/models/{model}:generateContent",
        headers={
            "x-goog-api-key": api_key,
            "Content-Type": "application/json",
        },
        payload=body,
        provider_label=f"gemini/{model}",
    )

    generated = parse_json_response(_extract_gemini_text(response))
    return f"gemini/{model}", _finalize(generated, item)


def _call_one_openai_compatible(
    *,
    provider: str,
    endpoint: str,
    api_key: str,
    model: str,
    item: dict[str, Any],
    extra_headers: dict[str, str] | None = None,
    strict_schema: bool = False,
    reasoning_effort: str | None = None,
) -> tuple[str, dict[str, Any]]:
    instructions, user_text = _prompt_payload(item)

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    if extra_headers:
        headers.update(extra_headers)

    payload: dict[str, Any] = {
        "model": model,
        "temperature": 0.15,
        "messages": [
            {"role": "system", "content": instructions},
            {"role": "user", "content": user_text},
        ],
    }

    if strict_schema:
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": "cyberoplus_facebook_post",
                "strict": True,
                "schema": OUTPUT_SCHEMA,
            },
        }
    else:
        payload["response_format"] = {"type": "json_object"}

    if reasoning_effort:
        payload["reasoning_effort"] = reasoning_effort

    response = _post_json(
        endpoint,
        headers=headers,
        payload=payload,
        provider_label=f"{provider}/{model}",
    )

    try:
        text = response["choices"][0]["message"]["content"]
    except Exception as exc:
        raise RuntimeError(
            f"{provider}/{model} returned an unexpected response."
        ) from exc

    generated = parse_json_response(str(text))
    return f"{provider}/{model}", _finalize(generated, item)


def _provider_attempts(item: dict[str, Any]):
    gemini_key = os.environ.get("GEMINI_API_KEY", "").strip()
    groq_key = os.environ.get("GROQ_API_KEY", "").strip()
    openrouter_key = os.environ.get("OPENROUTER_API_KEY", "").strip()

    if gemini_key:
        for model in _model_list(
            "GEMINI_MODELS",
            "GEMINI_MODEL",
            DEFAULT_GEMINI_MODELS,
        ):
            yield (
                f"gemini/{model}",
                lambda model=model: _call_one_gemini(
                    item,
                    gemini_key,
                    model,
                ),
            )

    if groq_key:
        for model in _model_list(
            "GROQ_MODELS",
            "GROQ_MODEL",
            DEFAULT_GROQ_MODELS,
        ):
            # Translation does not need deep chain-of-thought.
            effort = "none" if model.startswith("qwen/") else "low"
            yield (
                f"groq/{model}",
                lambda model=model, effort=effort: _call_one_openai_compatible(
                    provider="groq",
                    endpoint=GROQ_ENDPOINT,
                    api_key=groq_key,
                    model=model,
                    item=item,
                    strict_schema=True,
                    reasoning_effort=effort,
                ),
            )

    if openrouter_key:
        for model in _model_list(
            "OPENROUTER_MODELS",
            "OPENROUTER_MODEL",
            DEFAULT_OPENROUTER_MODELS,
        ):
            yield (
                f"openrouter/{model}",
                lambda model=model: _call_one_openai_compatible(
                    provider="openrouter",
                    endpoint=OPENROUTER_ENDPOINT,
                    api_key=openrouter_key,
                    model=model,
                    item=item,
                    extra_headers={
                        "HTTP-Referer": "https://www.cyberoplus.com/",
                        "X-Title": "Cybero Plus News Bot",
                    },
                    strict_schema=False,
                ),
            )


def call_gemini(item: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Compatibility entry point: run the full resilient AI fallback chain."""
    attempts = list(_provider_attempts(item))
    if not attempts:
        raise RuntimeError("No AI provider API key is configured.")

    errors: list[str] = []

    for label, call in attempts:
        try:
            model, result = call()
            print(
                json.dumps(
                    {
                        "ai_provider_selected": model,
                        "fallback_failures_before_success": len(errors),
                    },
                    ensure_ascii=False,
                )
            )
            return model, result
        except Exception as exc:
            errors.append(f"{label}: {exc}")
            print(
                f"WARNING: AI attempt failed, trying next model: {label}: {exc}",
                file=sys.stderr,
            )

    raise RuntimeError(
        "All configured AI models failed: " + " | ".join(errors)
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
