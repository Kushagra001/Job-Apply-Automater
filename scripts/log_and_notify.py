"""
log_and_notify.py — Logging & Notifications
=============================================
Appends every pipeline result (including errors) to a Google Sheet and
optionally sends a Telegram message. Degrades gracefully when credentials
are absent — a missing Telegram token is a warning, not a crash.

Google Sheet columns (in order — do NOT reorder):
  run_id | timestamp | company | title | source | score |
  variant_used | status | pdf_path | apply_url | notes

Status values:
  "applied"   — form submitted successfully
  "dry_run"   — dry-run completed, not submitted
  "skipped"   — score < threshold
  "error"     — exception in any pipeline stage

Environment variables required:
  GOOGLE_SA_JSON   — service account JSON (stringified)
  GOOGLE_SHEET_ID  — Google Sheet ID (from URL)

Optional:
  TELEGRAM_BOT_TOKEN  — Bot API token
  TELEGRAM_CHAT_ID    — Target chat ID
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
import requests
import gspread
from google.oauth2.service_account import Credentials

from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

SHEET_COLUMNS = [
    "run_id", "timestamp", "company", "title", "source",
    "score", "variant_used", "status", "pdf_path", "apply_url", "notes",
]

# Fix #5: cache the processed-URL set so we only hit the Sheets API once per process
_PROCESSED_URLS_CACHE: set[str] | None = None
_PROCESSED_URLS_CACHE_DRY_RUN: bool | None = None

# Buffer for the daily Telegram digest
_DIGEST_BUFFER = {
    "applied_tailored": [],
    "applied_base": [],
    "dry_run_success": [],
    "requires_captcha": [],
    "outreach_drafts": [],
    "failed": [],
    "skipped": [],
}

_COMPANY_HISTORY_CACHE: dict[str, list[datetime]] | None = None


# ── Google Sheets ─────────────────────────────────────────────────────────────

_WORKSHEET_CACHE: gspread.Worksheet | None = None

def _get_sheet():
    """
    Authenticate with gspread and return the target worksheet (cached per process).
    Raises EnvironmentError if credentials are missing.
    """
    global _WORKSHEET_CACHE
    if _WORKSHEET_CACHE is not None:
        return _WORKSHEET_CACHE

    sheet_id = os.environ.get("GOOGLE_SHEET_ID")
    sa_json = os.environ.get("GOOGLE_SA_JSON")

    if not sheet_id or not sa_json:
        raise EnvironmentError("GOOGLE_SHEET_ID or GOOGLE_SA_JSON is missing")

    try:
        creds_dict = json.loads(sa_json)
        creds = Credentials.from_service_account_info(
            creds_dict,
            scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
        client = gspread.authorize(creds)
        _WORKSHEET_CACHE = client.open_by_key(sheet_id).sheet1
        return _WORKSHEET_CACHE
    except Exception as e:
        raise EnvironmentError(f"Failed to authenticate with Google Sheets: {e}")


def append_row(row: dict) -> None:
    """
    Append a single result row to the Google Sheet.
    row must contain keys matching SHEET_COLUMNS (missing keys default to "").
    """
    sheet = _get_sheet()
    row_values = [str(row.get(col, "")) for col in SHEET_COLUMNS]
    sheet.append_row(row_values)
    logger.info("Successfully appended row %s to Google Sheet", row.get("run_id"))

def get_processed_urls(include_dry_run: bool | None = None) -> set[str]:
    """
    Fetch apply_urls from the Google Sheet that should not be re-processed.
    Includes terminal success/skip/closed/captcha statuses plus URLs that have
    failed >= 3 times. Dry-run rows are excluded during live runs so tested jobs
    can still be applied to for real, and included during dry-runs.

    Result is cached in _PROCESSED_URLS_CACHE so the Sheet is only read once
    per Python process (for a given dry-run mode).
    """
    global _PROCESSED_URLS_CACHE, _PROCESSED_URLS_CACHE_DRY_RUN
    if include_dry_run is None:
        include_dry_run = os.environ.get("DRY_RUN", "false").lower() == "true"

    if _PROCESSED_URLS_CACHE is not None and _PROCESSED_URLS_CACHE_DRY_RUN == include_dry_run:
        return _PROCESSED_URLS_CACHE

    TERMINAL_STATUSES = {
        "apply_success_tailored",
        "apply_success_base",
        "apply_success",   # legacy — keep for backward compat
        "skipped_low_score",
        "skipped_unsupported_ats",
        "skipped_unsupported",
        "skipped_senior_role",
        "skipped_geo_knockout",
        "job_closed",
        "requires_captcha",
        "interview_scheduled",
        "oa_received",
        "application_received",
        "rejection",
    }
    if include_dry_run:
        TERMINAL_STATUSES.add("dry_run_success")

    try:
        sheet = _get_sheet()
        all_rows = sheet.get_all_records()

        # Calculate failure / challenge counts per URL (retry up to 3 times before locking out)
        failure_counts = {}
        for r in all_rows:
            if r.get("status") in ("apply_failed", "apply_error", "requires_captcha"):
                url = str(r.get("apply_url", "")).strip()
                if url:
                    failure_counts[url] = failure_counts.get(url, 0) + 1

        _PROCESSED_URLS_CACHE = set()
        _PROCESSED_URLS_CACHE_DRY_RUN = include_dry_run

        for r in all_rows:
            url = str(r.get("apply_url", "")).strip()
            if not url:
                continue

            # Treat as processed if terminal status OR failed >= 3 times
            if r.get("status") in TERMINAL_STATUSES or failure_counts.get(url, 0) >= 3:
                _PROCESSED_URLS_CACHE.add(url)

        logger.info(
            "Loaded %d URLs from Google Sheets (cached, include_dry_run=%s).",
            len(_PROCESSED_URLS_CACHE),
            include_dry_run,
        )
        return _PROCESSED_URLS_CACHE
    except Exception as e:
        logger.warning("Failed to fetch processed URLs from Google Sheets: %s", e)
        return set()


def normalize_company_name(company: str) -> str:
    """Normalize company string for cooldown deduplication across job boards."""
    s = re.sub(r"[^a-z0-9]+", " ", (company or "").lower()).strip()
    return s


def get_company_apply_history(days: int = 30) -> dict[str, list[datetime]]:
    """
    Return {normalized_company: [submission_datetimes_utc]} from Google Sheets
    for active/submitted application statuses within the last `days` days.
    Cached per process; returns {} cleanly when credentials are missing.
    """
    global _COMPANY_HISTORY_CACHE
    if _COMPANY_HISTORY_CACHE is not None and days == 30:
        return _COMPANY_HISTORY_CACHE

    active_statuses = {
        "apply_success_tailored",
        "apply_success_base",
        "apply_success",
        "application_received",
        "oa_received",
        "interview_scheduled",
    }
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    history: dict[str, list[datetime]] = {}

    try:
        sheet = _get_sheet()
        all_rows = sheet.get_all_records()
        for r in all_rows:
            if r.get("status") not in active_statuses:
                continue
            comp_key = normalize_company_name(str(r.get("company", "")))
            if not comp_key:
                continue
            ts_raw = str(r.get("timestamp", "")).strip()
            if not ts_raw:
                continue
            try:
                ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                ts_utc = ts.astimezone(timezone.utc)
            except ValueError:
                continue
            if ts_utc >= cutoff:
                history.setdefault(comp_key, []).append(ts_utc)

        if days == 30:
            _COMPANY_HISTORY_CACHE = history
        return history
    except Exception as e:
        logger.warning("Failed to fetch company apply history from Google Sheets: %s", e)
        return {}


def check_company_cooldown(
    company: str,
    history: dict[str, list[datetime]] | None = None,
    run_counts: dict[str, int] | None = None,
    cooldown_days: int = 14,
    max_per_30d: int = 2,
) -> tuple[bool, str]:
    """Return (on_cooldown, reason) if `company` exceeded cooldown spacing or 30-day cap."""
    comp_key = normalize_company_name(company)
    if not comp_key:
        return False, ""

    in_run = (run_counts or {}).get(comp_key, 0)
    timestamps = list((history or {}).get(comp_key, []))
    total_recent = len(timestamps) + in_run

    if in_run >= 1:
        return True, f"Already processed {company} earlier in this run (1/run burst guard)"
    if total_recent >= max_per_30d:
        return True, f"Reached {total_recent}/{max_per_30d} applications to {company} in last 30 days"
    if timestamps and cooldown_days > 0:
        latest = max(timestamps)
        days_ago = (datetime.now(timezone.utc) - latest).total_seconds() / 86400.0
        if days_ago < cooldown_days:
            return True, f"Last applied to {company} {days_ago:.1f}d ago (< {cooldown_days}d cooldown)"

    return False, ""


def record_outreach_for_digest(
    company: str,
    title: str,
    recipient: str = "",
    gmail_draft_saved: bool = False,
) -> None:
    """Buffer an outreach draft notice into the daily Telegram digest."""
    _DIGEST_BUFFER.setdefault("outreach_drafts", []).append({
        "company": company,
        "title": title,
        "recipient": recipient,
        "gmail_draft_saved": gmail_draft_saved,
    })




# ── Telegram ──────────────────────────────────────────────────────────────────

def notify_telegram(message: str) -> None:
    """
    Send a Telegram message. Fails silently if TELEGRAM_BOT_TOKEN is not set.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return
    
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML"
    }
    
    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
    except Exception as e:
        logger.warning("Failed to send Telegram notification: %s", e)


