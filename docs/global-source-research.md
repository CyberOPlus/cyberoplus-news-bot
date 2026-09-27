# Cybero Plus — Global cybersecurity source research

This registry expands the current Telegram-only discovery layer into a multi-source pipeline.

## Recommended architecture

1. **Primary official data first**: CISA KEV, CIRCL Vulnerability-Lookup, NVD, GitHub Advisories, CVE List V5, FIRST EPSS.
2. **National CERTs**: CERT-EU, CERT-FR, Canada, Singapore, CERT-In, Morocco maCERT/DGSSI.
3. **Vendor research**: Google Threat Intelligence/Mandiant, Cloudflare, Unit 42, Fortinet and vendor PSIRTs.
4. **Media discovery**: BleepingComputer, The Hacker News, SecurityWeek. These are discovery sources; prefer the original vendor/CERT link for attribution whenever possible.
5. **No-feed sites**: use RSSHub or changedetection.io only when no official RSS/API exists.
6. **Extraction**: feedparser for RSS/Atom, Trafilatura first for full-text extraction, newspaper4k as a fallback.
7. **Enrichment**: correlate CVE IDs with CIRCL Vulnerability-Lookup, CISA KEV and FIRST EPSS.
8. **Deduplication**: group stories by CVE, canonical URL, normalized title and semantic similarity before the Facebook queue.
9. **Publishing**: one Cybero Plus Arabic rewrite per event, with original external source in the first Facebook comment.

## Important

Do not enable every candidate directly in production at once. First run source health checks and duplicate-rate tests; otherwise the same vulnerability can arrive through NVD, CVE, CISA, CERTs, vendors and media within minutes.

See `config/global_sources.json` for the current candidate registry.
