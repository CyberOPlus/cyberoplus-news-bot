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

## Evidence observability follow-up — 2026-10-07

The first three policy-v5 publications (Telegram 1790, 1792 and 1794) were
successfully delivered in English. Their stored rows had no `source_url` or
`primary_evidence`. This is not counted as primary verification merely because
the captions use cautious attribution. Delivery events now record one of:
`linked_primary_excerpt`, `linked_external_source`, or
`discovery_attribution_only`, together with evidence count and link presence.
This measurement changes no publication decision yet; it provides page-specific
coverage data before a source gate is introduced.

Research reviewed again on 2026-10-07:
- Meta's April 24, 2025 spam guidance: relevant captions, restrained hashtags,
  and original/authorized content remain the supported policy direction.
- Reuters Fact Check methodology: trace claims to origins, name sources, and link
  publicly viewable evidence where possible.
- Reuters' August 28, 2026 visual-verification account: images require provenance
  and contextual checks; automated AI detection alone is not conclusive.

These sources support measuring provenance and avoiding false verification
claims. They do not establish a safe universal posting rate or prove performance
for Cybero Plus.

## Missing-date and sensitive-claim hold follow-up — 2026-10-07

Production item 1796 arrived with a Telegram `time` element whose `datetime`
attribute was absent. The collector converted that missing value to the literal
string `"None"`. Freshness policy correctly refused publication, but the publisher
did not make `unverified_source_date` terminal, so the item remained in the
durable queue for every later run. The item was especially unsuitable for an
unsourced exception: it describes a court ruling and an AI-generated likeness of
a deceased victim.

The collector now keeps absent dates as JSON null. The publisher records an
`editorial_hold` for an unverifiable source date, retaining the row and reason for
audit while excluding it from repeated delivery attempts. This does not infer a
date from collection time and does not publish the item. A later explicitly
versioned repair can still reintroduce it with verified metadata if policy allows.

Research checked on 2026-10-07:
- Reuters Fact Check methodology supports tracing claims to their origin and
  linking public evidence rather than treating social discovery as verification.
- Reuters' September 30 report and the published Arizona Court of Appeals opinion
  independently support the underlying event. The court opinion is the stronger
  primary record; this manual discovery is documented evidence for the decision,
  not silently injected into automated copy.

Offline regression coverage reproduces the malformed Telegram time element and
the formerly lingering queue item. No live Facebook call is made.

## Sensitive-claim source gate — 2026-10-07

Page evidence after the observability change showed that item 1801, a federal
court claim, was published as `discovery_attribution_only`. Item 1803 then entered
the ready queue with an allegation against a named person and a $10 million U.S.
government reward, also without a link. This is page-specific evidence of a gap,
not an assumption based on another account's performance.

Facebook delivery now requires a public source link or stored primary excerpt for
captions containing court/criminal-process, wanted-person, allegation or reward
language. An unsourced match enters a durable `editorial_hold`; ordinary product
and technical updates are unaffected. This is a narrow harm-control gate, not a
claim that every matching report is false.

Before activation, item 1803 was enriched with the official U.S. Rewards for
Justice page and a bounded excerpt supporting the reward, identity and attributed
allegations. The existing English AI caption was retained; the source is restored
as a first comment and recorded as primary evidence. The supporting DOJ release
also states that an indictment is an allegation and the defendant is presumed
innocent unless convicted.

Research checked on 2026-10-07:
- https://rewardsforjustice.net/rewards/zhang-yu-sssb/
- https://www.justice.gov/opa/pr/justice-department-announces-arrest-prolific-chinese-state-sponsored-contract-hacker
- https://www.reuters.com/business/media-telecom/what-is-visual-verification-why-does-reuters-practice-it-2026-08-28/

Offline tests cover a blocked court claim, an allowed linked claim and an ordinary
update. No live Facebook test was sent.

## Security-competition result source gate — 2026-10-08

Production item 1804 was published as `discovery_attribution_only`. It summarized
Pwn2Own Ireland day-two results, including precise success, payout and bug-count
claims. The official Zero Day Initiative result page supports several core facts,
but it does not plainly state the caption's broad conclusion that more than half
of attempts reused known bugs. Its “DOOMsday” wording and hashtag also do not, by
themselves, establish the caption's literal claim that a Lexmark printer ran DOOM.

Future security-competition result captions now require a public source URL or
stored evidence before delivery. Unsourced matches enter a durable editorial
hold. The gate is deliberately narrow: it requires both competition context and
result language, so unrelated vendor updates are unaffected. Zero Day Initiative
was added to the exact HTTPS primary-host allowlist for explicit links already
present in discovery posts; this does not turn its blog into a new feed or infer
facts from URL slugs.

Research checked on 2026-10-08:
- https://www.zerodayinitiative.com/blog/2026/10/7/pwn2own-ireland-2026-day-two-results
  (official event result log; retrieved 2026-10-08).
- https://www.zerodayinitiative.com/blog
  (official index confirms the dated result entry; retrieved 2026-10-08).
- https://creativecommons.org/licenses/by/4.0/deed.en
  (reuse requires attribution, a license link and change disclosure; no image
  reuse change was made because the Telegram image's license was not established).

This is a factual-quality safeguard, not an engagement experiment and not a claim
about the page's best posting time. Offline tests cover blocked and linked result
posts plus exact-host validation. The already-published post was not edited or
republished because there is no tested, idempotent correction workflow; no live
Facebook test was sent.
