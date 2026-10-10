# Image-rights gate and continuous polling review — 2026-10-10

## Status: review branch only

This document and its helper files are a proposal on `maintenance/image-rights-and-polling-2026-10-10`; they are **not active on `main`**. The live publisher and workflow were not modified. Do not merge this branch until the production publisher passes origin-specific rights decisions into the resolver, credits are tested end to end, the polling helper is wired into the workflow, and the offline suite passes.

## Observed production state

The production workflow log showed successor dispatches approximately every 72 seconds from 15:04 to 15:23 UTC on 2026-10-10, despite `config/timing_strategy.json` specifying `continuous_poll_minutes: 5`. Recent runs were successful and mostly idle; this is unnecessary action churn, not evidence of Facebook post spam. The current publisher policy separately limits publication to one item per attempt, a 30-minute minimum gap, two posts per rolling hour, and twelve per rolling 24 hours.

The current image resolver prefers Telegram attachments and otherwise scrapes article/Open Graph images. Unlike `video_rights.py`, it does not require evidence of ownership, licensing, or permission for images. Public availability or an Open Graph tag is not permission to republish.

## Research checked on 2026-10-10

- Meta, **Rewarding Original Creators on Facebook**, 2026-03-13: https://about.fb.com/news/2026/03/rewarding-original-creators-on-facebook/ — primary platform guidance says original content is prioritized and duplicative posts or low-value edits may be deprioritized. This is not a reach guarantee for Cybero Plus.
- Meta, **Reducing Spammy Content on Facebook**, 2025-04-24: https://about.fb.com/news/2025/04/reducing-spammy-content-on-facebook/ — repetitive spam, unrelated captions, and excessive hashtags can reduce distribution/monetization; this does not define a universal safe posting cadence.
- Wikimedia Commons, **Reusing content outside Wikimedia**: https://commons.wikimedia.org/wiki/Commons:Reusing_content_outside_Wikimedia/licenses/en — licensing is file-specific and may require creator attribution and a license link.
- PostFast, **Best Time to Post Social Media in 2026: 61,008 Posts Scored Against Their Own Normal**, published 2026-09-06: https://postfa.st/blog/best-time-to-post-social-media-data-report — a platform/workspace dataset reports smaller timing effects when each account is compared with its own baseline. It is not Meta data and does not measure this Page.
- Adobe Express, **Facebook posting-time study**, published 2025-11-11: https://www.adobe.com/express/learn/blog/best-time-to-post-on-facebook — its sample of 40,000+ posts differs from PostFast's, so the results are descriptive and not proof of causation or a best time for this Page.

## Proposed changes

1. The proposed image-rights helper denies reuse by default and separates Telegram attachments from linked-source images. It requires explicit reviewed permission; licensed images need an HTTPS license URL and creator credit. The resolver interface accepts explicit per-origin allow decisions, and a branded fallback card avoids fabricating a real-event photograph.
2. The proposed polling helper targets the configured five-minute start-to-start interval, accounting for work already spent in a run. The live workflow still has its current fixed 60-second pause until that integration is approved and applied.
3. No timing, publication-cap, hashtag, or freshness settings were changed. The studies conflict and no page-specific post-level metrics were available, so external benchmarks do not justify changing this Page's schedule.

## Required verification before production use

- Integrate rights decisions into `facebook_publisher.py`; verify an unknown-rights image always falls back and an explicitly licensed image adds its creator/license attribution to the first comment.
- Wire `continuous_pacing.py` into the production workflow and verify the five-minute start-to-start behavior without delaying a run that already took longer than five minutes.
- Run `python -m unittest discover -s tests -v` and `python validate_pipeline.py` in an offline-only workflow with no Facebook secrets.
- Do not run a live Facebook test post. Existing published posts are not edited or deleted by this proposal; any previously used image with uncertain permission requires separate review.
