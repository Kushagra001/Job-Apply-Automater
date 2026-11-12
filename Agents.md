# AGENTS.md — job-pipeline

## Project purpose
Automated job discovery, JD-vs-resume scoring, resume tailoring, and auto-apply for ATS-hosted
job forms. Runs on a schedule via GitHub Actions (free tier). LinkedIn and Naukri quick-apply are
explicitly out of scope for automation — those platforms fingerprint automation environments and
risk account restriction. Naukri listing discovery (scrape-only, no login/apply) is in scope.

Owner: Kushagra Singh Negi. Also runs Stackform (freelance dev agency) and BidCopy (AI bid
proposal SaaS) — this project reuses the same pattern as his cold-outreach pipeline: Groq
(`openai/gpt-oss-20b` / `qwen/qwen3.8-27b`, free tier) for LLM steps, Google Sheets as the datastore/CRM, Playwright for
browser automation.

---

## Current state (last updated: 2026-11-12)

> **Status:** 🟢 Conversion-calibrated — Freshness & geo/work-auth filtering, strict `>= 65` scoring gate, honest resume alignment, company cooldowns, geo-safe ATS autofill, and cold outreach drafting (`scripts/outreach.py`) implemented and unit-tested.

| Stage | Script / Asset | Status |
|---|---|---|
| Repo scaffold | directory tree + stubs | ✅ Done — 2026-08-22 |
| Resume data | `resumes/*.json` (6 variants) | ✅ Done — 2026-11-12 |
| Discovery | `scripts/fetch_jds.py` | ✅ Done — 2026-11-12 |
| Enrichment & Doctor | `scripts/enrich_jd.py`, `scripts/doctor.py` | ✅ Done — 2026-09-21 |
| Scoring | `scripts/score_and_pick.py` | ✅ Done — 2026-11-12 |
| Tailoring | `scripts/tailor_resume.py` | ✅ Done — 2026-11-12 |
| PDF render | `scripts/render_pdf.py` | ✅ Done — 2026-11-12 |
| Logging & Status Track | `scripts/log_and_notify.py`, `scripts/track_applications.py` | ✅ Done — 2026-11-12 |
| Cold Outreach Drafts | `scripts/outreach.py` | ✅ Done — 2026-11-12 |
| ATS apply | `scripts/auto_apply_ats.py` | ✅ Done — 2026-11-12 |
| Orchestrator & CI/CD | `scripts/run_pipeline.py`, `.github/workflows/pipeline.yml` | ✅ Done — 2026-11-12 |
| Hardening | deduplication, cooldowns, retry, dry-run, sheet cleaner, regression tests | ✅ Done — 2026-11-12 |

---

## Non-negotiable rules
- **No blind auto-submit.** Every application must pass the scoring threshold before any resume
  is generated or any form is filled. Never lower the threshold to increase volume.
- **Never fabricate resume content.** Tailoring means re-weighting/reordering real experience
  (see `resumes/*.json`), never inventing skills, metrics, or roles the person hasn't done.
- **LinkedIn/Naukri apply automation is off-limits in this repo.** Logging into Naukri/LinkedIn to submit applications from a
  CI runner is not — do not add this without an explicit, separate request.
- **ATS-only for auto_apply_ats.py.** Only fill/submit on Greenhouse, Lever, or Ashby.
  other public company-hosted application forms that don't require a pre-existing logged-in
  session tied to the user's identity.
- **Secrets never in code.** `GROQ_API_KEY`, `GOOGLE_SA_JSON`, etc. live in GitHub Actions repo
  secrets only. Never print them, log them, or write them to any output file.
- **Every run must log, even on partial failure.** A broken scraper for one source must not
  crash the whole pipeline or skip logging for the sources that succeeded.

---

## Repo structure
```
job-pipeline/
├── .github/workflows/pipeline.yml   # cron schedule + manual trigger + email tracking
├── resumes/
│   ├── frontend.json                # base resume variant — frontend-focused
│   ├── backend-python.json          # base resume variant — Python/Django backend
│   ├── backend-node.json            # base resume variant — Node.js backend
│   ├── ai-engineer.json             # base resume variant — AI/ML engineering
│   ├── sdet-qa.json                 # base resume variant — QA/SDET automation
│   └── solutions-support.json       # base resume variant — Solutions/Technical Support
├── templates/
│   └── resume.html                  # Jinja2 HTML template for Playwright PDF render
├── scripts/
│   ├── run_pipeline.py              # CLI orchestrator, tiered threshold routing, circuit breaker
│   ├── fetch_jds.py                 # discovery: ATS APIs + aggregators, geo/seniority filters
│   ├── enrich_jd.py                 # fills sparse JDs via ATS APIs or Jina Reader
│   ├── doctor.py                    # pre-flight health checks (--doctor)
│   ├── score_and_pick.py            # Groq (with keyword heuristic fallback) scoring
│   ├── tailor_resume.py             # Groq: re-weight bullets within chosen variant
│   ├── render_pdf.py                # Playwright/Chromium HTML->PDF (1-page compressed)
│   ├── auto_apply_ats.py            # Playwright — Greenhouse/Lever/Ashby (+ aggregator redirects)
│   ├── outreach.py                  # Cold founder/recruiter outreach drafts ([Gmail]/Drafts + artifacts)
│   ├── log_and_notify.py            # Sheets append, company cooldowns + Telegram digest & captcha alerts
│   ├── track_applications.py        # Gmail IMAP tracker -> Sheets status & Telegram alerts
│   └── cleanup_sheet.py             # Google Sheets sanitization & deduplication tool
├── tests/                           # pytest suite (70+ unit/regression tests)
└── requirements.txt
```

