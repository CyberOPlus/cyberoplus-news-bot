#!/usr/bin/env python3
"""Safe production-pipeline validation. Never calls Facebook."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from ai_rewriter import (
    _preserve_ai_facebook_layout,
    enforce_source_policy,
    _strict_entity_tokens,
    _unwrap_latin_parentheses,
    _validate_fact_fidelity,
    _validate_reader_friendly_copy,
)
from image_resolver import build_branded_fallback_asset
from facebook_publisher import MIN_GAP, TIMING, build_delivery_message, queue_priority_key
from merge_pipeline_state import merge_ai_state, merge_rows, merge_state, tid_key
from video_processor import _processing_timeout
from video_rights import evaluate_video_rights
from story_dedupe import annotate_story, find_duplicate_story


ROOT = Path(__file__).resolve().parent
OUT = ROOT / "preview-output"
PROMPT = ROOT / "prompts" / "facebook_ar.txt"


def validate_msa_prompt() -> None:
    text = PROMPT.read_text(encoding="utf-8")
    required = (
        "العربية الفصحى",
        '"language": "ar"',
        "جمهور عربي",
        "قارئ عربي عادي مهتم بالتقنية لكنه غير متخصص",
        "ماذا حدث؟ لماذا يهم القارئ؟ ثم ما التفاصيل التقنية؟",
        "يستطيع قارئ عربي غير متخصص أن يفهم ما حدث ولماذا يهمه من أول سطرين",
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

    # A product identifier may legitimately be expanded while preserving its
    # embedded generation number: PS5 -> PlayStation 5.
    _validate_fact_fidelity(
        "The update works on PS5 firmware 13.60.",
        "يعمل التحديث على PlayStation 5 بإصدار 13.60.",
    )


def validate_entity_policy() -> None:
    if _strict_entity_tokens("AI agents"):
        raise RuntimeError("Generic short acronym phrase AI agents became immutable.")
    if _strict_entity_tokens("frontier AI lab"):
        raise RuntimeError("Generic phrase frontier AI lab became immutable.")
    if _strict_entity_tokens("use-after-free"):
        raise RuntimeError("Lowercase descriptive technical compound became immutable.")

    for value in ("OpenAI", "GitHub", "WebKit", "PS5"):
        if not _strict_entity_tokens(value):
            raise RuntimeError(f"Real technical entity is not protected: {value}")



def validate_ai_layout_policy() -> None:
    source = "🚨 عاجل: فقرة أولى واضحة.\n\nفقرة ثانية.\n- نقطة أولى\n- نقطة ثانية"
    cleaned = _preserve_ai_facebook_layout(source)
    if cleaned != source:
        raise RuntimeError("AI-selected paragraph/list layout was rebuilt unexpectedly.")

    parenthetical = "أطلقت Microsoft ميزة Copilot (Preview) لدعم وظائف (AI)."
    unwrapped = _unwrap_latin_parentheses(parenthetical)
    if "(Preview)" in unwrapped or "(AI)" in unwrapped:
        raise RuntimeError("Latin-only parenthetical terms remain in visible copy.")
    if "Copilot Preview" not in unwrapped or "AI" not in unwrapped:
        raise RuntimeError("Parenthetical cleanup removed the foreign term itself.")


def validate_visible_copy_ownership() -> None:
    generated = {
        "content_type": "breaking_news",
        "attention_label": "breaking",
        "attention_evidence": "source_explicit_breaking",
        "certainty": "confirmed",
        "main_fact": "شركة مثال أطلقت تحديثاً جديداً.",
        "supporting_facts": [],
        "protected_entities": [],
        "protected_numbers": [],
        "title": "",
        "facebook_post": "أصبح التحديث متاحاً للمستخدمين الآن.",
        "first_comment": "",
        "language": "ar",
        "source_url": "",
        "card_title": "شركة مثال تطلق تحديثاً جديداً",
        "link_role": "none",
    }
    result = enforce_source_policy(
        generated,
        [],
        source_text="BREAKING: شركة مثال أطلقت تحديثاً جديداً وأصبح التحديث متاحاً للمستخدمين الآن.",
    )
    visible = str(result.get("facebook_post") or "")
    if "عاجل" in visible or "تحذير" in visible or "❗" in visible:
        raise RuntimeError("Code injected an editorial attention label into AI copy.")
    if str(result.get("card_title") or "").startswith(("عاجل", "تحذير", "مهم")):
        raise RuntimeError("Code injected an attention prefix into AI card title.")


def validate_facebook_delivery_policy() -> None:
    security_item = {
        "telegram_id": 5001,
        "source_published_at": "2026-10-02T10:00:00+00:00",
        "title": "ثغرة جديدة في منتج Example تُستغل ضد المستخدمين",
        "facebook_post": "أكدت الجهة المطورة أن التحقيق مستمر وأن التحديث الأمني متاح الآن.",
        "card_title": "ثغرة في Example تُستغل ضد المستخدمين",
        "editorial": {
            "content_type": "vulnerability",
            "attention_label": "warning",
            "certainty": "confirmed",
            "main_fact": "ثغرة في Example تُستغل ضد المستخدمين.",
        },
    }
    message = build_delivery_message(security_item)
    plain_message = message.replace("\u2067", "").replace("\u2069", "")
    if not plain_message.startswith("⚠️ "):
        raise RuntimeError("Warning emoji was not applied at the RTL headline start.")
    if "تحذير:" in plain_message or "عاجل:" in plain_message or "مهم:" in plain_message:
        raise RuntimeError("Text attention labels leaked into Facebook delivery.")
    if "#الأمن_السيبراني" not in message or "#CyberoPlus" not in message:
        raise RuntimeError("Expected Cybero Plus/topic hashtags are missing.")
    if message.count("#") > 2:
        raise RuntimeError("Delivery renderer exceeded the two-hashtag limit.")
    if "http://" in message or "https://" in message:
        raise RuntimeError("External URL leaked into the Facebook body.")

    ai_item = {
        "telegram_id": 5002,
        "source_published_at": "2026-10-02T10:01:00+00:00",
        "title": "OpenAI تطلق تحديثاً جديداً لأدوات الذكاء الاصطناعي",
        "facebook_post": "يتوفر التحديث تدريجياً للمستخدمين.",
        "card_title": "OpenAI تطلق تحديثاً جديداً",
        "editorial": {
            "content_type": "product_update",
            "attention_label": "none",
            "certainty": "confirmed",
            "main_fact": "OpenAI تطلق تحديثاً جديداً.",
        },
    }
    ai_message = build_delivery_message(ai_item)
    if "#الذكاء_الاصطناعي" not in ai_message:
        raise RuntimeError("AI topic hashtag classification regressed.")

    breaking = dict(ai_item)
    breaking["telegram_id"] = 6001
    breaking["editorial"] = dict(ai_item["editorial"], attention_label="breaking", content_type="breaking_news")
    normal = dict(ai_item)
    normal["telegram_id"] = 6002
    normal["editorial"] = dict(ai_item["editorial"], attention_label="none", content_type="general")
    if not queue_priority_key(breaking) < queue_priority_key(normal):
        raise RuntimeError("Breaking news no longer outranks normal queue items.")

    configured = int((TIMING.get("publishing_policy") or {}).get("minimum_gap_minutes", 0) or 0)
    if configured < 10 or MIN_GAP != configured:
        raise RuntimeError(
            f"Facebook pacing is unsafe or diverged from config: configured={configured}, active={MIN_GAP}"
        )


def validate_reader_friendly_policy() -> None:
    _validate_reader_friendly_copy(
        "أداة جديدة توسع قدرات PS5 على الإصدارات من 7.00 إلى 13.60",
        "تسمح الأداة بتشغيل برامج غير رسمية، ثم تستخدم خللاً في متصفح الجهاز للوصول إلى صلاحيات أعمق داخل النظام.",
    )

    bad_cases = (
        (
            "ثغرة WebKit use-after-free جديدة في PS5",
            "يمكن استغلالها عبر المتصفح.",
        ),
        (
            "أداة جديدة لأجهزة PS5",
            "تم الكشف عن جايبريك جديد للأجهزة.",
        ),
        (
            "أداة جديدة تفتح جميع إصدارات PS5",
            "أداة جديدة تتيح تشغيل برامج غير رسمية على الجهاز.",
        ),
        (
            "أداة جديدة لأجهزة PS5",
            "تم الكشف عن جايكربريك جديد للأجهزة.",
        ),
        (
            "أداة جديدة لأجهزة PS5",
            "تم الكشف عن جايكبريك جديد للأجهزة.",
        ),
        (
            "أداة جديدة لأجهزة PS5",
            "تسمح الأداة بتشغيل برامج homebrew عبر WebKit مباشرة على الجهاز.",
        ),
        (
            "تحديث جديد لأجهزة PS5",
            "هذه التقنية قد تفتح الباب أمام مزيد من الاستخدامات مستقبلاً.",
        ),
        (
            "تحديث جديد لأجهزة PS5",
            "هذا التطور يفتح باباً لتشغيل تطبيقات غير مدعومة رسمياً.",
        ),
    )
    for title, body in bad_cases:
        try:
            _validate_reader_friendly_copy(title, body)
        except RuntimeError:
            continue
        raise RuntimeError(
            f"Reader-friendly policy accepted unsuitable Facebook copy: {title}"
        )


def validate_story_dedupe() -> None:
    first = annotate_story({
        "telegram_id": 1680,
        "source_published_at": "2026-09-28T15:21:22+00:00",
        "status": "ready",
        "card_title": "توقيف مشتبه به في ShinyHunters بهولندا",
        "editorial": {
            "main_fact": "توقيف مشتبه به في ShinyHunters يدعى Umbreon Pepijn van der Stap في هولندا.",
            "supporting_facts": ["المشتبه به يبلغ 23 سنة.", "احتجز في 16 سبتمبر."],
            "protected_entities": ["ShinyHunters", "Umbreon", "Pepijn van der Stap"],
            "protected_numbers": ["16", "23"],
        },
    })
    same_story = annotate_story({
        "telegram_id": 1682,
        "source_published_at": "2026-09-28T19:30:52+00:00",
        "status": "ready",
        "card_title": "اعتقال Umbreon المشتبه به في قضية ShinyHunters",
        "editorial": {
            "main_fact": "اعتقال Pepijn van der Stap المعروف باسم Umbreon والمشتبه به في ShinyHunters.",
            "supporting_facts": ["عمره 23 سنة.", "أعيد احتجازه في 16 سبتمبر."],
            "protected_entities": ["ShinyHunters", "Umbreon", "Pepijn van der Stap"],
            "protected_numbers": ["16", "23"],
        },
    })
    match = find_duplicate_story(same_story, [first])
    if not match or match.get("duplicate_of_telegram_id") != 1680:
        raise RuntimeError("Semantic dedupe failed to catch the known duplicate story.")

    real_update = annotate_story({
        "telegram_id": 1683,
        "source_published_at": "2026-09-28T20:30:52+00:00",
        "status": "ready",
        "card_title": "ShinyHunters تعلن حادثة جديدة لدى شركة أخرى",
        "editorial": {
            "main_fact": "ShinyHunters أعلنت حادثة منفصلة تخص شركة أخرى.",
            "supporting_facts": [],
            "protected_entities": ["ShinyHunters", "شركة أخرى"],
            "protected_numbers": [],
        },
    })
    if find_duplicate_story(real_update, [first]):
        raise RuntimeError("Semantic dedupe incorrectly blocked a materially different update.")


def validate_video_rights_policy() -> None:
    item = {
        "telegram_id": 999999999,
        "media": {
            "has_video": True,
            "telegram_post_url": "https://t.me/IntCyberDigest/999999999",
        },
    }
    decision = evaluate_video_rights(item)
    if decision.reupload_allowed:
        raise RuntimeError("Unverified third-party Telegram video became reusable.")

    # Twenty minutes must receive substantially more CPU budget than the old
    # fixed five-minute processing timeout.
    if _processing_timeout(20 * 60) < 20 * 60:
        raise RuntimeError("Long-video processing timeout is too short.")


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
    validate_visible_copy_ownership()
    validate_facebook_delivery_policy()
    validate_reader_friendly_policy()
    validate_story_dedupe()
    validate_entity_policy()
    validate_video_rights_policy()
    validate_merge_logic()
    cards = validate_fallback_cards()

    report = {
        "status": "ok",
        "facebook_called": False,
        "msa_prompt": "ok",
        "numeric_fidelity_regression": "ok",
        "ai_layout_policy": "ok",
        "visible_copy_ai_ownership": "ok",
        "facebook_delivery_policy": "ok",
        "reader_friendly_policy": "ok",
        "semantic_story_dedupe": "ok",
        "entity_policy": "ok",
        "video_rights_policy": "ok",
        "long_video_budget": "ok",
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