def notify_captcha(jd: dict, apply_url: str, score: int | None = None) -> None:
    """Send an immediate high-priority Telegram alert when an application encounters a captcha challenge."""
    company = html.escape(str(jd.get("company", "Unknown Company")))
    title = html.escape(str(jd.get("title", "Unknown Title")))
    effective_score = jd.get("score", 0) if score is None else score
    safe_url = html.escape(str(apply_url), quote=True)
    msg = (
        f"⚠️ <b>Captcha Challenge Encountered</b>\n\n"
        f"<b>Role:</b> {title}\n"
        f"<b>Company:</b> {company}\n"
        f"<b>Score:</b> {effective_score}\n\n"
        f"👉 <a href='{safe_url}'>Click here to complete application manually</a>"
    )
    notify_telegram(msg)


def send_daily_digest() -> None:
    """
    Format and send the buffered daily digest to Telegram.
    Called once at the end of the pipeline run.
    """
    total = sum(len(lst) for lst in _DIGEST_BUFFER.values())
    if total == 0:
        return

    dry_runs = _DIGEST_BUFFER.get("dry_run_success", [])
    outreach_list = _DIGEST_BUFFER.get("outreach_drafts", [])
    msg = f"<b>Job Application Pipeline Complete</b> 🏁\n\n"
    if dry_runs:
        msg += f"🧪 Dry Run (Forms Filled): {len(dry_runs)}\n"
    msg += f"🟢 Applied (tailored): {len(_DIGEST_BUFFER.get('applied_tailored', []))}\n"
    msg += f"🟡 Applied (base): {len(_DIGEST_BUFFER.get('applied_base', []))}\n"
    if outreach_list:
        msg += f"✉️ Outreach Drafts Prepared: {len(outreach_list)}\n"
    if _DIGEST_BUFFER.get("requires_captcha"):
        msg += f"⚠️ Requires Captcha: {len(_DIGEST_BUFFER['requires_captcha'])}\n"
    msg += f"🔴 Failed: {len(_DIGEST_BUFFER.get('failed', []))}\n"
    msg += f"⏭️ Skipped: {len(_DIGEST_BUFFER.get('skipped', []))}\n\n"

    if dry_runs:
        msg += "<b>🧪 Dry Run Forms Filled:</b>\n"
        for d in dry_runs[:5]:
            comp = html.escape(str(d.get("company", "")))
            title = html.escape(str(d.get("title", "")))
            score = d.get("score", 0)
            msg += f"• {comp} — {title} (Score: {score})\n"
        if len(dry_runs) > 5:
            msg += f"<i>...and {len(dry_runs) - 5} more</i>\n"
        msg += "\n"

    if outreach_list:
        msg += "<b>Cold Outreach Drafts (Review &amp; Send):</b>\n"
        for o in outreach_list[:5]:
            comp = html.escape(str(o.get("company", "")))
            title = html.escape(str(o.get("title", "")))
            rec = html.escape(str(o.get("recipient", "") or "lookup on LinkedIn/website"))
            loc_tag = "Gmail Drafts" if o.get("gmail_draft_saved") else "output/outreach"
            msg += f"• {comp} — {title} → {rec} ({loc_tag})\n"
        msg += "\n"

    if _DIGEST_BUFFER["requires_captcha"]:
        msg += "<b>Jobs Requiring Captcha:</b>\n"
        for c in _DIGEST_BUFFER["requires_captcha"][:5]:
            comp = html.escape(str(c.get("company", "")))
            title = html.escape(str(c.get("title", "")))
            url = html.escape(str(c.get("apply_url", "")), quote=True)
            msg += f"• {comp} - {title} (<a href='{url}'>Apply Manually</a>)\n"
        msg += "\n"
    
    if _DIGEST_BUFFER['failed']:
        msg += "<b>Failed Apps:</b>\n"
        # Only show up to 5 failures to keep message length sane
        for f in _DIGEST_BUFFER['failed'][:5]:
            comp = html.escape(str(f.get("company", "")))
            title = html.escape(str(f.get("title", "")))
            url = html.escape(str(f.get("url", "")), quote=True)
            msg += f"• {comp} - {title} (<a href='{url}'>Link</a>)\n"
        if len(_DIGEST_BUFFER['failed']) > 5:
            msg += f"<i>...and {len(_DIGEST_BUFFER['failed']) - 5} more</i>\n"

    notify_telegram(msg)