---

## Data contracts (keep these stable — many scripts depend on the exact shape)

**JD schema** (output of every `fetch_*` function in `fetch_jds.py`):
```json
{
  "title": "string",
  "company": "string",
  "location": "string",
  "remote": true,
  "description": "string",
  "apply_url": "string",
  "source": "remoteok | remotive | jobicy | arbeitnow | himalayas | simplify | yc_startups | hackernews | greenhouse | lever | ashby",
  "fetched_at": "ISO8601"
}
```

**Resume variant schema** (`resumes/*.json`) — structured, not prose:
```json
{
  "variant": "frontend | backend-python | backend-node | ai-engineer | sdet-qa | solutions-support",
  "summary": "string",
  "experience": [
    { "company": "string", "role": "string", "dates": "string", "bullets": ["string", "..."] }
  ],
  "skills": ["string", "..."],
  "projects": [
    { "name": "string", "stack": "string", "bullets": ["string", "..."] }
  ],
  "education": ["string", "..."],
  "certifications": ["string", "..."]
}
```
All bullets must be truthful subsets/rephrasings of real experience. Adding a new variant means
copying real bullets from the source resume and re-weighting emphasis — never inventing new ones.

**Score result** (output of `score_and_pick.py`):
```json
{
  "jd_id": "string",
  "best_variant": "frontend | backend-python | backend-node | ai-engineer | sdet-qa | solutions-support",
  "score": 0-100,
  "missing_skills": ["string", "..."],
  "reasoning": "string"
}
```
Threshold policy: `score_and_pick.py` marks `status="skipped"` below `SCORE_THRESHOLD = 65`.
`run_pipeline.py` enforces the strict `>= 65` gate (`SCORE_TAILOR_THRESHOLD = 65`, `SCORE_VOLUME_FLOOR = 65`),
enforces per-company cooldowns (`14d` / max `2` per 30 days), and triggers `outreach.py` drafts for qualifying high-fit roles.

---

## Environment
- Python 3.10+, `pip install -r requirements.txt`
- Playwright: `playwright install chromium` after pip install
- Secrets expected in env: `GROQ_API_KEY`, `GOOGLE_SA_JSON` (service account JSON, stringified), `GOOGLE_SHEET_ID`
- Optional: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `GROQ_MODEL`, `MAX_JOBS_PER_RUN`, `REMOTE_ONLY`, `REMOTE_FOCUS`, `GMAIL_USER`, `GMAIL_APP_PASSWORD`

---

## Conventions
- Every `fetch_*`, `score_*`, `tailor_*` function is independently testable with a fixture JD —
  don't couple scoring logic to live network calls in tests.
- Scraper functions must wrap API calls in try/except and log failures via
  `log_and_notify.log_failure(source, error)` rather than raising — one broken API endpoint should
  not take down the run.
- Dedupe JDs across sources by normalized `(company, title)` before scoring, to avoid scoring or
  applying to the same listing twice.
- Prefer small, single-purpose functions per source/site — layouts differ enough that a shared
  scraper abstraction usually costs more than it saves.
- Commit messages: reference which pipeline stage changed, e.g. `fetch: add wellfound scraper`.

---

## Agent instructions (for AI coding assistants)

### General
- Read this file, `Progress.md`, AND `STRATEGY_AND_FIXES.md` at the start of every session before writing any code or implementing any fix/strategy.
- Do not start a new checkpoint if the previous one has unresolved blockers listed in `Progress.md`.
- Ask before adding new third-party dependencies — prefer stdlib or already-listed packages.
- After completing any checkpoint or bug fix, update the status table above, `Progress.md`, AND `STRATEGY_AND_FIXES.md` immediately.

### Checkpoint build order (enforced)
Build in this exact sequence — each stage depends on the one above it:

1. **Repo scaffold** — create the directory tree, `requirements.txt`, and empty script stubs
2. **Resume JSON files** — populate `resumes/frontend.json`, `backend-python.json`, `backend-node.json`,
   `ai-engineer.json` with real data (to be provided by owner)
