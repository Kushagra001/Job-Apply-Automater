# AGENTS.md — job-pipeline

## Project purpose
Automated job discovery, JD-vs-resume scoring, resume tailoring, and auto-apply for ATS-hosted
job forms. Runs on a schedule via GitHub Actions (free tier). LinkedIn and Naukri quick-apply are
explicitly out of scope for automation — those platforms fingerprint automation environments and
risk account restriction. Naukri listing discovery (scrape-only, no login/apply) is in scope.

Owner: Kushagra Singh Negi. Also runs Stackform (freelance dev agency) and BidCopy (AI bid
proposal SaaS) — this project reuses the same pattern as his cold-outreach pipeline: Groq
(Llama 3.3 70B, free tier) for LLM steps, Google Sheets as the datastore/CRM, Playwright for
browser automation.

---

## Current state (last updated: 2026-08-22)

> **Status:** 🟡 In Progress — scaffolding phase. No scripts written yet. Repo is local-only.

| Stage | Script / Asset | Status |
|---|---|---|
| Repo scaffold | directory tree + stubs | ✅ Done — 2026-08-22 |
| Resume data | `resumes/*.json` | ✅ Done — 2026-08-23 |
| Discovery | `scripts/fetch_jds.py` | ✅ Done — 2026-08-29 |
| Scoring | `scripts/score_and_pick.py` | ✅ Done — 2026-08-29 |
| Tailoring | `scripts/tailor_resume.py` | ✅ Done — 2026-08-29 |
| PDF render | `scripts/render_pdf.py` | ✅ Done — 2026-08-29 |
| Logging | `scripts/log_and_notify.py` | ✅ Done — 2026-08-29 |
| ATS apply | `scripts/auto_apply_ats.py` | ✅ Done — 2026-08-29 |
| CI/CD | `.github/workflows/pipeline.yml` | ❌ Not started |
| Hardening | deduplication, retry, dry-run, tests | ❌ Not started |

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
├── .github/workflows/pipeline.yml   # cron schedule + manual trigger
├── resumes/
│   ├── frontend.json                # base resume variant — frontend-focused
│   ├── backend-python.json          # base resume variant — Python/Django backend
│   ├── backend-node.json            # base resume variant — Node.js backend
│   └── ai-engineer.json             # base resume variant — AI/ML engineering
├── scripts/
│   ├── fetch_jds.py                 # discovery: APIs + scrapers, normalizes to JD schema
│   ├── score_and_pick.py            # Groq: score JD against each resume variant, pick best
│   ├── tailor_resume.py             # Groq: re-weight bullets within the chosen variant
│   ├── render_pdf.py                # fills the fixed template with tailored JSON
│   ├── auto_apply_ats.py            # Playwright — Greenhouse/Lever/Ashby only
│   └── log_and_notify.py            # Sheets append + Telegram/email ping
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
  "source": "remoteok | remotive | hackernews | greenhouse | lever | ashby",
  "fetched_at": "ISO8601"
}
```

**Resume variant schema** (`resumes/*.json`) — structured, not prose:
```json
{
  "variant": "frontend | backend-python | backend-node | ai-engineer",
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
  "best_variant": "frontend | backend-python | backend-node | ai-engineer",
  "score": 0-100,
  "missing_skills": ["string", "..."],
  "reasoning": "string"
}
```
Threshold for proceeding to tailoring: score >= 65 (tune based on observed interview conversion,
not application volume).

---

## Environment
- Python 3.11, `pip install -r requirements.txt`
- Playwright: `playwright install chromium` after pip install
- Secrets expected in env: `GROQ_API_KEY`, `GOOGLE_SA_JSON` (service account JSON, stringified)
- Optional: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` for run notifications

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
- Read this file AND `Progress.md` at the start of every session before writing any code.
- Do not start a new checkpoint if the previous one has unresolved blockers listed in `Progress.md`.
- Ask before adding new third-party dependencies — prefer stdlib or already-listed packages.
- After completing any checkpoint, update the status table above AND `Progress.md` immediately.

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
- Use `groq` SDK with model `llama-3.3-70b-versatile`.
- Prompt must request structured JSON output matching the Score result schema — use
  `response_format={"type": "json_object"}` if supported, otherwise parse with `json.loads`.
- Load all four resume variants and score the JD against each; return the best.
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
- Use `weasyprint` (HTML→PDF) or a Jinja2 → LaTeX → `pdflatex` pipeline — pick one and commit.
- Template lives at `templates/resume.html` (or `.tex`). Do not inline style in the script.
- Output path convention: `output/<company>_<variant>_<YYYYMMDD>.pdf`.

#### `log_and_notify.py`
- Google Sheets: use `gspread` with a service account. Sheet ID stored in env var `GOOGLE_SHEET_ID`.
- Columns (in order): `run_id`, `timestamp`, `company`, `title`, `source`, `score`,
  `variant_used`, `status`, `pdf_path`, `apply_url`, `notes`.
- Telegram: POST to Bot API. Fail silently if `TELEGRAM_BOT_TOKEN` is not set.
- `log_failure(source, error)` must write to both Sheets (status=`error`) and stderr.

#### `auto_apply_ats.py`
- Entry point: `apply(jd: dict, pdf_path: str) -> bool`.
- Detect ATS from `apply_url` domain: `greenhouse.io` → Greenhouse flow, `lever.co` → Lever
  (e.g., `boards.greenhouse.io` → Greenhouse flow, `jobs.lever.co` → Lever
  flow, `ashbyhq.com` → Ashby flow.
- `--dry-run` flag: fill all fields but do NOT click the final submit button.
- Every selector must have a timeout of 15 s; raise `ATSTimeoutError` on miss (caught by caller).
- Do not add new ATS platforms without updating the detection table in this section.

---

## Testing before merging changes
- Run `fetch_jds.py` locally against real sources at least once after any change —
  APIs can break or change their schema.
- Run `score_and_pick.py` against 2-3 saved fixture JDs (one per resume variant's ideal case) to
  confirm variant selection still routes correctly.
- Never test `auto_apply_ats.py` against a real company's live form unless intentionally
  submitting an application — use a Greenhouse/Lever sandbox or a `--dry-run` flag that fills but
  does not click submit.

---

## Open items / explicitly deferred
- LinkedIn/Naukri auto-apply: deferred indefinitely, would require a persistent-session VM
  (e.g. Oracle Cloud free tier) outside GitHub Actions — separate scope if revisited.
- PDF template design: owner to supply preferred resume layout before `render_pdf.py` is built.
- Google Sheet ID: owner must create the sheet and set `GOOGLE_SHEET_ID` secret before
  `log_and_notify.py` can be tested end-to-end.