# ── Public helpers ────────────────────────────────────────────────────────────

def log_result(
    jd: dict,
    score: int,
    variant_used: str,
    status: str,
    pdf_path: str = "",
    notes: str = "",
) -> None:
    """
    Log a pipeline result to Google Sheets and send a Telegram summary.
    Called by the pipeline orchestrator after each JD is processed.
    """
    # Fix #16: use 12 hex chars (up from 8) to lower collision probability
    run_id = str(uuid.uuid4())[:12]
    row = {
        "run_id": run_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "company": jd.get("company", ""),
        "title": jd.get("title", ""),
        "source": jd.get("source", ""),
        "score": score,
        "variant_used": variant_used,
        "status": status,
        "pdf_path": pdf_path,
        "apply_url": jd.get("apply_url", ""),
        "notes": notes,
    }
    try:
        append_row(row)
    except (NotImplementedError, EnvironmentError) as e:
        logger.warning("log_result: %s, printing to stdout", e)
        print(json.dumps(row))
    except Exception as e:
        logger.error("log_result: failed to append row: %s", e)
        print(json.dumps(row))

    # Buffer for Telegram digest instead of sending instantly
    if status == "apply_success_tailored":
        _DIGEST_BUFFER["applied_tailored"].append(row)
    elif status == "apply_success_base":
        _DIGEST_BUFFER["applied_base"].append(row)
    elif status == "dry_run_success":
        _DIGEST_BUFFER["dry_run_success"].append(row)
    elif status == "requires_captcha":
        _DIGEST_BUFFER["requires_captcha"].append(row)
        try:
            notify_captcha(jd, jd.get("apply_url", ""), score=score)
        except Exception as e:
            logger.warning("Failed to send immediate captcha notification: %s", e)
    elif status in (
        "skipped_low_score", "skipped_senior_role", "skipped_unsupported_ats",
        "skipped_unsupported", "skipped_geo_knockout", "skipped_company_cooldown", "job_closed",
    ):
        _DIGEST_BUFFER["skipped"].append(row)
    elif status in ("apply_failed", "apply_error", "render_error"):
        # Add URL to failed jobs for easy manual apply
        _DIGEST_BUFFER["failed"].append({
            "company": jd.get("company", "Unknown"),
            "title": jd.get("title", "Unknown"),
            "url": jd.get("apply_url", "")
        })


def log_failure(source: str, error: Exception) -> None:
    """
    Log a scraper/pipeline failure to Google Sheets (status=error) and stderr.
    Must never raise — called inside except blocks.
    """
    logger.error("FAILURE [%s]: %s", source, error, exc_info=True)
    row = {
        "run_id": str(uuid.uuid4())[:8],
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "status": "error",
        "notes": str(error),
    }
    try:
        append_row(row)
    except Exception:
        pass  # Best-effort — do not let logging crash the pipeline