3. **`fetch_jds.py`** — implement API sources (RemoteOK, Remotive)
4. **`score_and_pick.py`** — Groq call with structured output, variant selection, threshold gate
5. **`tailor_resume.py`** — Groq call that mutates bullet ordering/phrasing within one variant
6. **`render_pdf.py`** — fill HTML/LaTeX template with tailored JSON, output deterministic PDF
7. **`log_and_notify.py`** — Google Sheets append + Telegram ping; must handle offline gracefully
8. **`auto_apply_ats.py`** — Playwright flows for each ATS (Greenhouse → Lever → Ashby)
9. **`.github/workflows/pipeline.yml`** — wire all scripts into a scheduled CI run
10. **Hardening** — deduplication, retry logic, --dry-run flag, selector regression tests

### Per-script guidance

#### `fetch_jds.py`
- Implement `fetch_remoteok()` and `fetch_remotive()` via JSON APIs (no Playwright needed).
- All functions must return `List[dict]` matching the JD schema exactly.
- Normalize `remote` field to a boolean; strip HTML from `description`.

#### `score_and_pick.py`
- Use `groq` SDK with verified active fallback models (`openai/gpt-oss-20b` primary, `qwen/qwen3.8-27b` secondary).
- Prompt must request structured JSON output matching the Score result schema — use
  `response_format={"type": "json_object"}` if supported, otherwise parse with `json.loads`.
- Load all resume variants and score the JD against each; return the best.
- Hard gate: if `score < 65`, write a `status: "skipped"` row to Sheets and stop the pipeline
  for that JD.

#### `tailor_resume.py`
- Input: one resume variant JSON + one JD dict.
- Output: a *modified copy* of the resume JSON (never mutate the source file).
- Groq prompt must instruct: re-rank bullets to surface the most relevant ones, update the
  summary to mirror JD keywords — but must not invent new bullets or add skills not in the
  source JSON.
- Validate the output schema before returning (all required fields present, bullet count
  unchanged per role).

#### `render_pdf.py`
- Uses Jinja2 (`templates/resume.html`) → Playwright/Chromium HTML→PDF with automatic single-page compression and `pypdf` text verification. Do not inline styles in the script.
- Output path convention: `output/<sanitized_company>_<variant>_<YYYYMMDD>.pdf`.

#### `log_and_notify.py`
- Google Sheets: use `gspread` with a service account. Sheet ID stored in env var `GOOGLE_SHEET_ID`.
- Columns (in order): `run_id`, `timestamp`, `company`, `title`, `source`, `score`,
  `variant_used`, `status`, `pdf_path`, `apply_url`, `notes`.
- Telegram: POST to Bot API. Fail silently if `TELEGRAM_BOT_TOKEN` is not set.
- `log_failure(source, error)` must write to both Sheets (status=`error`) and stderr.

#### `auto_apply_ats.py`
- Entry point: `apply(jd: dict, pdf_path: str, dry_run: bool = False, cover_letter: str = "", use_persistent_profile: bool = False, zero_llm_mode: bool = False) -> tuple[bool, str]`.
- Detect ATS from `apply_url` domain (`_ATS_DOMAIN_MAP` + `gh_jid=`):
  - `boards.greenhouse.io` / `job-boards.greenhouse.io` / `job-boards.eu.greenhouse.io` / `embed/job_app?for=...&token=...` / `gh_jid=` → Greenhouse flow (supports `iframe#grnhse_iframe` and React `role="combobox"`)
  - `jobs.lever.co` / `apply.lever.co` → Lever flow (with invisible hCaptcha challenge detection)
  - `jobs.ashbyhq.com` → Ashby flow (SPA hydration wait + `#_systemfield_resume` + Yes/No toggles)
  - Aggregator redirect wrappers (`remotive.com`, `remoteok.com`, `arbeitnow.*`, `jobicy.com`, `himalayas.app`) → resolve outbound redirect and ONLY apply if target lands on Greenhouse, Lever, or Ashby.
- `--dry-run` flag: fill all fields but do NOT click the final submit button.
- Every selector has a 20 s timeout (`SELECTOR_TIMEOUT_MS`); raises `ATSTimeoutError` on miss (caught by `apply()`).
- Do not add new ATS platforms without updating the detection table in this section.

---

## Testing before merging changes
- Run `pytest -v` after any changes (unit tests mock network/LLM calls).
- Run `fetch_jds.py` locally against real sources at least once after scraper changes —
  APIs can break or change their schema.
- Run `score_and_pick.py` against 2-3 saved fixture JDs to confirm variant selection still routes correctly.
- Never test `auto_apply_ats.py` against a real company's live form unless intentionally
  submitting an application — use `--dry-run` or `tests/` mocks exclusively.

---

## Open items / explicitly deferred
- LinkedIn/Naukri auto-apply: deferred indefinitely, would require a persistent-session VM
  (e.g. Oracle Cloud free tier) outside GitHub Actions — separate scope if revisited.
- Score threshold calibration: review observed conversion rate between `SCORE_VOLUME_FLOOR = 20` / `SCORE_TAILOR_THRESHOLD = 55` in `run_pipeline.py` vs the stricter `>= 65` gate.