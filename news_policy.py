"""Shared English rollout and conservative delivery eligibility."""
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
EDITORIAL_VERSION = 2

def config():
    return json.loads((ROOT / 'config/timing_strategy.json').read_text())

def english_enabled():
    return config().get('language', {}).get('primary') == 'en'

def require_english(text):
    # This is a script gate, not a semantic language detector. The model must
    # also declare English; names may retain accented Latin characters.
    if not re.search(r'[A-Za-z]', text) or re.search(r'[\u0600-\u06ff\u0750-\u077f\u4e00-\u9fff\u0400-\u04ff]', text):
        raise RuntimeError('English editorial copy required; keep item pending for AI repair.')
    if re.search(r'(?i)\b(?:like and share|share this|comment below|tag your friends|you won.t believe)\b', text):
        raise RuntimeError('Engagement bait is not allowed.')

def timestamp(value):
    try:
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
    except (TypeError, ValueError):
        return None

def eligibility(item, events, current=None):
    current = current or datetime.now(timezone.utc)
    policy = config()['publishing_policy']
    if english_enabled() and item.get('language') != 'en':
        return 'pending_english_rewrite'
    published = timestamp(item.get('source_published_at'))
    if not published or published > current + timedelta(minutes=5):
        return 'unverified_source_date'
    if current - published > timedelta(hours=policy.get('max_source_age_hours', 24)):
        return 'stale_source'
    unique = {}
    for event in events:
        at = timestamp(event.get('published_at'))
        if event.get('event') == 'published' and at and at > current - timedelta(hours=24):
            unique[event.get('facebook_post_id') or event.get('telegram_id')] = at
    if len(unique) >= policy.get('max_posts_rolling_24h', 12):
        return 'rolling_daily_cap'
    if sum(at > current - timedelta(hours=1) for at in unique.values()) >= policy.get('max_posts_rolling_hour', 2):
        return 'rolling_hourly_cap'
    return ''
