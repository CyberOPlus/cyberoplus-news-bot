#!/usr/bin/env python3
"""Semantic story dedupe for the Cybero Plus Facebook pipeline."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit, urlunsplit

_BIDI_RE = re.compile(r"[\u200e\u200f\u202a-\u202e\u2066-\u2069]")
_DIACRITICS_RE = re.compile(r"[\u0610-\u061a\u064b-\u065f\u0670\u06d6-\u06ed]")
_TOKEN_RE = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿ0-9\u0600-\u06ff][A-Za-zÀ-ÖØ-öø-ÿ0-9._+/#:&'’\-\u0600-\u06ff]*")
_STOPWORDS = {
    "هذا", "هذه", "ذلك", "تلك", "الذي", "التي", "الذين", "في", "من", "على", "عن",
    "إلى", "الى", "مع", "بعد", "قبل", "بين", "عبر", "كما", "وقد", "وهو", "وهي",
    "تم", "أن", "ان", "إن", "ما", "لا", "لم", "لن", "هو", "هي",
    "the", "a", "an", "and", "or", "to", "of", "in", "on", "for", "with", "by",
    "from", "is", "are", "was", "were", "has", "have", "had", "says", "said",
}

def _norm(text: Any) -> str:
    value = _BIDI_RE.sub("", str(text or "")).casefold()
    value = _DIACRITICS_RE.sub("", value).replace("ـ", "")
    value = (
        value.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
        .replace("ى", "ي").replace("ؤ", "و").replace("ئ", "ي")
    )
    value = re.sub(r"https?://\S+", " ", value)
    return re.sub(r"\s+", " ", value).strip()

def _tokens(text: Any) -> set[str]:
    values = set()
    for token in _TOKEN_RE.findall(_norm(text)):
        token = token.strip("._+/#:&'’-")
        if len(token) >= 3 and token not in _STOPWORDS:
            values.add(token)
    return values

def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    union = left | right
    return len(left & right) / len(union) if union else 0.0

def _canonical_url(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return raw.casefold().rstrip("/")
    if not parsed.netloc:
        return raw.casefold().rstrip("/")
    return urlunsplit((parsed.scheme.casefold() or "https", parsed.netloc.casefold(), parsed.path.rstrip("/"), "", ""))

def _editorial(row: dict[str, Any]) -> dict[str, Any]:
    value = row.get("editorial")
    return value if isinstance(value, dict) else {}

def _entity_set(row: dict[str, Any]) -> set[str]:
    return {_norm(v) for v in (_editorial(row).get("protected_entities") or []) if len(_norm(v)) >= 3}

def _number_set(row: dict[str, Any]) -> set[str]:
    return {_norm(v) for v in (_editorial(row).get("protected_numbers") or []) if _norm(v)}

def _main_text(row: dict[str, Any]) -> str:
    editorial = _editorial(row)
    return " ".join(
        p for p in (
            str(editorial.get("main_fact") or "").strip(),
            str(row.get("card_title") or "").strip(),
        ) if p
    )

def _fact_text(row: dict[str, Any]) -> str:
    editorial = _editorial(row)
    return " ".join([_main_text(row), *[str(v or "").strip() for v in (editorial.get("supporting_facts") or [])[:8]]]).strip()

def _story_time(row: dict[str, Any]) -> datetime | None:
    for key in ("source_published_at", "prepared_at", "published_at"):
        raw = str(row.get(key) or "").strip()
        if not raw or raw.casefold() == "none":
            continue
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    return None

def story_fingerprint(row: dict[str, Any]) -> str:
    payload = {
        "url": _canonical_url(row.get("source_url")),
        "entities": sorted(_entity_set(row)),
        "numbers": sorted(_number_set(row)),
        "main": _norm(_main_text(row)),
    }
    if not any(payload.values()):
        return ""
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

def annotate_story(row: dict[str, Any]) -> dict[str, Any]:
    value = dict(row)
    fp = story_fingerprint(value)
    if fp:
        value["story_fingerprint"] = fp
        value["story_fingerprint_version"] = 1
    return value

def story_similarity(candidate: dict[str, Any], previous: dict[str, Any]) -> tuple[float, str]:
    left_url = _canonical_url(candidate.get("source_url"))
    right_url = _canonical_url(previous.get("source_url"))
    if left_url and right_url and left_url == right_url:
        return 1.0, "same_source_url"

    left_fp = str(candidate.get("story_fingerprint") or story_fingerprint(candidate))
    right_fp = str(previous.get("story_fingerprint") or story_fingerprint(previous))
    if left_fp and right_fp and left_fp == right_fp:
        return 1.0, "same_story_fingerprint"

    left_time = _story_time(candidate)
    right_time = _story_time(previous)
    if left_time and right_time and abs((left_time - right_time).total_seconds()) > 96 * 3600:
        return 0.0, "outside_semantic_window"

    le = _entity_set(candidate)
    re_ = _entity_set(previous)
    shared = le & re_
    entity_score = _jaccard(le, re_)

    ln = _number_set(candidate)
    rn = _number_set(previous)
    numbers_conflict = bool(ln and rn and not (ln & rn))

    main_score = _jaccard(_tokens(_main_text(candidate)), _tokens(_main_text(previous)))
    fact_score = _jaccard(_tokens(_fact_text(candidate)), _tokens(_fact_text(previous)))

    if not numbers_conflict and fact_score >= 0.68:
        return fact_score, "high_fact_overlap"
    if not numbers_conflict and main_score >= 0.60:
        return main_score, "high_main_fact_overlap"
    if not numbers_conflict and len(shared) >= 2 and entity_score >= 0.45 and max(main_score, fact_score) >= 0.18:
        return max(main_score, fact_score, entity_score), "same_entities_and_story"
    if not numbers_conflict and len(shared) >= 3 and entity_score >= 0.60:
        return entity_score, "same_distinctive_entities"

    return max(main_score, fact_score, entity_score), "different_story"

def find_duplicate_story(candidate: dict[str, Any], previous_rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    candidate_id = int(candidate.get("telegram_id", 0) or 0)
    best = None
    duplicate_reasons = {
        "same_source_url", "same_story_fingerprint", "high_fact_overlap",
        "high_main_fact_overlap", "same_entities_and_story", "same_distinctive_entities",
    }
    for previous in previous_rows:
        previous_id = int(previous.get("telegram_id", 0) or 0)
        if not previous_id or previous_id == candidate_id:
            continue
        if str(previous.get("status") or "") in {"unsupported_media", "duplicate"}:
            continue
        score, reason = story_similarity(candidate, previous)
        if reason not in duplicate_reasons:
            continue
        match = {
            "duplicate_of_telegram_id": previous_id,
            "reason": reason,
            "score": round(float(score), 4),
            "story_fingerprint": str(previous.get("story_fingerprint") or story_fingerprint(previous)),
        }
        if best is None or match["score"] > best["score"]:
            best = match
    return best
