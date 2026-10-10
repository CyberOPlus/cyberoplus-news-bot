#!/usr/bin/env python3
"""Strict, auditable reuse gate for Facebook post images."""
from __future__ import annotations
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
POLICY_PATH = ROOT / "config" / "image_rights.json"
ALLOWED_STATUSES = {"owned", "licensed", "reuse_allowed"}
KNOWN_STATUSES = ALLOWED_STATUSES | {"unknown", "restricted", "denied"}
ORIGINS = {"telegram", "source"}

@dataclass(frozen=True)
class ImageRightsDecision:
    status: str
    reuse_allowed: bool
    basis: str
    scope: str
    credit: str = ""
    license_url: str = ""
    license_note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

def _is_https_url(value: str) -> bool:
    try:
        parsed = urlparse(str(value or "").strip())
    except ValueError:
        return False
    return parsed.scheme == "https" and bool(parsed.hostname)

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
        return {"version": 1, "default_status": "unknown", "channel_policies": {}, "post_overrides": {}}
    data = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError("config/image_rights.json must contain a JSON object.")
    return data

def _normalize_rule(raw: Any, *, fallback_status: str, basis: str, scope: str) -> ImageRightsDecision:
    rule = raw if isinstance(raw, dict) else {}
    status = str(rule.get("status") or fallback_status or "unknown").strip().lower()
    if status not in KNOWN_STATUSES:
        status = "unknown"
    explicit_allow = rule.get("reuse_allowed") is True
    credit = str(rule.get("credit") or "").strip()
    license_url = str(rule.get("license_url") or "").strip()
    permission_url = str(rule.get("permission_url") or "").strip()
    explicit_basis = str(rule.get("basis") or "").strip()
    metadata_ok = True
    if status == "owned":
        metadata_ok = bool(explicit_basis)
    elif status == "licensed":
        metadata_ok = bool(credit) and _is_https_url(license_url)
    elif status == "reuse_allowed":
        metadata_ok = _is_https_url(permission_url or license_url)
    allowed = status in ALLOWED_STATUSES and explicit_allow and metadata_ok
    resolved_basis = explicit_basis or basis
    if explicit_allow and not metadata_ok:
        resolved_basis = "reuse_rule_missing_required_provenance_metadata"
    return ImageRightsDecision(
        status=status, reuse_allowed=allowed, basis=resolved_basis, scope=scope,
        credit=credit, license_url=license_url or permission_url,
        license_note=str(rule.get("license_note") or "").strip(),
    )

def evaluate_image_rights(item: dict[str, Any], origin: str) -> ImageRightsDecision:
    """Evaluate one origin: a Telegram attachment or linked source image."""
    origin = str(origin or "").strip().lower()
    if origin not in ORIGINS:
        return ImageRightsDecision("unknown", False, "unsupported_image_origin", f"image_origin:{origin or 'unknown'}")
    media = item.get("media") or {}
    telegram_id = str(item.get("telegram_id") or "").strip()
    scope = f"telegram_post:{telegram_id or 'unknown'}:{origin}"
    policy = load_policy()
    overrides = policy.get("post_overrides") or {}
    post = overrides.get(telegram_id) if isinstance(overrides, dict) else None
    rule = post.get(origin) if isinstance(post, dict) else None
    if isinstance(rule, dict):
        return _normalize_rule(rule, fallback_status="unknown", basis="explicit_post_override", scope=scope)
    inline = media.get("image_rights")
    inline_rule = inline.get(origin) if isinstance(inline, dict) else None
    if isinstance(inline_rule, dict):
        return _normalize_rule(inline_rule, fallback_status="unknown", basis="explicit_inline_metadata", scope=scope)
    # Channel permissions apply only to attached Telegram media, not linked pages.
    if origin == "telegram":
        channel = _channel_from_telegram_url(str(media.get("telegram_post_url") or ""))
        channels = policy.get("channel_policies") or {}
        if channel and isinstance(channels, dict):
            for key, raw in channels.items():
                if str(key).lstrip("@").casefold() == channel.casefold():
                    return _normalize_rule(raw, fallback_status="unknown", basis="explicit_channel_policy", scope=f"telegram_channel:@{channel}")
    default_status = str(policy.get("default_status") or "unknown").strip().lower()
    if default_status not in KNOWN_STATUSES:
        default_status = "unknown"
    return _normalize_rule({}, fallback_status=default_status, basis="no_explicit_reuse_permission", scope=scope)
