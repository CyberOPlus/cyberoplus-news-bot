#!/usr/bin/env python3
from __future__ import annotations
import json, os, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
import requests

from image_resolver import (
    build_branded_fallback_asset,
    normalize_for_facebook,
    resolve_post_images,
)

ROOT=Path(__file__).resolve().parent
READY=ROOT/"data"/"ready.jsonl"
EVENTS=ROOT/"data"/"facebook_events.jsonl"
GRAPH_VERSION=os.environ.get("META_GRAPH_VERSION","v26.0")
GRAPH_BASE=f"https://graph.facebook.com/{GRAPH_VERSION}"
MIN_GAP=int(os.environ.get("FACEBOOK_MIN_GAP_MINUTES","12"))

def now(): return datetime.now(timezone.utc)
def now_iso(): return now().isoformat()

def read_jsonl(path):
    if not path.exists(): return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]

def append_event(row):
    EVENTS.parent.mkdir(parents=True,exist_ok=True)
    with EVENTS.open("a",encoding="utf-8") as f:
        f.write(json.dumps(row,ensure_ascii=False)+"\n")

def api(method,path,token,data=None,params=None):
    data=dict(data or {}); params=dict(params or {})
    if method=="GET": params["access_token"]=token
    else: data["access_token"]=token
    r=requests.request(method,f"{GRAPH_BASE}/{path.lstrip('/')}",data=data if method!="GET" else None,params=params if method=="GET" else None,timeout=45)
    p=r.json()
    if not r.ok or "error" in p:
        e=p.get("error") or {}
        raise RuntimeError(f"{e.get('message') or r.status_code} (code={e.get('code')}, subcode={e.get('error_subcode')})")
    return p

def verify(page_id,token):
    p=api("GET",page_id,token,params={"fields":"id,name"})
    if str(p.get("id"))!=str(page_id): raise RuntimeError("FACEBOOK_PAGE_ID does not match the Page token")
    return p

def state():
    posts={}; last=None
    for e in read_jsonl(EVENTS):
        tid=int(e.get("telegram_id",0) or 0)
        if e.get("event")=="published" and tid:
            posts[tid]={"post_id":e.get("facebook_post_id"),"comment":e.get("first_comment") or "","comment_posted":False}
            try:
                dt=datetime.fromisoformat(e.get("published_at"))
                if dt.tzinfo is None: dt=dt.replace(tzinfo=timezone.utc)
                if last is None or dt>last: last=dt
            except: pass
        elif e.get("event")=="comment_posted" and tid in posts:
            posts[tid]["comment_posted"]=True
    return posts,last

def post_comment(post_id,token,message):
    return api("POST",f"{post_id}/comments",token,data={"message":message}).get("id","")

def retry_comments(posts,token):
    for tid,row in sorted(posts.items()):
        if row["comment"] and not row["comment_posted"]:
            try:
                cid=post_comment(row["post_id"],token,row["comment"])
                append_event({"event":"comment_posted","telegram_id":tid,"facebook_post_id":row["post_id"],"comment_id":cid,"commented_at":now_iso()})
            except Exception as exc:
                print(f"WARNING comment retry {tid}: {exc}",file=sys.stderr)

def upload_photo(page_id,token,url):
    p=api("POST",f"{page_id}/photos",token,data={"url":url,"published":"false"})
    return str(p.get("id") or "")


def upload_photo_bytes(page_id, token, asset):
    normalized = normalize_for_facebook(asset)
    url=f"{GRAPH_BASE}/{page_id}/photos"
    response=requests.post(
        url,
        data={"access_token":token,"published":"false"},
        files={"source":(normalized.filename,normalized.content,normalized.mime)},
        timeout=60,
    )
    payload=response.json()
    if not response.ok or "error" in payload:
        error=payload.get("error") or {}
        raise RuntimeError(f"{error.get('message') or response.status_code} (code={error.get('code')}, subcode={error.get('error_subcode')})")
    return str(payload.get("id") or "")

def publish(page_id,token,message,photo_ids=None):
    data={"message":message}
    for index, photo_id in enumerate((photo_ids or [])[:10]):
        data[f"attached_media[{index}]"]=json.dumps({"media_fbid":photo_id})
    p=api("POST",f"{page_id}/feed",token,data=data)
    return str(p.get("id") or "")

