# Image-rights gate and continuous polling review — 2026-10-10

## Scope and observed production state

Reviewed the current `main` branch, `AGENTS.md`, publisher workflow, image resolver, video-rights policy, durable queues, publication-event history, and recent workflow logs on 2026-10-10.

The production workflow log showed a successor dispatch approximately every 72 seconds from 15:04 to 15:23 UTC, despite `config/timing_strategy.json` specifying `continuous_poll_minutes: 5`. The runs were mostly successful and idle; this was unnecessary action churn, not evidence of Facebook post spam. The existing publisher still enforces one item per attempt, a 30-minute minimum gap, and rolling caps of two posts per hour and twelve per 24 hours.

The current image resolver selected Telegram attachments first and otherwise scraped Open Graph/article images. Unlike `video_rights.py`, it did not require ownership, license, or reuse-permission evidence for images. Public availability and an OG tag are not permission to republish.

## Research checked on 2026-10-10

- Meta, **Rewarding Original Creators on Facebook**, 2026-03-13: https://about.fb.com/news/2026/03/rewarding-original-creators-on-facebook/  
  Meta says it prioritizes original content and deprioritizes duplicative posts or low-value edits. This is primary platform guidance, not a guarantee of reach for Cybero Plus.
- Meta, **Reducing Spammy Content on Facebook**, 2025-04-24: https://about.fb.com/news/2025/04/reducing-spammy-content-on-facebook/  
  Meta describes reduced distribution/monetization for repetitive spam, unrelated captions, and excessive hashtags. This does not establish a universally safe posting cadence.
- Wikimedia Commons, **Reusing content outside Wikimedia**: https://commons.wikimedia.org/wiki/Commons:Reusing_content_outside_Wikimedia/licenses/en  
  The license is attached to each file and may require attribution and a license link; reusers must verify the individual file's terms.
- PostFast, **Best Time to Post Social Media in 2026: 61,008 Posts Scored Against Their Own Normal**, published 2026-09-06: https://postfa.st/blog/best-time-to-post-social-media-data-report  
  This published platform/workspace dataset reports smaller timing effects after comparing posts with each account's own baseline. It is not Meta data and does not measure this Page.
- Adobe Express, **Facebook posting-time study**, published 2025-11-11: https://www.adobe.com/express/learn/blog/best-time-to-post-on-facebook  
  This study analyzed over 40,000 posts from top creators. Its sample differs from PostFast's; descriptive timing associations are not proof that changing the schedule causes higher reach.

## Decisions and hypotheses

1. **Image rights:** Treat each origin separately. Telegram attachments and linked-source images are denied by default. Reuse is enabled only by a reviewed post/channel rule or explicit non-AI `media.image_rights` metadata. A licensed rule requires an HTTPS license URL and creator credit; a permission rule requires an HTTPS permission/license reference. Required credit/license details are appended to the first comment. When rights are unknown, the publisher uses the existing owned branded card with the AI-written English title; it does not fabricate an event photograph.
2. **Polling:** Pace the continuous successor against the configured five-minute start-to-start interval, rather than sleeping a fixed 60 seconds after each run. If processing itself exceeds five minutes, the successor can continue immediately. Keep the existing five-minute cron and watchdog as recovery paths.
3. **Scheduling/growth:** Do not change the 30-minute gap, rolling caps, or post-time strategy in this review. Published timing studies conflict and no Page-specific post-level metrics were available in the inspected state. Another account's data is not a valid claim about this Page's best time.

## Verification plan

- Offline tests cover default-deny image rights, required license metadata, origin separation, image-download suppression, image-credit comments, and start-to-start polling math.
- The test-only workflow has no Facebook secrets and makes no live post. A green offline suite verifies local logic only; it does not prove Meta or AI provider availability, legal ownership of a specific existing image, or future reach.
- Existing published posts are not edited or deleted automatically. This change governs future reuse; previously published images should be reviewed separately if permission is uncertain.
