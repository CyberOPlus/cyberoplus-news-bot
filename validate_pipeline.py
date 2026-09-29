#!/usr/bin/env python3
"""Safe production-pipeline validation. Never calls Facebook."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from ai_rewriter import _preserve_ai_facebook_layout, _unwrap_latin_parentheses, _validate_fact_fidelity
from image_resolver import build_branded_fallback_asset
from merge_pipeline_state import merge_ai_state, merge_rows, merge_state, tid_key


ROOT = Path(__file__).resolve().parent
OUT = ROOT / "preview-output"
PROMPT = ROOT / "prompts" / "facebook_ar.txt"


def validate_msa_prompt() -> None:
    text = PROMPT.read_text(encoding="utf-8")
    required = (
        "العربية الفصحى",
        '"language": "ar"',
        "جمهور عربي",
    )
    forbidden = (
        "بالدارجة المغربية",
        "استعمل دارجة مغربية",
        "ترجم للدارجة",
        "فالتعليق الأول",
        '"language": "ary"',
    )
    missing = [value for value in required if value not in text]
    present = [value for value in forbidden if value in text]
    if missing:
        raise RuntimeError(f"MSA prompt is missing required markers: {missing}")
    if present:
        raise RuntimeError(f"Dialect markers remain in prompt: {present}")


def validate_numeric_fidelity() -> None:
    # Regression test for punctuation attached to version numbers.
    source = "Firmware versions are supported from 7.00 to 13.60, according to the source."
    output = "يدعم الإصدار النطاق من 7.00 إلى 13.60 وفقاً للمصدر."
    _validate_fact_fidelity(source, output)



def validate_ai_layout_policy() -> None:
    source = "فقرة أولى واضحة.\n\nفقرة ثانية.\n- نقطة أولى\n- نقطة ثانية"
    cleaned = _preserve_ai_facebook_layout(source)
    if cleaned != source:
        raise RuntimeError("AI-selected paragraph/list layout was rebuilt unexpectedly.")

    parenthetical = "أطلقت Microsoft ميزة Copilot (Preview) لدعم وظائف (AI)."
    unwrapped = _unwrap_latin_parentheses(parenthetical)
    if "(Preview)" in unwrapped or "(AI)" in unwrapped:
        raise RuntimeError("Latin-only parenthetical terms remain in visible copy.")
    if "Copilot Preview" not in unwrapped or "AI" not in unwrapped:
        raise RuntimeError("Parenthetical cleanup removed the foreign term itself.")


def validate_merge_logic() -> None:
    remote = [
        {"telegram_id": 10, "status": "ready", "title": "remote"},
        {"telegram_id": 11, "status": "ready"},
    ]
    local = [
        {"telegram_id": 10, "status": "ready", "title": "local", "media": {"has_image": True}},
        {"telegram_id": 12, "status": "ready"},
    ]
    merged = merge_rows(remote, local, key_fn=tid_key)
    ids = sorted(int(row["telegram_id"]) for row in merged)
    if ids != [10, 11, 12]:
        raise RuntimeError(f"Unexpected merge IDs: {ids}")

    state = merge_state(
        {"last_seen_id": 20, "updated_at": "2026-09-29T18:00:00+00:00"},
        {"last_seen_id": 21, "updated_at": "2026-09-29T18:01:00+00:00"},
    )
    if int(state.get("last_seen_id", 0)) != 21:
        raise RuntimeError("State merge did not keep the newest Telegram ID.")

    ai = merge_ai_state(
        {"last_processed_id": 20, "processed_count": 50, "updated_at": "2026-09-29T18:00:00+00:00"},
        {"last_processed_id": 21, "processed_count": 51, "updated_at": "2026-09-29T18:01:00+00:00"},
    )
    if int(ai.get("last_processed_id", 0)) != 21 or int(ai.get("processed_count", 0)) != 51:
        raise RuntimeError("AI-state merge regression.")


def validate_fallback_cards() -> list[dict]:
    OUT.mkdir(parents=True, exist_ok=True)
    titles = (
        "ثغرة جديدة تهدد أجهزة Android القديمة",
        "تحديث أمني يصل إلى Windows 11 هذا الأسبوع",
        "باحثون يكشفون تسريب بيانات من خدمة Cloud عالمية",
        "أداة تستغل WebKit على PS5 من الإصدار 7.00 إلى 13.60",
    )

    rows = []
    for index, title in enumerate(titles, start=1):
        asset = build_branded_fallback_asset(title, variant_key=index - 1)
        if (asset.width, asset.height) != (1600, 900):
            raise RuntimeError(
                f"Fallback card {index} has unexpected dimensions: "
                f"{asset.width}x{asset.height}"
            )
        if len(asset.content) < 30_000:
            raise RuntimeError(f"Fallback card {index} is unexpectedly small.")

        target = OUT / f"validation-card-{index}.jpg"
        target.write_bytes(asset.content)
        rows.append(
            {
                "file": target.name,
                "title": title,
                "width": asset.width,
                "height": asset.height,
                "bytes": len(asset.content),
            }
        )
    return rows


def main() -> int:
    validate_msa_prompt()
    validate_numeric_fidelity()
    validate_ai_layout_policy()
    validate_merge_logic()
    cards = validate_fallback_cards()

    report = {
        "status": "ok",
        "facebook_called": False,
        "msa_prompt": "ok",
        "numeric_fidelity_regression": "ok",
        "ai_layout_policy": "ok",
        "state_merge_logic": "ok",
        "fallback_cards": cards,
    }
    (OUT / "validation.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
