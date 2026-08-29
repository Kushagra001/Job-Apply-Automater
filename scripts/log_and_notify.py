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


# ── Google Sheets ─────────────────────────────────────────────────────────────

def _get_sheet():
    """
    Authenticate with gspread and return the target worksheet.
    Raises EnvironmentError if credentials are missing.
    """
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
        sheet = client.open_by_key(sheet_id).sheet1
        return sheet
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
    run_id = str(uuid.uuid4())[:8]
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

    msg = (f"[{status.upper()}] {jd.get('company')} — {jd.get('title')}\n"
           f"Score: {score} | Variant: {variant_used}")
    try:
        notify_telegram(msg)
    except NotImplementedError:
        pass  # Not implemented yet — skip silently


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
