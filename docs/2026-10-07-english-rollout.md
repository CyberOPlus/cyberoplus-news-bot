# English rollout and poisoned queue recovery — 2026-10-07

Production evidence: main eec1b92 still configured Arabic. Run 37568515579 (job
112621662456, 03:50 UTC) failed with `Post has no visible Facebook text` on
Telegram 1750. It was the only pending ready row. Last recorded publication was
1784 at 02:33:48 UTC. No AGENTS.md existed.

Research checked on 2026-10-07:
- https://about.fb.com/news/2025/04/cracking-down-spammy-content-facebook/
  Meta's April 24, 2025 announcement warns against irrelevant captions, excessive
  hashtags, spam flooding and unauthorized reuse. This supports relevance,
  restrained tagging and avoiding backlog bursts; it supplies no safe numeric cap.
- https://buffer.com/resources/data-best-content-format-social-media/
  Observational data across Buffer-connected accounts, not a randomized test on
  Cybero Plus. It supports testing media formats, not promising photo reach or
  changing this page's timing without its own metrics.

Changes:
- English generation/validation, source-comment labels, relevant English tags and
  LTR card rendering. Legacy Arabic functions remain for regression coverage.
- Reprepare only unpublished ready rows in the old language; preserve terminal IDs.
  Editorial version wins state merges even when its JSON is shorter.
- Media-only input needs editorial context. Old empty ready rows enter a durable
  editorial hold; stale rows expire from delivery without deleting history.
- Default 30-minute spacing, 2/hour and 12/rolling 24h, 24-hour freshness. These are
  conservative operator defaults, not Meta rules or empirically optimal values.
- Fetch up to two explicit primary links on reviewed HTTPS hosts, no redirects,
  bounded downloads and excerpts. Preserve excerpt URL/time in queue. Outage is
  not verification. No independent global news feed or paid search dependency.
- Persist HTTP 429 Retry-After as a cooldown before any next API operation; never
  immediately retry a potentially accepted publish.

Limitations: regex script validation is not a semantic language detector; evidence
matching is bounded excerpt selection plus AI attribution, not proof of every
claim. General web source discovery, image licensing automation, per-post reach
analytics and statistically powered experiments remain future work. Existing
image selection is retained; no claim of improved reach or completed image-rights
verification is made. No new live post is used for testing.

Verification: offline unittest suite passed; `python validate_pipeline.py` passed
its legacy fidelity/dedupe/rights/state/render checks without Facebook calls.
An English 1600x900 card was rendered and visually inspected for legibility and
no clipping. Additional tests reproduce the empty-row blockage and assert the
next valid item can publish, and prove a saved cooldown prevents even the Page
verification request. Production activation is recorded separately by the commit
and workflow state; tests do not prove external AI/Meta availability.
