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

import json
import logging
import os
import sys
import uuid
from datetime import datetime, timezone
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

# Buffer for the daily Telegram digest
_DIGEST_BUFFER = {
    "applied_tailored": [],
    "applied_base": [],
    "failed": [],
    "skipped": [],
}


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

def get_processed_urls() -> set[str]:
    """
    Fetch apply_urls from the Google Sheet that were SUCCESSFULLY processed.
    Only caches apply_success_tailored, apply_success_base, and dry_run_success rows.
    apply_failed rows are intentionally excluded so they get retried next run.

    Fix: result is cached in _PROCESSED_URLS_CACHE so the Sheet is only
    read once per Python process, not on every pipeline run.
    """
    global _PROCESSED_URLS_CACHE
    if _PROCESSED_URLS_CACHE is not None:
        return _PROCESSED_URLS_CACHE

    SUCCESSFUL_STATUSES = {
        "apply_success_tailored",
        "apply_success_base",
        "apply_success",   # legacy — keep for backward compat
        "dry_run_success",
    }

    try:
        sheet = _get_sheet()
        all_rows = sheet.get_all_records()
        
        # Calculate failure counts per URL
        failure_counts = {}
        for r in all_rows:
            if r.get("status") in ("apply_failed", "apply_error"):
                url = str(r.get("apply_url", "")).strip()
                if url:
                    failure_counts[url] = failure_counts.get(url, 0) + 1

        _PROCESSED_URLS_CACHE = set()
        
        for r in all_rows:
            url = str(r.get("apply_url", "")).strip()
            if not url:
                continue
            
            # Treat as processed if it succeeded OR if it has failed >= 3 times
            if r.get("status") in SUCCESSFUL_STATUSES or failure_counts.get(url, 0) >= 3:
                _PROCESSED_URLS_CACHE.add(url)
                
        logger.info(
            "Loaded %d URLs from Google Sheets (cached). Succeeded or max retries reached.",
            len(_PROCESSED_URLS_CACHE),
        )
        return _PROCESSED_URLS_CACHE
    except Exception as e:
        logger.warning("Failed to fetch processed URLs from Google Sheets: %s", e)
        return set()




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


def send_daily_digest() -> None:
    """
    Format and send the buffered daily digest to Telegram.
    Called once at the end of the pipeline run.
    """
    total = sum(len(lst) for lst in _DIGEST_BUFFER.values())
    if total == 0:
        return

    msg = f"<b>Job Application Pipeline Complete</b> 🏁\n\n"
    msg += f"🟢 Applied (tailored): {len(_DIGEST_BUFFER['applied_tailored'])}\n"
    msg += f"🟡 Applied (base): {len(_DIGEST_BUFFER['applied_base'])}\n"
    msg += f"🔴 Failed: {len(_DIGEST_BUFFER['failed'])}\n"
    msg += f"⏭️ Skipped: {len(_DIGEST_BUFFER['skipped'])}\n\n"
    
    if _DIGEST_BUFFER['failed']:
        msg += "<b>Failed Apps:</b>\n"
        # Only show up to 5 failures to keep message length sane
        for f in _DIGEST_BUFFER['failed'][:5]:
            msg += f"• {f['company']} - {f['title']} (<a href='{f['url']}'>Link</a>)\n"
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
    elif status == "skipped_low_score":
        _DIGEST_BUFFER["skipped"].append(row)
    elif status in ("apply_failed", "apply_error"):
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
