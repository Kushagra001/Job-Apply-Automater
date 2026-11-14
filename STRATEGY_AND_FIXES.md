# STRATEGY_AND_FIXES.md — Working Strategies & Bug-Fix Registry

> **Mandatory Rule:** Refer to this document alongside `AGENTS.md` and `Progress.md` **before implementing any new fix or strategy**. Update this file whenever a bug is fixed or a conversion strategy is validated/retired so we never repeat past mistakes or regress working paths.

---

## 1. Primary Goal & Target Profile

- **Primary Goal:** Land a **remote-first engineering role** (India Remote or Worldwide / Async EOR with zero visa sponsorship barrier).
- **Candidate Constraints:** Early-career (`0–2 yrs`, MCA `9.03/10`, BCA `8.20/10`) based in Jaipur, India. Core strengths: Full-stack TypeScript/React/Next.js, Python/Django/Flask/FastAPI, Node.js/Express, PostgreSQL/MongoDB/Supabase, AI/LLM pipelines, QA/SDET (`Playwright`/`pytest`), Solutions/Developer Support.
- **Operating Rules:** Never lower `SCORE_THRESHOLD = 65`; never fabricate experience/metrics; never auto-apply on LinkedIn/Naukri.

---

## 2. Proven Working Strategies (Keep Intact)