def main():
    try:
        page_id=os.environ["FACEBOOK_PAGE_ID"].strip()
        token=os.environ["FACEBOOK_PAGE_ACCESS_TOKEN"].strip()
        page=verify(page_id,token)
        print(json.dumps({"facebook_connection":"ok","page_name":page.get("name"),"page_id":page.get("id")},ensure_ascii=False))

        posts,last=state()
        retry_comments(posts,token)
        posts,last=state()

        ready=read_jsonl(READY)
        pending=[x for x in sorted(ready,key=lambda r:int(r.get("telegram_id",0))) if x.get("status")=="ready" and int(x.get("telegram_id",0)) not in posts]
        if not pending:
            print('{"status":"queue_up_to_date"}'); return 0

        if last and now()<last+timedelta(minutes=MIN_GAP):
            print(json.dumps({"status":"waiting_for_gap","next_allowed_at":(last+timedelta(minutes=MIN_GAP)).isoformat()})); return 0

        item=pending[0]
        tid=int(item["telegram_id"])
        title=str(item.get("title") or "").strip()
        body=str(item.get("facebook_post") or "").strip()
        message=(title+"\n\n"+body).strip() if title and body else (title or body)
        if not message: raise RuntimeError("Ready item has no text")

        media=item.get("media") or {}
        telegram_image_urls=[
            str(url).strip()
            for url in (media.get("image_urls") or [])
            if str(url).strip()
        ]
        if not telegram_image_urls and str(media.get("image_url") or "").strip():
            telegram_image_urls=[str(media.get("image_url")).strip()]

        source_url=str(item.get("source_url") or "").strip()
        assets,image_diagnostics=resolve_post_images(telegram_image_urls,source_url,max_images=10)

        if not assets:
            card_title=str(item.get("card_title") or title or body).strip()
            assets=[build_branded_fallback_asset(card_title, variant_key=tid)]
            image_diagnostics["generated_fallback_used"]=True
            image_diagnostics["selected"]=[{
                "origin":"generated_fallback",
                "width":assets[0].width,
                "height":assets[0].height,
                "url":assets[0].url,
            }]
        else:
            image_diagnostics["generated_fallback_used"]=False

        photo_ids=[]
        media_mode="text"
        media_errors=[]
        for asset in assets:
            try:
                photo_id=upload_photo_bytes(page_id,token,asset)
                if photo_id:
                    photo_ids.append(photo_id)
            except Exception as exc:
                media_errors.append(str(exc))
                print(f"WARNING high-quality image upload failed: {exc}",file=sys.stderr)

        if not photo_ids:
            raise RuntimeError("No image could be uploaded; refusing to publish a text-only post.")

        media_mode="images" if len(photo_ids)>1 else (
            "source_image" if assets and assets[0].origin=="source"
            else "generated_card" if assets and assets[0].origin=="generated_fallback"
            else "image"
        )

        post_id=publish(page_id,token,message,photo_ids)
        first_comment=str(item.get("first_comment") or "").strip()
        append_event({"event":"published","telegram_id":tid,"facebook_post_id":post_id,"published_at":now_iso(),"first_comment":first_comment,"source_url":item.get("source_url") or "","media_mode":media_mode,"media_errors":media_errors,"image_diagnostics":image_diagnostics})

        comment_status="none"
        if first_comment:
            try:
                cid=post_comment(post_id,token,first_comment)
                append_event({"event":"comment_posted","telegram_id":tid,"facebook_post_id":post_id,"comment_id":cid,"commented_at":now_iso()})
                comment_status="posted"
            except Exception as exc:
                comment_status="pending_retry"
                print(f"WARNING first comment pending retry: {exc}",file=sys.stderr)

        print(json.dumps({"status":"published_to_facebook","telegram_id":tid,"facebook_post_id":post_id,"media_mode":media_mode,"first_comment_status":comment_status,"image_diagnostics":image_diagnostics},ensure_ascii=False,indent=2))
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}",file=sys.stderr); return 1

if __name__=="__main__":
    raise SystemExit(main())
