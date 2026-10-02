#!/usr/bin/env python3
"""Prepare a faithful Modern Standard Arabic Facebook post using resilient free AI fallbacks.

The public entry points are intentionally kept stable because ai_batch_processor.py
imports normalize_item() and call_gemini().

Provider order:
1) Groq free-plan models (fast path)
2) Gemini free-tier models
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
from urllib.parse import urlparse

import requests

from telegram_collector import fetch_page, parse_messages


ROOT = Path(__file__).resolve().parent
INBOX_PATH = ROOT / "data" / "inbox.jsonl"
PROMPT_PATH = ROOT / "prompts" / "facebook_ar.txt"

GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"
GROQ_ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"
OPENROUTER_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"

DEFAULT_GEMINI_MODELS = (
    "gemini-3.7-flash",
    "gemini-3.5-flash",
)
DEFAULT_GROQ_MODELS = (
    "openai/gpt-oss-120b",
    "qwen/qwen3.8-27b",
    "openai/gpt-oss-20b",
)
DEFAULT_OPENROUTER_MODELS = ("openrouter/free",)

TIMEOUT_SECONDS = max(5, int(os.environ.get("AI_HTTP_TIMEOUT_SECONDS", "20")))
MAX_HTTP_ATTEMPTS = max(1, int(os.environ.get("AI_HTTP_ATTEMPTS", "1")))
TRANSIENT_HTTP_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}

URL_RE = re.compile(r"https?://[^\s<>()\[\]{}\"']+")
SOURCE_NOTE_RE = re.compile(
    r"(?im)^\s*(?:source|via|credit|credits)\s*:\s*(.+?)\s*$"
)
TITLE_SIGNAL_RE = re.compile(
    r"(?im)^\s*(?:‼️\s*)?(?:BREAKING|ALERT|URGENT|EXCLUSIVE)\s*:"
)
PROMO_RE = re.compile(
    r"(?i)(?:\s*(?:stay tuned(?: for (?:more|updates?|more info(?:rmation)?))?|"
    r"follow us(?: for (?:more|updates?))?|more updates soon)\.?\s*)$"
)
LATIN_RUN_RE = re.compile(
    r"(?<![\u2068\w])"
    r"([A-Za-z][A-Za-z0-9._+/#:&'’\-]*"
    r"(?:\s+[A-Za-z0-9][A-Za-z0-9._+/#:&'’\-]*){0,3})"
    r"(?![\w\u2069])"
)
LATIN_PAREN_RE = re.compile(
    r"\(\s*((?=[^()\n]*[A-Za-z])[^()\n\u0600-\u06FF]*?)\s*\)"
)

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "content_type": {
            "type": "string",
            "enum": [
                "breaking_news", "security_alert", "vulnerability", "incident",
                "tool", "product_update", "report", "update", "general"
            ],
        },
        "attention_label": {
            "type": "string",
            "enum": ["breaking", "warning", "important", "none"],
        },
        "attention_evidence": {"type": "string"},
        "certainty": {
            "type": "string",
            "enum": ["confirmed", "attributed", "reported", "uncertain"],
        },
        "main_fact": {"type": "string"},
        "supporting_facts": {"type": "array", "items": {"type": "string"}},
        "protected_entities": {"type": "array", "items": {"type": "string"}},
        "protected_numbers": {"type": "array", "items": {"type": "string"}},
        "title": {"type": "string"},
        "facebook_post": {"type": "string"},
        "first_comment": {"type": "string"},
        "language": {"type": "string"},
        "source_url": {"type": "string"},
        "card_title": {"type": "string"},
        "link_role": {
            "type": "string",
            "enum": ["source", "tool", "download", "project", "more_info", "none"]
        },
    },
    "required": [
        "content_type",
        "attention_label",
        "attention_evidence",
        "certainty",
        "main_fact",
        "supporting_facts",
        "protected_entities",
        "protected_numbers",
        "title",
        "facebook_post",
        "first_comment",
        "language",
        "source_url",
        "card_title",
        "link_role",
    ],
    "additionalProperties": False,
}

CONTENT_TYPES = {
    "breaking_news", "security_alert", "vulnerability", "incident",
    "tool", "product_update", "report", "update", "general",
}
ATTENTION_LABELS = {"breaking", "warning", "important", "none"}
CERTAINTY_LEVELS = {"confirmed", "attributed", "reported", "uncertain"}

EXPLICIT_BREAKING_RE = re.compile(
    r"(?i)(?:^|\n)\s*(?:[🚨‼️❗]+\s*)?(?:BREAKING|URGENT|عاجل)\b"
)
EXPLICIT_WARNING_RE = re.compile(
    r"(?i)(?:^|\n)\s*(?:[⚠️‼️❗]+\s*)?(?:ALERT|WARNING|WARN|تنبيه|تحذير)\b"
)
EXPLICIT_IMPORTANT_RE = re.compile(
    r"(?i)(?:^|\n)\s*(?:[❗‼️]+\s*)?(?:IMPORTANT|مهم)\b"
)
RISK_SIGNAL_RE = re.compile(
    r"(?i)\b(?:zero[- ]day|0day|exploit(?:ed|ation|ing)?|in the wild|"
    r"malware|ransomware|phishing|scam|credential(?:s)?|breach|"
    r"compromis(?:e|ed)|attack(?:s|ed|ing)?|vulnerabilit(?:y|ies)|"
    r"data leak|stolen|steal(?:ing)?|CVE-\d{4}-\d{4,7})\b|"
    r"(?:ثغرة|اختراق|هجوم|برمجية خبيثة|تصيد|احتيال|تسريب بيانات|سرقة بيانات)"
)
ATTRIBUTION_SIGNAL_RE = re.compile(
    r"(?i)\b(?:reportedly|according to (?:sources|reports)|sources say|"
    r"claims?|allegedly|بحسب مصادر|حسب مصادر|وفقاً لتقارير|وفقا لتقارير)\b"
)
UNCERTAIN_SIGNAL_RE = re.compile(
    r"(?i)\b(?:unconfirmed|suspected|possibly|may have|might have|"
    r"غير مؤكد|مشتبه|ربما)\b"
)
CVE_RE = re.compile(r"(?i)\bCVE-\d{4}-\d{4,7}\b")
NUMBER_RE = re.compile(r"(?<![\w])\d[\d.,:/-]*%?(?![\w])")
ATTENTION_PREFIX_RE = re.compile(
    r"(?i)^\s*(?:[🚨⚠️❗‼️]+\s*)?"
    r"(?:عاجل|تحذير(?: أمني)?|تنبيه(?: أمني)?|مهم|BREAKING|URGENT|ALERT|WARNING)"
    r"\s*[:：\-–—]?\s*"
)
DISALLOWED_TECH_TRANSLITERATION_RE = re.compile(
    r"\bج[اأإآايكلر\s]{0,7}بريك\b",
    flags=re.I,
)
TITLE_JARGON_RE = re.compile(
    r"(?i)\b(?:firmware|kernel|webkit|use-after-free|homebrew|jailbreak)\b"
)
GENERIC_EDITORIAL_CONCLUSION_RE = re.compile(
    r"(?:هذه التقنية قد|هذا التطور\s+(?:قد\s+)?يفتح\s+باب(?:اً|ا)?|"
    r"قد تفتح الباب|يفتح الباب أمام|يفتح\s+باب(?:اً|ا)?\s+ل|يمهد الطريق|"
    r"يثير تساؤلات|يشكل خطوة مهمة|يمثل خطوة مهمة|"
    r"هذا\s+يشير\s+إلى\s+خطر|يشير\s+ذلك\s+إلى\s+خطر)"
)
GENERATED_ATTACK_CLAIM_RE = re.compile(r"(?:اختراق|هجوم|استهداف)")
SOURCE_ATTACK_EVENT_RE = re.compile(
    r"(?i)\b(?:hacked|hack(?:ed|ing)?|compromised|breached|attack(?:ed|ing|ers?)?|"
    r"targeted|intrusion(?:s)?|intruder(?:s)?|threat\s+actor(?:s)?|unauthorized\s+access)\b|"
    r"(?:اختراق|هجوم|استهداف|تسلل|مهاجم(?:ون|ين)?)"
)



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
    cleaned = PROMO_RE.sub("", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
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
        "reply_context": item.get("reply_context") or [],
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
            return min(max(float(value), 0.5), 30.0)
        except ValueError:
            pass
    return min(2.0 ** attempt, 15.0)


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

    return "\n".join(chunks).strip()


def parse_json_response(text: str) -> dict[str, Any]:
    """Accept strict JSON and normalize the structured editorial payload."""
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

    array_fields = {"supporting_facts", "protected_entities", "protected_numbers"}
    for key in required:
        if key in array_fields:
            value = data.get(key)
            if not isinstance(value, list):
                value = [value] if value not in (None, "") else []
            data[key] = [
                str(item).strip()
                for item in value
                if str(item or "").strip()
            ]
        elif not isinstance(data.get(key), str):
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

    # Models sometimes emit FSI/PDI themselves. Normalize first so we wrap once.
    normalized = (
        text.replace("\u2066", "")
        .replace("\u2067", "")
        .replace("\u2068", "")
        .replace("\u2069", "")
        .replace("\u200e", "")
        .replace("\u200f", "")
    )
    normalized = re.sub(
        r"(?<=[A-Za-z0-9])[\u2010\u2011\u2012\u2013\u2212](?=[A-Za-z0-9])",
        "-",
        normalized,
    )
    protected = URL_RE.sub(stash_url, normalized)

    def wrap(match: re.Match[str]) -> str:
        value = match.group(1)
        if not value.strip():
            return value
        return "\u2068" + value + "\u2069"

    protected = LATIN_RUN_RE.sub(wrap, protected)

    for key, value in placeholders.items():
        protected = protected.replace(key, value)

    return protected


def force_rtl_paragraphs(text: str) -> str:
    """Force every visible Facebook paragraph RTL while preserving Latin runs."""
    if not text:
        return ""
    isolated = isolate_latin_runs_rtl(text)
    lines: list[str] = []
    for line in isolated.split("\n"):
        value = line.strip()
        if not value:
            lines.append("")
            continue
        # RLI/PDI makes the whole paragraph Arabic-directional even when it
        # starts with an English product/company name. Nested FSI/PDI keeps
        # English/Latin terms readable left-to-right.
        lines.append("\u2067" + value + "\u2069")
    return "\n".join(lines)


def _canonical_source_link(allowed_links: list[str]) -> str:
    """The model never gets to invent or rewrite the source URL."""
    for url in allowed_links:
        value = str(url or "").strip()
        if value.startswith(("http://", "https://")):
            return value
    return ""


def _clean_source_text(text: str) -> str:
    value = URL_RE.sub("", str(text or ""))
    value = re.sub(r"\s+", " ", value).strip()
    return value


def _canonical_numeric_token(value: str) -> str:
    """Normalize harmless formatting differences without changing the fact value."""
    value = str(value or "").strip()
    if not value:
        return ""

    suffix = "%" if value.endswith("%") else ""
    core = value[:-1] if suffix else value
    core = core.rstrip(".,:/-")

    # Models may render English thousands separators differently, e.g.
    # source "1,000" -> Arabic prose "1000". Treat those as the same fact.
    core = re.sub(r"(?<=\d),(?=\d{3}(?:\D|$))", "", core)
    return core + suffix if core else ""


def _numeric_tokens(text: str) -> set[str]:
    clean = URL_RE.sub("", str(text or ""))
    tokens: set[str] = set()
    for match in NUMBER_RE.finditer(clean):
        value = _canonical_numeric_token(match.group(0))
        if value:
            tokens.add(value)
    return tokens


def _identifier_numeric_tokens(text: str) -> set[str]:
    """Extract numeric facts embedded in identifiers or compact units.

    Examples: TA419, GPT-4o and 110TB. These must be compared on both the
    source and generated sides; otherwise a correctly preserved identifier can
    look like an omitted number, while a translated compact unit can look like
    an invented number.
    """
    clean = URL_RE.sub("", str(text or ""))
    values: set[str] = set()

    for token in re.findall(r"\b[A-Za-z0-9][A-Za-z0-9._+/#:&()'’\\-]*\b", clean):
        if not any(ch.isalpha() for ch in token) or not any(ch.isdigit() for ch in token):
            continue
        for raw_value in re.findall(r"\d+(?:[.,]\d+)?", token):
            value = _canonical_numeric_token(raw_value)
            if value:
                values.add(value)

    return values


def _cve_tokens(text: str) -> set[str]:
    return {match.group(0).upper() for match in CVE_RE.finditer(str(text or ""))}


def _validate_fact_fidelity(source_text: str, facebook_post: str) -> None:
    """Reject obvious fabricated numeric/CVE facts before a post reaches the queue."""
    source_numbers = _numeric_tokens(source_text) | _identifier_numeric_tokens(source_text)
    output_numbers = _numeric_tokens(facebook_post) | _identifier_numeric_tokens(facebook_post)
    invented_numbers = sorted(output_numbers - source_numbers)
    if invented_numbers:
        raise RuntimeError(
            "AI introduced numeric facts not present in source: "
            + ", ".join(invented_numbers[:8])
        )
    missing_numbers = sorted(source_numbers - output_numbers)
    if missing_numbers:
        raise RuntimeError(
            "AI omitted numeric facts from source: "
            + ", ".join(missing_numbers[:12])
        )

    source_cves = _cve_tokens(source_text)
    output_cves = _cve_tokens(facebook_post)
    invented_cves = sorted(output_cves - source_cves)
    if invented_cves:
        raise RuntimeError(
            "AI introduced CVE identifiers not present in source: "
            + ", ".join(invented_cves)
        )
    missing_cves = sorted(source_cves - output_cves)
    if missing_cves:
        raise RuntimeError(
            "AI omitted CVE identifiers from source: "
            + ", ".join(missing_cves)
        )

    source_clean = _clean_source_text(source_text)
    output_clean = _clean_source_text(facebook_post)
    if len(source_clean) >= 220 and len(output_clean) < max(110, int(len(source_clean) * 0.38)):
        raise RuntimeError("AI output is too short and may have dropped material source facts.")
    if len(source_clean) >= 100 and len(output_clean) > max(900, int(len(source_clean) * 2.5)):
        raise RuntimeError("AI output is unusually long and may contain added material.")


def _strict_entity_tokens(entity: str) -> list[str]:
    """Return only identifiers that truly need verbatim preservation.

    Natural-language names, organisations, places and acronyms may be
    translated or transliterated correctly in Arabic (for example White House,
    Proofpoint, FBI or MFA). Blocking publication because those strings are not
    repeated in Latin script creates false fidelity failures.

    Keep strict matching for high-signal technical identifiers instead:
    tokens with digits (TA419, GPT-4o), mixed internal capitals (OneDrive,
    OpenAI/WebKit), or identifier punctuation tied to those signals.
    """
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9._+/#:&()'’\\-]*", str(entity or ""))
    strict: list[str] = []

    for token in tokens:
        letters = "".join(ch for ch in token if ch.isalpha())
        has_digit = any(ch.isdigit() for ch in token)
        has_internal_upper = (
            len(letters) >= 2
            and not letters.isupper()
            and any(ch.isupper() for ch in letters[1:])
        )
        has_name_symbol = (
            any(ch in "._+/#:&-" for ch in token)
            and (has_digit or has_internal_upper)
        )

        if has_digit or has_internal_upper or has_name_symbol:
            strict.append(token)

    return strict


def _validate_protected_entities(entities: list[str], facebook_post: str) -> None:
    """Protect real names/identifiers without rejecting valid Arabic translation."""
    clean_post = (
        str(facebook_post or "")
        .replace("\u2066", "")
        .replace("\u2067", "")
        .replace("\u2068", "")
        .replace("\u2069", "")
        .replace("\u200e", "")
        .replace("\u200f", "")
    )
    post_lower = clean_post.lower()
    missing: list[str] = []

    for raw in entities:
        entity = str(raw or "").strip()
        strict_tokens = _strict_entity_tokens(entity)
        if not strict_tokens:
            # Generic phrases such as "AI agents" or "Dutch police" may be
            # translated naturally into Arabic and are not immutable names.
            continue

        absent = [token for token in strict_tokens if token.lower() not in post_lower]
        if absent:
            missing.append(entity)

    if missing:
        raise RuntimeError(
            "AI changed or omitted protected entities: "
            + ", ".join(missing[:8])
        )


def _validated_certainty(source_text: str, requested: str) -> str:
    value = requested if requested in CERTAINTY_LEVELS else "confirmed"
    if UNCERTAIN_SIGNAL_RE.search(source_text):
        return "uncertain"
    if ATTRIBUTION_SIGNAL_RE.search(source_text) and value == "confirmed":
        return "attributed"
    return value


def _validated_attention(
    source_text: str,
    requested: str,
    evidence: str,
) -> tuple[str, str]:
    requested = requested if requested in ATTENTION_LABELS else "none"
    evidence = str(evidence or "").strip()

    if EXPLICIT_BREAKING_RE.search(source_text):
        return "breaking", "source_explicit_breaking"

    if EXPLICIT_WARNING_RE.search(source_text):
        return "warning", "source_explicit_warning"

    if EXPLICIT_IMPORTANT_RE.search(source_text) and requested == "none":
        return "important", "source_explicit_important"

    if requested == "breaking":
        # BREAKING is never inferred just because a story is important.
        requested = "warning" if RISK_SIGNAL_RE.search(source_text) else "none"

    if requested == "warning" and not RISK_SIGNAL_RE.search(source_text):
        requested = "none"

    if requested == "important" and not evidence:
        requested = "none"

    return requested, evidence if requested != "none" else ""


def _strip_attention_prefix(text: str) -> str:
    value = str(text or "").strip()
    for _ in range(2):
        cleaned = ATTENTION_PREFIX_RE.sub("", value, count=1).strip()
        if cleaned == value:
            break
        value = cleaned
    return value


def _clean_publisher_owned_tokens(text: str) -> str:
    """Keep emoji, hashtags, links and attention labels out of AI-owned visible copy."""
    value = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    value = URL_RE.sub("", value)
    value = re.sub(r"(?<![\w#])#[\w\u0600-\u06FF_]+", "", value)
    value = re.sub(r"[🚨⚠️❗‼️🔐🛡️🤖🆕🔥]", "", value)
    lines = []
    for raw in value.split("\n"):
        line = _strip_attention_prefix(raw)
        line = re.sub(r"[ \t]+", " ", line).strip()
        lines.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _visible_key(text: str) -> str:
    value = re.sub(r"[\u200e\u200f\u202a-\u202e\u2066-\u2069]", "", str(text or ""))
    value = re.sub(r"[^\w\u0600-\u06FF]+", " ", value, flags=re.UNICODE)
    return re.sub(r"\s+", " ", value).strip().casefold()


def _remove_exact_title_repeat(title: str, body: str) -> str:
    """Remove only an exact first-paragraph repeat; never paraphrase or drop facts."""
    if not title or not body:
        return body
    parts = [part.strip() for part in re.split(r"\n\s*\n", body) if part.strip()]
    if parts and _visible_key(parts[0]) == _visible_key(title):
        return "\n\n".join(parts[1:]).strip()
    return body


def _opening_words(text: str, limit: int = 3) -> list[str]:
    value = re.sub(
        r"[\u200e\u200f\u202a-\u202e\u2066-\u2069]",
        "",
        str(text or ""),
    )
    value = re.sub(r"[^\w\u0600-\u06FF]+", " ", value, flags=re.UNICODE)
    words = [word.casefold() for word in value.split() if word]
    return words[:limit]


def _repeats_headline_opening(title: str, body: str) -> bool:
    """Catch headline/body restarts without blocking necessary entity mentions."""
    first_paragraph = next(
        (part.strip() for part in re.split(r"\n\s*\n", str(body or "")) if part.strip()),
        "",
    )
    title_words = _opening_words(title, 3)
    body_words = _opening_words(first_paragraph, 3)
    if len(title_words) >= 2 and len(body_words) >= 2 and title_words[:2] == body_words[:2]:
        return True
    if len(title_words) >= 3 and len(body_words) >= 3 and title_words[:3] == body_words[:3]:
        return True

    clean_title = re.sub(r"[\u200e\u200f\u202a-\u202e\u2066-\u2069]", "", str(title or ""))
    clean_first = re.sub(r"[\u200e\u200f\u202a-\u202e\u2066-\u2069]", "", first_paragraph)
    lead_pairs = (
        ("أداة جديدة", "الأداة الجديدة"),
        ("تحديث جديد", "التحديث الجديد"),
        ("ثغرة جديدة", "الثغرة الجديدة"),
        ("ميزة جديدة", "الميزة الجديدة"),
        ("إصدار جديد", "الإصدار الجديد"),
        ("نسخة جديدة", "النسخة الجديدة"),
        ("خدمة جديدة", "الخدمة الجديدة"),
        ("تقرير جديد", "التقرير الجديد"),
        ("نموذج جديد", "النموذج الجديد"),
    )
    for indefinite, definite in lead_pairs:
        if indefinite in clean_title and (indefinite in clean_first or definite in clean_first):
            return True

    return False


ORIGINAL_TECH_TERMS = (
    "jailbreak",
    "homebrew",
    "firmware",
    "kernel",
    "WebKit",
    "use-after-free",
)


def _normalized_tech_text(text: str) -> str:
    value = re.sub(
        r"[\u200e\u200f\u202a-\u202e\u2066-\u2069]",
        "",
        str(text or ""),
    )
    value = re.sub(
        r"(?<=[A-Za-z0-9])[\u2010\u2011\u2012\u2013\u2212](?=[A-Za-z0-9])",
        "-",
        value,
    )
    return value.casefold()


def _validate_original_technical_terms(source_text: str, output_text: str) -> None:
    source = _normalized_tech_text(source_text)
    output = _normalized_tech_text(output_text)
    missing = [
        term
        for term in ORIGINAL_TECH_TERMS
        if term.casefold() in source and term.casefold() not in output
    ]
    if missing:
        raise RuntimeError(
            "AI translated or omitted original technical terms from source: "
            + ", ".join(missing)
        )


def _dedupe_lead_object(title: str, body: str) -> str:
    """Remove common headline-object repetition without blocking publication."""
    if not title or not body:
        return body
    parts = [part.strip() for part in re.split(r"\n\s*\n", str(body)) if part.strip()]
    if not parts:
        return body

    clean_title = re.sub(
        r"[\u200e\u200f\u202a-\u202e\u2066-\u2069]",
        "",
        str(title),
    )
    first = parts[0]

    noun_forms = {
        "أداة": r"(?:الأداة|أداة)",
        "تحديث": r"(?:التحديث|تحديث)",
        "ثغرة": r"(?:الثغرة|ثغرة)",
        "ميزة": r"(?:الميزة|ميزة)",
        "إصدار": r"(?:الإصدار|إصدار)",
        "نسخة": r"(?:النسخة|نسخة)",
        "خدمة": r"(?:الخدمة|خدمة)",
        "تقرير": r"(?:التقرير|تقرير)",
        "نموذج": r"(?:النموذج|نموذج)",
    }
    for noun, form in noun_forms.items():
        if noun not in clean_title:
            continue
        first = re.sub(
            rf"^(تتيح|تسمح|توفر|تدعم|تشمل|تعمل|تعتمد)\s+(?:هذه\s+)?{form}(?:\s+الجديد(?:ة)?)?\s+",
            r"\1 ",
            first,
            count=1,
        )

    if "أداة" in clean_title:
        first = re.sub(
            r"^تمكّن\s+(?:هذه\s+)?(?:الأداة|أداة)[^،.!؟?]{0,90}?\s+من\s+",
            "تتيح ",
            first,
            count=1,
        )

    parts[0] = re.sub(r"\s+", " ", first).strip()
    return "\n\n".join(parts)


def _strip_generic_editorial_conclusion(body: str) -> str:
    parts = [part.strip() for part in re.split(r"\n\s*\n", str(body or "")) if part.strip()]
    if len(parts) > 1 and GENERIC_EDITORIAL_CONCLUSION_RE.search(parts[-1]):
        parts.pop()
    return "\n\n".join(parts)


def _validate_reader_friendly_copy(title: str, body: str) -> None:
    """Reject copy that is accurate but unnecessarily hard for a general reader."""
    clean_title = re.sub(
        r"[\u200e\u200f\u202a-\u202e\u2066-\u2069]",
        "",
        str(title or ""),
    )
    clean_body = re.sub(
        r"[\u200e\u200f\u202a-\u202e\u2066-\u2069]",
        "",
        str(body or ""),
    )
    visible = f"{clean_title}\n{clean_body}"

    if DISALLOWED_TECH_TRANSLITERATION_RE.search(visible):
        raise RuntimeError(
            "AI used an invented Arabic transliteration for a technical term."
        )

    if len(TITLE_JARGON_RE.findall(clean_title)) > 1:
        raise RuntimeError(
            "Facebook title contains too much unexplained technical jargon."
        )



def _unwrap_latin_parentheses(text: str) -> str:
    """Remove decorative parentheses around Latin-only terms in visible Arabic copy."""
    value = str(text or "")
    previous = None
    while value != previous:
        previous = value
        value = LATIN_PAREN_RE.sub(lambda match: match.group(1).strip(), value)
    return value


def _split_long_facebook_paragraph(text: str, max_chars: int = 240) -> list[str]:
    """Split long prose for readability without changing words or punctuation."""
    value = re.sub(r"[ \t]+", " ", str(text or "")).strip()
    if not value or len(value) <= max_chars:
        return [value] if value else []

    sentences = [
        part.strip()
        for part in re.split(r"(?<=[.!?؟])\s+", value)
        if part.strip()
    ]

    if len(sentences) <= 1:
        chunks = []
        remaining = value
        while len(remaining) > max_chars:
            cut = remaining.rfind(" ", 0, max_chars + 1)
            if cut < max_chars // 2:
                cut = remaining.find(" ", max_chars)
            if cut <= 0:
                break
            chunks.append(remaining[:cut].strip())
            remaining = remaining[cut + 1 :].strip()
        if remaining:
            chunks.append(remaining)
        return chunks or [value]

    chunks = []
    current = ""
    for sentence in sentences:
        candidate = f"{current} {sentence}".strip() if current else sentence
        if current and len(candidate) > max_chars:
            chunks.append(current)
            current = sentence
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def _preserve_ai_facebook_layout(text: str) -> str:
    """Keep AI wording intact while formatting long Facebook copy for reading."""
    value = str(text or "").strip()
    value = value.replace("\r\n", "\n").replace("\r", "\n")

    output: list[str] = []
    prose_buffer: list[str] = []

    def flush_prose() -> None:
        if not prose_buffer:
            return
        paragraph = " ".join(prose_buffer).strip()
        prose_buffer.clear()
        for chunk in _split_long_facebook_paragraph(paragraph):
            if output and output[-1] != "":
                output.append("")
            output.append(chunk)

    for raw_line in value.split("\n"):
        line = re.sub(r"[ \t]+", " ", raw_line).strip()
        if not line:
            flush_prose()
            if output and output[-1] != "":
                output.append("")
            continue

        if re.match(r"^(?:[-•▪◦]|\d+[.)-])\s+", line):
            flush_prose()
            output.append(line)
            continue

        prose_buffer.append(line)

    flush_prose()
    formatted = "\n".join(output)
    return re.sub(r"\n{3,}", "\n\n", formatted).strip()


def enforce_source_policy(
    result: dict[str, Any],
    allowed_links: list[str],
    source_note: str = "",
    has_explicit_title: bool = False,
    source_text: str = "",
) -> dict[str, Any]:
    source_url = _canonical_source_link(allowed_links)
    note = str(source_note or "").strip()
    source_text = str(source_text or "")

    role = str(result.get("link_role") or "none").strip()
    if role not in {"source", "tool", "download", "project", "more_info", "none"}:
        role = "source" if source_url else "none"
    if note and role == "none":
        role = "source"

    result["source_url"] = source_url
    result["link_role"] = role

    # Facebook uses one flowing caption. title is retained only for backward
    # compatibility in stored JSON and must never create a second visible lead.
    title = ""
    result["title"] = ""
    result["language"] = "ar"

    content_type = str(result.get("content_type") or "general").strip()
    result["content_type"] = content_type if content_type in CONTENT_TYPES else "general"

    attention, evidence = _validated_attention(
        source_text,
        str(result.get("attention_label") or "none").strip(),
        str(result.get("attention_evidence") or "").strip(),
    )
    result["attention_label"] = attention
    result["attention_evidence"] = evidence
    result["certainty"] = _validated_certainty(
        source_text,
        str(result.get("certainty") or "confirmed").strip(),
    )

    # Keep editorial analysis auditable, but never trust it as the source of truth.
    result["main_fact"] = re.sub(
        r"\s+", " ", str(result.get("main_fact") or "").strip()
    )[:600]
    result["supporting_facts"] = [
        re.sub(r"\s+", " ", str(value).strip())[:600]
        for value in (result.get("supporting_facts") or [])
        if str(value or "").strip()
    ][:12]

    # Protect entities from the editorial source text, not from URL slugs.
    # A GitHub/project name that appears only inside a source URL is already
    # preserved by source_url/first_comment and must not be forced into the
    # Facebook body.
    source_entity_text = _clean_source_text(source_text).lower()
    result["protected_entities"] = [
        str(value).strip()
        for value in (result.get("protected_entities") or [])
        if str(value or "").strip()
        and str(value).strip().lower() in source_entity_text
    ][:30]
    result["protected_numbers"] = sorted(_numeric_tokens(source_text))

    labels = {
        "source": "المصدر",
        "tool": "الأداة",
        "download": "رابط التحميل",
        "project": "المشروع",
        "more_info": "الرابط",
    }
    if note and source_url:
        label = labels.get(role, "المصدر")
        result["first_comment"] = f"{label}: {note}\n{source_url}"
    elif note:
        label = labels.get(role, "المصدر")
        result["first_comment"] = f"{label}: {note}"
    elif source_url:
        label = labels.get(role, "المصدر")
        result["first_comment"] = f"{label}: {source_url}"
    else:
        result["first_comment"] = ""
        result["link_role"] = "none"

    forbidden_fragments = (
        "internationalcyberdigest.com",
        "t.me/intcyberdigest",
        "x.com/intcyberdigest",
        "twitter.com/intcyberdigest",
        "@intcyberdigest",
        "international cyber digest",
    )

    body = str(result.get("facebook_post") or "")
    for fragment in forbidden_fragments:
        body = re.sub(re.escape(fragment), "", body, flags=re.I)
    for url in allowed_links:
        if url:
            body = body.replace(str(url), "")

    body = _clean_publisher_owned_tokens(body)
    body = _unwrap_latin_parentheses(body)
    body = _preserve_ai_facebook_layout(body)
    body = _strip_generic_editorial_conclusion(body)

    if not body:
        raise RuntimeError("AI returned no visible Facebook copy.")

    visible_copy = body
    _validate_reader_friendly_copy("", body)
    if GENERATED_ATTACK_CLAIM_RE.search(visible_copy) and not SOURCE_ATTACK_EVENT_RE.search(source_text):
        raise RuntimeError(
            "AI turned an exploit or technical capability into an attack/hack that the source did not report."
        )
    _validate_fact_fidelity(source_text, visible_copy)
    _validate_protected_entities(result.get("protected_entities") or [], visible_copy)

    # The AI owns the factual wording; the publisher owns attention emoji,
    # hashtags and final delivery decoration.
    result["title"] = ""
    result["facebook_post"] = force_rtl_paragraphs(body) if body else ""

    card_title = str(result.get("card_title") or "").strip()
    card_title = _unwrap_latin_parentheses(card_title)
    card_title = re.sub(r"\s+", " ", card_title)
    if DISALLOWED_TECH_TRANSLITERATION_RE.search(card_title):
        raise RuntimeError("AI used an invented Arabic transliteration in card title.")
    if len(TITLE_JARGON_RE.findall(card_title)) > 1:
        raise RuntimeError("Card title contains too much unexplained technical jargon.")
    words = [word for word in card_title.split() if word]
    if len(words) > 11:
        card_title = " ".join(words[:11]).rstrip("،,:;؛.!?؟")

    result["card_title"] = isolate_latin_runs_rtl(card_title)
    return result



def _link_hints(urls: list[str]) -> list[dict[str, str]]:
    """Expose link type/domain to the AI without exposing fact-like URL slugs."""
    hints: list[dict[str, str]] = []
    download_suffixes = (
        ".zip", ".7z", ".rar", ".tar", ".gz", ".exe", ".msi", ".apk", ".dmg",
        ".deb", ".rpm", ".iso", ".pdf",
    )

    for raw in urls:
        value = str(raw or "").strip()
        if not value:
            continue
        try:
            parsed = urlparse(value)
        except ValueError:
            continue

        host = (parsed.hostname or "").lower()
        path = (parsed.path or "").lower()
        if not host:
            continue

        if host in {"github.com", "www.github.com", "gitlab.com", "www.gitlab.com"}:
            role_hint = "project"
        elif path.endswith(download_suffixes):
            role_hint = "download"
        else:
            role_hint = "source"

        hint = {"host": host, "role_hint": role_hint}
        if hint not in hints:
            hints.append(hint)

    return hints


def _prompt_payload(item: dict[str, Any]) -> tuple[str, str]:
    instructions = PROMPT_PATH.read_text(encoding="utf-8")

    # URLs are metadata, not editorial facts. Remove them from every text block
    # shown to the model so usernames/project slugs cannot be promoted into
    # unsupported claims. The canonical links remain outside the AI boundary.
    editorial_text = URL_RE.sub("", str(item.get("text") or "")).strip()
    reply_context = []
    for row in item.get("reply_context") or []:
        clean = dict(row)
        clean["text"] = URL_RE.sub("", str(clean.get("text") or "")).strip()
        clean.pop("source_links", None)
        reply_context.append(clean)

    user_payload = {
        "telegram_id": item["telegram_id"],
        "published_at": item["published_at"],
        "text": editorial_text,
        # Do not expose URL paths/slugs to the model. They can contain project,
        # usernames or filenames that are useful as links but are not facts in
        # the Telegram prose. The code restores the canonical URL later.
        "link_hints": _link_hints(item["source_links"]),
        "source_note": item.get("source_note", ""),
        "has_explicit_title": bool(item.get("has_explicit_title")),
        "reply_context": reply_context,
        "media": {
            "has_image": item["has_image"],
            "has_video": item["has_video"],
            "has_document": item["has_document"],
        },
    }
    user_text = (
        "طبّق التعليمات على هذا المنشور وأعد JSON صالحاً فقط.\n\n"
        + json.dumps(user_payload, ensure_ascii=False, indent=2)
    )

    fidelity_retry_hint = str(item.get("_fidelity_retry_hint") or "").strip()
    if fidelity_retry_hint:
        user_text += (
            "\n\nإعادة تصحيح إلزامية: المحاولة السابقة رُفضت لأنها أسقطت "
            "معلومة من المصدر. أعد كتابة facebook_post كاملاً من جديد، ولا تكتفِ "
            "بإضافة الكلمة أو الرقم في النهاية. تأكد أن كل الأسماء والأرقام "
            "والتفاصيل والشروط والأسباب والنتائج الواردة في النص موجودة في "
            "سياقها الصحيح. سبب الرفض السابق: "
            + fidelity_retry_hint
        )

    reader_retry_hint = str(item.get("_reader_retry_hint") or "").strip()
    if reader_retry_hint:
        user_text += (
            "\n\nإعادة تحرير إلزامية للقارئ العام: المحاولة السابقة كانت دقيقة "
            "لكنها لم تكن سهلة بما يكفي لقارئ عربي غير متخصص. أعد كتابة title "
            "وfacebook_post وcard_title من جديد مع الحفاظ على جميع الحقائق المهمة. "
            "ابدأ بالأثر أو التغيير الذي يفهمه المستخدم، وأجّل المصطلحات التقنية "
            "إلى التفاصيل. لا تخترع تعريباً صوتياً ولا تضف خاتمة تحليلية أو وصف "
            "هجوم/اختراق غير موجود في المصدر. سبب الرفض السابق: "
            + reader_retry_hint
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
        item.get("text", ""),
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
            "maxOutputTokens": 1600,
            "responseMimeType": "application/json",
            # Gemini currently rejects JSON Schema's additionalProperties key.
            # Keep it in OUTPUT_SCHEMA for local validation, but omit it here.
            "responseSchema": {
                key: value
                for key, value in OUTPUT_SCHEMA.items()
                if key != "additionalProperties"
            },
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
        # Keep free-tier Groq requests below the enforced output-token budget.
        # The schema is intentionally concise and does not need multi-thousand
        # token generations while preserving every source detail.
        "max_tokens": 1600,
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

    # Prefer Groq first. In production logs it has consistently returned in
    # seconds, while transient Gemini 503/timeouts can otherwise hold the whole
    # queue. Fidelity validation still applies identically to every provider.
    if groq_key:
        for model in _model_list(
            "GROQ_MODELS",
            "GROQ_MODEL",
            DEFAULT_GROQ_MODELS,
        ):
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

    # Keep lighter Gemini models as later fallbacks.
    if gemini_key:
        for model in ("gemini-3.5-flash-lite", "gemini-3.1-flash-lite"):
            yield (
                f"gemini/{model}",
                lambda model=model: _call_one_gemini(
                    item,
                    gemini_key,
                    model,
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

    fidelity_markers = (
        "AI changed or omitted protected entities",
        "AI omitted numeric facts from source",
        "AI omitted CVE identifiers from source",
        "AI output is too short and may have dropped source facts",
    )
    reader_markers = (
        "AI used an invented Arabic transliteration",
        "Facebook title contains too much unexplained technical jargon",
        "Card title contains too much unexplained technical jargon",
        "AI turned an exploit or technical capability into an attack/hack that the source did not report",
    )

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
            first_error = str(exc)

            # A model that produced valid JSON but dropped a source fact gets one
            # immediate repair pass with the exact deterministic rejection reason.
            # This avoids waiting for the next 5-minute cycle and keeps fidelity
            # guards strict instead of weakening them.
            if any(marker in first_error for marker in fidelity_markers):
                item["_fidelity_retry_hint"] = first_error
                try:
                    model, result = call()
                    print(
                        json.dumps(
                            {
                                "ai_provider_selected": model,
                                "fidelity_repair": True,
                                "fallback_failures_before_success": len(errors),
                            },
                            ensure_ascii=False,
                        )
                    )
                    return model, result
                except Exception as repair_exc:
                    first_error += f" | repair: {repair_exc}"
                finally:
                    item.pop("_fidelity_retry_hint", None)

            elif any(marker in first_error for marker in reader_markers):
                item["_reader_retry_hint"] = first_error
                try:
                    model, result = call()
                    print(
                        json.dumps(
                            {
                                "ai_provider_selected": model,
                                "reader_repair": True,
                                "fallback_failures_before_success": len(errors),
                            },
                            ensure_ascii=False,
                        )
                    )
                    return model, result
                except Exception as repair_exc:
                    first_error += f" | reader_repair: {repair_exc}"
                finally:
                    item.pop("_reader_retry_hint", None)

            errors.append(f"{label}: {first_error}")
            print(
                f"WARNING: AI attempt failed, trying next model: {label}: {first_error}",
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
