#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ai_batch_processor import enrich_reply_context
from ai_rewriter import call_gemini, normalize_item
from facebook_publisher import (
    EVENTS,
    api,
    append_event,
    post_comment,
    publish_images,
    publish_video_file,
    upload_photo_bytes,
    verify,
)
from image_resolver import (
    build_branded_fallback_asset,
    resolve_post_images,
    telegram_image_candidates,
)
from telegram_collector import fetch_page, parse_messages
from video_processor import prepare_branded_video

TARGET_IDS=(1674,1675,1676)
EXPECTED={
    1674:{"text_all":("google maps","gaza strip")},
    1675:{"text_all":("openai","anthropic")},
    1676:{"source_any":("sh3llc0d3.com","netscaler")},
}
OUT=Path("live-test-output")

def events():
    if not EVENTS.exists(): return []
    return [json.loads(x) for x in EVENTS.read_text(encoding="utf-8").splitlines() if x.strip()]

def published_ids():
    return {int(x.get("telegram_id",0) or 0) for x in events() if x.get("event")=="published"}

def validate_target(raw):
    tid=int(raw["telegram_id"])
    rule=EXPECTED[tid]
    text=str(raw.get("raw_text") or raw.get("clean_text") or "").lower()
    links=" ".join(str(x) for x in (raw.get("source_links") or [])).lower()
    for marker in rule.get("text_all",()):
        if marker not in text:
            raise RuntimeError(f"Telegram {tid} safety marker missing: {marker}")
    any_markers=rule.get("source_any",())
    if any_markers and not any(m in links or m in text for m in any_markers):
        raise RuntimeError(f"Telegram {tid} source safety markers missing")

def unique(values):
    out=[]
    for value in values:
        value=str(value or "").strip()
        if value and value not in out: out.append(value)
    return out

def live_verify(object_id,token,mode):
    fields="id,created_time,permalink_url"
    fields += ",description" if mode=="video" else ",message,full_picture,attachments{media_type,url,target,media,subattachments}"
    try:
        return api("GET",object_id,token,params={"fields":fields})
    except Exception as exc:
        return {"id":object_id,"verification_error":str(exc)}

def main():
    OUT.mkdir(parents=True,exist_ok=True)
    page_id=os.environ["FACEBOOK_PAGE_ID"].strip()
    token=os.environ["FACEBOOK_PAGE_ACCESS_TOKEN"].strip()
    page=verify(page_id,token)

    messages=parse_messages(fetch_page())
    by_id={int(x["telegram_id"]):x for x in messages}
    missing=[x for x in TARGET_IDS if x not in by_id]
    if missing: raise RuntimeError(f"Target IDs not found on current Telegram page: {missing}")

    targets=[by_id[x] for x in TARGET_IDS]
    for raw in targets: validate_target(raw)
    targets=enrich_reply_context(targets,messages)

    done=published_ids()
    results=[]
    for raw in targets:
        tid=int(raw["telegram_id"])
        if tid in done:
            results.append({"telegram_id":tid,"status":"already_published_skipped"})
            continue

        item=normalize_item(raw)
        model,rewritten=call_gemini(item)
        message=str(rewritten.get("facebook_post") or "").strip()
        if not message: raise RuntimeError(f"Telegram {tid} produced no Facebook text")
        first_comment=str(rewritten.get("first_comment") or "").strip()
        source_url=str(rewritten.get("source_url") or "").strip()

        warnings=[]
        post_id=""
        mode=""
        details={}

        if raw.get("has_video"):
            video_urls=unique(list(raw.get("video_urls") or [])+([raw.get("video_url")] if raw.get("video_url") else []))
            try:
                with tempfile.TemporaryDirectory(prefix=f"cyberoplus-live-{tid}-") as td:
                    video=prepare_branded_video(video_urls,str(raw.get("telegram_post_url") or ""),Path(td))
                    details={"width":video.width,"height":video.height,"duration":video.duration,"bytes":video.size,"branded":video.branded}
                    post_id=publish_video_file(page_id,token,message,video)
                    mode="video"
            except Exception as exc:
                warnings.append(f"video fallback: {exc}")

        if not post_id:
            stored=unique(list(raw.get("image_urls") or [])+([raw.get("image_url")] if raw.get("image_url") else [])+list(raw.get("video_thumbnail_urls") or []))
            fresh=telegram_image_candidates(str(raw.get("telegram_post_url") or ""))
            assets,details=resolve_post_images(unique(fresh+stored),source_url,max_images=10)
            if not assets:
                assets=[build_branded_fallback_asset(str(rewritten.get("card_title") or message),variant_key=tid)]
                details["generated_fallback_used"]=True

            photo_ids=[]
            for asset in assets:
                try:
                    pid=upload_photo_bytes(page_id,token,asset)
                    if pid: photo_ids.append(pid)
                except Exception as exc:
                    warnings.append(f"image: {exc}")
            if not photo_ids: raise RuntimeError(f"Telegram {tid}: no media uploaded")

            post_id=publish_images(page_id,token,message,photo_ids)
            mode="generated_card" if getattr(assets[0],"origin","")=="generated_fallback" else ("images" if len(photo_ids)>1 else "image")
            details["uploaded_count"]=len(photo_ids)

        append_event({
            "event":"published","telegram_id":tid,"facebook_post_id":post_id,
            "published_at":datetime.now(timezone.utc).isoformat(),
            "first_comment":first_comment,"source_url":source_url,
            "media_mode":mode,"live_test":True
        })

        comment_id=""
        if first_comment:
            try:
                comment_id=post_comment(post_id,token,first_comment)
                append_event({
                    "event":"comment_posted","telegram_id":tid,
                    "facebook_post_id":post_id,"comment_id":comment_id,
                    "commented_at":datetime.now(timezone.utc).isoformat(),
                    "live_test":True
                })
            except Exception as exc:
                warnings.append(f"comment: {exc}")

        time.sleep(2)
        results.append({
            "telegram_id":tid,"status":"published","facebook_object_id":post_id,
            "media_mode":mode,"ai_model":model,
            "first_comment_posted":bool(comment_id) if first_comment else None,
            "media_details":details,"live_verification":live_verify(post_id,token,mode),
            "warnings":warnings
        })
        done.add(tid)
        if tid!=TARGET_IDS[-1]: time.sleep(3)

    payload={"status":"target_three_live_test_complete","page_id":page.get("id"),"page_name":page.get("name"),"target_ids":list(TARGET_IDS),"results":results}
    (OUT/"results.json").write_text(json.dumps(payload,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(payload,ensure_ascii=False,indent=2))
    return 0

if __name__=="__main__":
    try: raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}",file=sys.stderr)
        raise
