"""Shared English rollout and conservative delivery eligibility."""
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
EDITORIAL_VERSION = 2

# Claims about courts, criminal process, wanted people and government rewards
# can cause disproportionate harm if a discovery-channel post is wrong or
# missing context. Require a public evidence link before Facebook delivery.
SENSITIVE_CLAIM_RE = re.compile(
    r'(?i)\b(?:court|judge|judicial|warrant|indict(?:ed|ment)?|arrest(?:ed)?|'
    r'charg(?:ed|es?)|convict(?:ed|ion)?|sentenc(?:ed|e)|lawsuit|sued|'
    r'wanted|reward|bounty|alleg(?:ed|edly|ation)|accus(?:ed|ation))\b'
)

# Competitive security-event result posts commonly contain many precise claims
# (success/failure, payout, bug count and affected target) in a compact caption.
# Discovery-channel attribution alone is not enough to validate that scoreboard.
SECURITY_COMPETITION_RE = re.compile(
    r'(?i)\b(?:pwn2own|hacking competition|security competition|bug bounty contest)\b'
)
COMPETITION_RESULT_RE = re.compile(
    r'(?i)\b(?:result(?:s)?|successful|success|failed|failure|won|earned|payout|'
    r'prize|compromised|hacked|exploit(?:ed)?)\b'
)

# A disputed or hedged cyber-intrusion report needs a link readers can inspect.
# Keep the co-occurrence requirement narrow so ordinary vendor product claims do
# not enter a hold merely because they use the word "claim".
CYBER_INTRUSION_RE = re.compile(
    r'(?i)\b(?:hack(?:ed|ing)?|breach(?:ed)?|compromis(?:ed|e)|intrusion|'
    r'unauthori[sz]ed access|infiltrat(?:ed|ion))\b'
)
DISPUTE_MARKER_RE = re.compile(
    r'(?i)\b(?:claim surfaced|disputed|denied|does not believe|did not believe|'
    r'unclear whether|unconfirmed|conflicting reports?)\b'
)

# AI copy that merely says a discovery-channel message is unsupported is not a
# useful news item. Keep this deliberately limited to explicit missing-evidence
# wording; ordinary attributed uncertainty remains eligible.
UNSUPPORTED_LOW_INFORMATION_RE = re.compile(
    r'(?i)(?:\bwithout (?:any )?(?:supporting )?'
    r'(?:evidence|context|details|source)\b|'
    r'\b(?:provided|offered|included|gave) no (?:supporting )?'
    r'(?:evidence|context|verifiable details|source)\b|'
    r'\bunverified (?:claim|report|message|post)\b)'
)

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

def sensitive_claim_needs_source(item):
    text = ' '.join(str(item.get(key) or '') for key in ('facebook_post', 'card_title'))
    if not SENSITIVE_CLAIM_RE.search(text):
        return False
    if str(item.get('source_url') or '').strip():
        return False
    return not any(
        isinstance(row, dict) and str(row.get('url') or '').strip()
        for row in (item.get('primary_evidence') or [])
    )

def has_public_source(item):
    if str(item.get('source_url') or '').strip():
        return True
    return any(
        isinstance(row, dict) and str(row.get('url') or '').strip()
        for row in (item.get('primary_evidence') or [])
    )

def competition_result_needs_source(item):
    text = ' '.join(str(item.get(key) or '') for key in ('facebook_post', 'card_title'))
    return bool(
        SECURITY_COMPETITION_RE.search(text)
        and COMPETITION_RESULT_RE.search(text)
        and not has_public_source(item)
    )

def disputed_intrusion_needs_source(item):
    text = ' '.join(str(item.get(key) or '') for key in ('facebook_post', 'card_title'))
    return bool(
        CYBER_INTRUSION_RE.search(text)
        and DISPUTE_MARKER_RE.search(text)
        and not has_public_source(item)
    )

def unsupported_low_information_needs_hold(item):
    text = ' '.join(str(item.get(key) or '') for key in ('facebook_post', 'card_title'))
    return bool(UNSUPPORTED_LOW_INFORMATION_RE.search(text) and not has_public_source(item))

def eligibility(item, events, current=None):
    current = current or datetime.now(timezone.utc)
    policy = config()['publishing_policy']
    if english_enabled() and item.get('language') != 'en':
        return 'pending_english_rewrite'
    if sensitive_claim_needs_source(item):
        return 'sensitive_claim_without_source'
    if competition_result_needs_source(item):
        return 'competition_result_without_source'
    if disputed_intrusion_needs_source(item):
        return 'disputed_intrusion_without_source'
    if unsupported_low_information_needs_hold(item):
        return 'unsupported_low_information_claim'
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