### A. Discovery & Remote-First Sourcing (`scripts/fetch_jds.py`, `scripts/run_pipeline.py`)
- **Freshness First (`<= 14 days`):** Use `first_published` over `updated_at` on Greenhouse (`updated_at` spikes falsely during bulk syncs like Rubrik's 152-job timestamp bump). Reject unknown/stale zombie reqs `> MAX_JOB_AGE_DAYS`.
- **Canonical Greenhouse Embed URLs:** When `_fetch_single_greenhouse(slug)` encounters a custom corporate domain `absolute_url` (e.g., `rubrik.com`, `stripe.com`, `okta.com`, `mongodb.com`, `databricks.com`), rewriting to `https://job-boards.greenhouse.io/embed/job_app?for={slug}&token={id}` bypasses corporate Akamai/Cloudflare WAF 403s, 302 loops back to marketing pages, and stripped `?gh_jid=` parameters (`validate_ats_job_url` and `enrich_jd.resolve_greenhouse` parse `for=` + `token=`).
- **Remote-Only / Remote-First Optimization:**
  - `REMOTE_ONLY` (`true` in CI `.github/workflows/pipeline.yml`, `--remote-only` CLI flag in `run_pipeline.py`, `default=false` in `fetch_all()` unless configured) filters queue strictly to `remote == True` jobs when landing remote roles is the primary goal.
  - Even when `REMOTE_ONLY=false`, `fetch_all()` orders `remote_jobs + onsite_jobs`, prioritizing direct ATS links (`greenhouse.io`, `lever.co`, `ashbyhq.com`) strictly before Cloudflare-gated aggregator hops (`himalayas.app`) inside each tier, and `score_and_pick.py` gives `+5` to competitive remote jobs (`>= 50`) while penalizing non-remote jobs (`-10`) under `REMOTE_FOCUS=true`.
- **Segment-Aware APAC & ATS Metadata Expansion:**
  - `is_location_ok()` permits broad `APAC` / `Asia Pacific` remote roles (`"APAC"`, `"Remote - NA, APAC, EMEA"`, `"APAC, EMEA, LATAM, Canada"`) unless locked to an incompatible APAC sub-country (`"APAC; Singapore"`), while strictly rejecting `"excluding India"`.
  - `_fetch_single_lever()` inspects `workplaceType == "remote"` and `categories.allLocations` (`fly` remote jobs); `_fetch_single_ashby()` inspects `secondaryLocations` for eligible India/Global remote options; `_fetch_single_greenhouse()` inspects `remote` in `title` alongside geo checks; `fetch_hackernews()` uses `hitsPerPage=500`, `timeout=25`, and a 31-day monthly window; `fetch_himalayas()` queries `/jobs/api/search` (India/Worldwide) without restrictive `seniority="entry-level,junior"` lockout and drops AI-annotation/talent-pool spam (`micro1`, `TELUS Digital`, `G2i`).
- **Board Curation:** Prioritize India-accessible & EOR-friendly boards (`gitlab`, `canonical`, `cloudflare`, `postman`, `vercel`, `airtable`, `chainguard`, `automattic`, `valtech`, `springboard`, `secfix`, `firstwork`, `freehand`, `phantom`, `prefect`, `fly`, `fivetran`, `rubrik`, `druva`, `commerceiq`, `zscaler`, `inmobi`, `posthog`, `supabase`, `airbyte`, `livekit`, `triggerdev`, `elevenlabs`, `hevodata`, `fampay`) while pruning dead/region-locked slugs (`slice`).
- **Arbeitnow Geographic Safety:** Reject non-global European ccTLDs (`arbeitnow.fr`, `.nl`, `.ch`, `.de`, `.es`, `.it`, `.at`, `.be`, `.pl`, `.co.uk`) and localized EU remote phrases (`werken op afstand`, `telearbeit`, `télétravail`) or country suffixes in titles (`Data Scientist - France`) when `location` is empty.

### B. LLM Inference & Cost/Quota Resilience (`scripts/score_and_pick.py`, `scripts/tailor_resume.py`, `scripts/doctor.py`)
- **Active Groq Model Hierarchy (Verified 2026-11-12 against `/v1/models`):**
  1. `openai/gpt-oss-20b` (primary — fast, JSON-object reliable)
  2. `qwen/qwen3.8-27b` (secondary — fast fallback; strip any `<think>...</think>` blocks before `json.loads`)
  3. `openai/gpt-oss-120b` (tertiary — deeper reasoning)
  4. `openai/gpt-oss-safeguard-20b` (last resort)
  - *Retired / Do Not Use:* `groq/compound`, `groq/compound-mini`, `llama-3.3-70b-versatile` (removed from account; caused 404 NotFoundError churn).
- **Never Retry Permanent 404 / Daily Quota Errors:** `_should_retry_groq()` must fail fast on `NotFoundError` (`404 model_not_found`) and `DailyRateLimitExceeded`, switching to the next fallback model or Zero-LLM circuit breaker cleanly.
- **Reasoning-Model Token Budgets:** In `doctor.py`, use `max_tokens >= 256` so reasoning models don't spend 100% of a tiny budget on internal thinking tokens and emit empty `content`.
- **Groq Error Handling:** `_clean_llm_json()` strips `<think>...</think>` blocks and markdown fences cleanly across `score_and_pick.py`, `tailor_resume.py`, `outreach.py`, and `auto_apply_ats.py`.
- **Single-Call Multi-Variant Scoring:** Keep scoring consolidated into 1 Groq call per JD (`score_jd_against_all_variants`) with `>= 65` strict gate and conservative `< 60` cap on sparse JDs (`< 100` chars).

### C. Tailoring, Honest Positioning & PDF Parsing (`scripts/tailor_resume.py`, `templates/resume.html`, `resumes/*.json`)
- **Truthfulness Invariant:** Only re-rank real bullets and deterministically surface JD-matching skills via `_prioritize_matching_skills()`. Keep role names intact (`"Frontend Developer (Freelance)"`, `"Python Web Developer Intern"`). Never inflate RPS/install counts.
- **Guaranteed Remote-Ready Cover Letter:** If the LLM omits `"cover_letter"` or returns empty text, synthesize a grounded 3-sentence fallback citing a real project + async/IST overlap/EOR contractor readiness.
- **ATS PDF Typography:** Keep `font-family: "Liberation Sans", Arial, "Helvetica Neue", Helvetica, sans-serif` and `word-spacing: 0.03em` in `templates/resume.html` so `pypdf` extracts `"by 20%"` cleanly without merged tokens, plus `_sanitize_slug(max_len=50)` so long HN company names never trigger `[Errno 36] File name too long`.

### D. ATS Auto-Apply & Geo/Compliance Safety (`scripts/auto_apply_ats.py`)
- **Modern React Greenhouse (`job-boards.greenhouse.io` & `job-boards.eu.greenhouse.io`):**
  - Dropdowns use React Select `<input role="combobox">` (`selects=0`) and fields often use `id` / `aria-label` instead of `name`. Handle BOTH `<select>` AND `<input role="combobox">` + clickable `[role="option"]`, and keep `[role="combobox"]` out of plain text autofill!
- **Ashby SPA (`jobs.ashbyhq.com`):**
  - Wait for React root hydration (inputs OR `"Page not found"`) before checking `_is_job_closed()`, so closed Ashby reqs classify as `job_closed` instead of timing out after 20s as `apply_failed`.
  - Target `input[type='file']#_systemfield_resume` before falling back to `input[type='file']`.
- **Lever (`jobs.lever.co`):**
  - Invisible `hCaptcha` iframes (`newassets.hcaptcha.com`) may intercept `SUBMIT APPLICATION` clicks (`hcaptchaSubmitBtn`). Detect hCaptcha overlays cleanly as `requires_captcha` for manual Telegram completion instead of `apply_failed`.
- **Foreign Work-Auth Honesty:** Never answer `"Yes"` to US/UK/Canada/EU legal work authorization; abort cleanly with `skipped_geo_knockout`.

### E. Cooldowns, Cold Outreach & Status Tracking (`scripts/log_and_notify.py`, `scripts/outreach.py`, `scripts/track_applications.py`)
- **Per-Company Cooldowns:** Max `1` application per company per run, `14d` cooldown, max `2` per 30 days.
- **Cold Outreach (`scripts/outreach.py`):** Save human-reviewed drafts (`[Gmail]/Drafts` + `output/outreach/*.json`) for high-scoring (`>= 72`), HackerNews, or YC roles; filter image/asset filenames (`@2x.png`, `@1.0.min.js`) out of regex email extractors.
- **Email Status Tracker (`scripts/track_applications.py`):** Match explicit sender display names & subjects **before** body keyword scans (avoid matching short words like `"Fly"`/`"Linear"` in random emails).

---

## 3. Bug Fix History (Chronological)

| Date | Bug / Failure Pattern | Root Cause | Fix & Prevention Rule |
|---|---|---|---|
| 2026-11-11 | Critical schema/apply/PDF regressions | Long filenames caused `[Errno 36]`; `#grnhse_iframe` missed; checkbox crash in `_human_type` | Added `_sanitize_slug`, iframe switching, input type guard (`tests/test_critical_bug_fixes.py`). |
| 2026-11-12 (AM) | Zero callbacks over 1+ month (`~4,973` rows) | Ghost/stale jobs, US/UK walls, inflated RPS bullets, weak tailoring, no cold outreach | Implemented Phases 1–4 callback overhaul (`tests/test_callback_upgrades.py`). |
| 2026-11-12 (PM) | `groq/compound` & `compound-mini` 404 on every Groq call; `doctor` empty `'';` `tailor_resume` empty `cover_letter` | Models retired from Groq; `max_tokens=5` exhausted by reasoning; LLM omitted `cover_letter`; Tenacity retried `404` | Updated `FALLBACK_MODELS` to verified live models, skipped retries on `404`, added `_clean_llm_json` (`<think>` strip), fixed `doctor` `max_tokens=64`, added `_fallback_cover_letter`. |
| 2026-11-12 (PM) | Modern Greenhouse `role="combobox"` unhandled + social `id` fields missed + custom `?gh_jid=` WAF/wrapper timeouts (`Stripe`, `Rubrik`, `Okta`, `Twilio`) | React Select uses `<input role="combobox">`; `name=None` on inputs; corporate domains add WAF/marketing pages | Added `_fill_react_comboboxes`, `id`/`aria-label`/`label` social fallback, EU host support, custom-domain Apply-link clicker, and canonical `job-boards.greenhouse.io` URL normalization in `fetch_jds.py` + `auto_apply_ats.py`. |
| 2026-11-12 (PM) | `182+` Ashby `Timeout filling ashby form` on closed jobs + file upload targeting wrong input | `_is_job_closed` ran before Ashby SPA hydration; `#_systemfield_resume` lacked `name` | Wait for Ashby hydration before `_is_job_closed`, check `#_systemfield_resume` first, handle Yes/No button toggles. |
| 2026-11-12 (PM) | Arbeitnow `.fr/.nl/.ch` leaking past location filter (`400+` Sheet failures) + Aggregator HTML false positives + RemoteOK/Jobicy redirect stalls | Empty `location` bypassed country check; `_ATS_DOMAIN_MAP` lacked regional TLDs; `_is_viable_apply_url` scanned raw HTML text | Reject non-global `arbeitnow.*` ccTLDs and foreign title/EU remote tokens in `fetch_arbeitnow`, support any `arbeitnow.<tld>` in `detect_ats`, parse real outbound `href` links in `_is_viable_apply_url`. |
| 2026-11-12 (PM) | Asset filenames (`@2x.png`) extracted as emails; geo-knockout bypassed if `"India"` anywhere in JD; `Fly`/`Linear` email misattribution; Lever invisible hCaptcha click timeouts | Naive regex matched `@2x.png`; global `has_india` short-circuit; body scan ran before sender/subject; invisible hCaptcha blocked click | Added static-asset extension filter to email extractors, proximity-scoped India exception in `has_geo_knockout_in_text`, reordered `extract_company_from_email`, handled Lever hCaptcha challenge detection. |
| 2026-11-13 (AM) | `REMOTE_ONLY=true` starved at 7 jobs / 1 unique company (`ElevenLabs`); `fetch_hackernews` Algolia 15s timeout; `himalayas.app` Cloudflare pages crowding out direct ATS queues | `APAC` blacklisted unconditionally; Lever/Ashby ignored `workplaceType`/`secondaryLocations`; HN `hitsPerPage=1000` timed out; `himalayas` filtered `seniority=entry-level,junior` & intermixed AI-annotation marketplaces ahead of ATS | Added curated remote-India/EOR boards (`secfix`, `firstwork`, `freehand`, `phantom`, `prefect`, `chainguard`, `valtech`, `fly`), segment-aware APAC rule, Lever/Ashby metadata fallbacks, HN 25s/500-hit stability + 31d thread window, spam filtering (`_is_excluded_spam_listing`), and direct-ATS-first queue sorting. |
| 2026-11-13 (PM) | Ashby closed/deleted listings timed out for 20s as `apply_failed` (`Cursor`, `PostHog`) | React root briefly rendered `Loading...` (68 chars), passing length check before 404 text appeared; timeout exception handler lacked closed-page classification | Hydration wait checks for form inputs OR closed phrases past `Loading...`; `apply()` catches `PlaywrightTimeoutError` and inspects `_is_job_closed()` + `_detect_captcha()` to classify as `job_closed` or `requires_captcha`. |
| 2026-11-13 (PM) | Telegram digest showed `Applied: 0` during dry-runs | `dry_run_success` was not tracked in `_DIGEST_BUFFER` | Added `dry_run_success` buffer and explicit `🧪 Dry Run (Forms Filled)` section in Telegram digest. |
| 2026-11-14 (AM) | Live run applications failed with `Submission unverified` on Ashby & Lever; Groq 429 OTPM limit | Ashby Yes/No questions use `<button>` toggles that weren't clicked; `_verify_submission` missed Ashby's submit button class; Lever hCaptcha challenge was in 2nd iframe; `qwen/qwen3.8-27b` exceeded 1000 OTPM | Added `_fill_button_toggles` & `_dismiss_cookie_banner`; expanded `_verify_submission` button locators & platform confirmation containers; inspected `page.frames` & all iframes in `_detect_captcha`; capped `max_tokens` (500-600) and prioritized `openai/gpt-oss-120b` (2000 OTPM). |
| 2026-11-14 (AM) | Ashby live runs marked `requires_captcha` prematurely | Ashby embeds Google reCAPTCHA Enterprise `bframe` in background (`is_visible: False`) which tripped raw `page.frames` URL check | Restricted `_detect_captcha` to visible challenge elements (`width > 20 and height > 20`) and removed background `recaptcha` check from `page.frames`. |
