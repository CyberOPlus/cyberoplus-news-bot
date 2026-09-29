#!/usr/bin/env python3
"""Conservative video-rights gate for Facebook re-uploads.

A Telegram attachment is never treated as reusable merely because it is public.
Re-upload is allowed only by an explicit local policy or per-post override.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parent
POLICY_PATH = ROOT / "config" / "video_rights.json"
ALLOWED_STATUSES = {"owned", "licensed", "reuse_allowed"}
KNOWN_STATUSES = ALLOWED_STATUSES | {"unknown", "restricted", "denied"}


@dataclass(frozen=True)
class VideoRightsDecision:
    status: str
    reupload_allowed: bool
    basis: str
    scope: str
    license_note: str = ""
    clip_start_seconds: float | None = None
    max_clip_seconds: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _channel_from_telegram_url(url: str) -> str:
    try:
        parsed = urlparse(str(url or ""))
    except ValueError:
        return ""
    if (parsed.hostname or "").lower() not in {"t.me", "www.t.me", "telegram.me", "www.telegram.me"}:
        return ""
    parts = [part for part in parsed.path.split("/") if part]
    return parts[0].lstrip("@") if parts else ""


def load_policy() -> dict[str, Any]:
    if not POLICY_PATH.exists():
        return {
            "version": 1,
            "default_status": "unknown",
            "channel_policies": {},
            "post_overrides": {},
        }
    data = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError("config/video_rights.json must contain a JSON object.")
    return data


def _normalize_rule(
    raw: Any,
    *,
    fallback_status: str,
    basis: str,
    scope: str,
) -> VideoRightsDecision:
    rule = raw if isinstance(raw, dict) else {}
    status = str(rule.get("status") or fallback_status or "unknown").strip().lower()
    if status not in KNOWN_STATUSES:
        status = "unknown"

    # An allow-status is necessary but not sufficient: the rule must explicitly
    # opt into re-uploading. This avoids a typo or descriptive label silently
    # turning into publication permission.
    explicit_allow = rule.get("reupload_allowed") is True
    allowed = status in ALLOWED_STATUSES and explicit_allow

    def optional_number(name: str) -> float | None:
        value = rule.get(name)
        if value in (None, ""):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return max(0.0, number)

    clip_start = optional_number("clip_start_seconds")
    max_clip = optional_number("max_clip_seconds")
    if max_clip == 0:
        max_clip = None

    return VideoRightsDecision(
        status=status,
        reupload_allowed=allowed,
        basis=str(rule.get("basis") or basis),
        scope=scope,
        license_note=str(rule.get("license_note") or "").strip(),
        clip_start_seconds=clip_start,
        max_clip_seconds=max_clip,
    )


def evaluate_video_rights(item: dict[str, Any]) -> VideoRightsDecision:
    """Return the publication decision for one prepared queue item."""
    policy = load_policy()
    media = item.get("media") or {}
    telegram_id = str(item.get("telegram_id") or "").strip()
    telegram_url = str(media.get("telegram_post_url") or "").strip()
    channel = _channel_from_telegram_url(telegram_url)

    overrides = policy.get("post_overrides") or {}
    if telegram_id and isinstance(overrides, dict) and telegram_id in overrides:
        return _normalize_rule(
            overrides[telegram_id],
            fallback_status="unknown",
            basis="explicit_post_override",
            scope=f"telegram_post:{telegram_id}",
        )

    # Inline metadata is supported for future collectors/importers, but it must
    # still carry explicit reupload_allowed=true. AI output is never consulted.
    inline = media.get("video_rights")
    if isinstance(inline, dict) and inline:
        return _normalize_rule(
            inline,
            fallback_status="unknown",
            basis="explicit_inline_metadata",
            scope=f"telegram_post:{telegram_id or 'unknown'}",
        )

    channels = policy.get("channel_policies") or {}
    if channel and isinstance(channels, dict):
        for key, rule in channels.items():
            if str(key).lstrip("@").casefold() == channel.casefold():
                return _normalize_rule(
                    rule,
                    fallback_status="unknown",
                    basis="explicit_channel_policy",
                    scope=f"telegram_channel:@{channel}",
                )

    default_status = str(policy.get("default_status") or "unknown").strip().lower()
    if default_status not in KNOWN_STATUSES:
        default_status = "unknown"

    return VideoRightsDecision(
        status=default_status,
        reupload_allowed=False,
        basis="no_explicit_reuse_permission",
        scope=f"telegram_channel:@{channel}" if channel else "video_attachment",
        license_note="",
    )
