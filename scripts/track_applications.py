"""
track_applications.py — Automated Email & Application Status Tracker
=====================================================================
Monitors Gmail for recruiter responses, interview invitations, coding assessments,
and status updates. Automatically updates Google Sheet application rows and
sends high-priority alerts via Telegram.

Classifications:
- interview_scheduled : Recruiter phone screen, technical interview, hiring manager chat
- oa_received         : HackerRank, CodeSignal, Codility, take-home assessment
- application_received: ATS receipt confirmation
- rejection           : Position filled or not moving forward
"""

from __future__ import annotations

import argparse
import email
from email.header import decode_header
import imaplib
import logging
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

# ── Classification Patterns ──────────────────────────────────────────────────

STATUS_PATTERNS = {
    "interview_scheduled": [
        re.compile(r"\b(?:schedule|invitation to|invited to)\s+(?:an?\s+)?(?:interview|call|chat|screening)\b", re.I),
        re.compile(r"\b(?:technical|phone|video|hiring manager)\s+(?:interview|screen|chat)\b", re.I),
        re.compile(r"\b(?:would like to invite you to|like to schedule a time to speak|speak with our team)\b", re.I),
        re.compile(r"\b(?:next round of interviews|moving forward in the interview process)\b", re.I),
        re.compile(r"\bcalendly\.com/[^\s]+|hire\.lever\.co/[^\s]+|goodtime\.io/[^\s]+", re.I),
    ],
    "oa_received": [
        re.compile(r"\b(?:online assessment|coding challenge|technical assessment|take-home|technical test)\b", re.I),
        re.compile(r"\b(?:hackerrank|codesignal|codility|byteboard|karat|testgorilla)\b", re.I),
        re.compile(r"\b(?:complete the assessment within|assessment link)\b", re.I),
    ],
    "rejection": [
        re.compile(r"\b(?:unfortunately|not moving forward|pursue other candidates|decided not to move forward)\b", re.I),
        re.compile(r"\b(?:at this time we have decided|decided to proceed with other|not selected for this role)\b", re.I),
        re.compile(r"\b(?:wish you the best in your job search|regret to inform you)\b", re.I),
    ],
    "application_received": [
        re.compile(r"\b(?:thank you for applying|we received your application|application received|application submitted)\b", re.I),
        re.compile(r"\b(?:confirming receipt of your application|thanks for your interest in)\b", re.I),
    ],
}


def classify_email(subject: str, body: str) -> tuple[str | None, float]:
    """
    Classify the status of an application email based on subject and body text.
    Returns (status_label, confidence_score: 0.0 - 1.0).
    Evaluation order prioritizes positive signals (interview > oa > rejection > received).
    """
    full_text = f"{subject}\n{body}".lower()

    # 1. Interview Invitations (Highest priority)
    for pat in STATUS_PATTERNS["interview_scheduled"]:
        if pat.search(full_text):
            return "interview_scheduled", 0.95

    # 2. Online Assessments / Coding Challenges
    for pat in STATUS_PATTERNS["oa_received"]:
        if pat.search(full_text):
            return "oa_received", 0.90

    # 3. Rejections
    for pat in STATUS_PATTERNS["rejection"]:
        if pat.search(full_text):
            return "rejection", 0.85

    # 4. Confirmations of receipt
    for pat in STATUS_PATTERNS["application_received"]:
        if pat.search(full_text):
            return "application_received", 0.80

    return None, 0.0


def decode_str(header_val: str | None) -> str:
    """Decode RFC 2047 email headers."""
    if not header_val:
        return ""
    parts = decode_header(header_val)
    out = []
    for text, encoding in parts:
        if isinstance(text, bytes):
            out.append(text.decode(encoding or "utf-8", errors="replace"))
        else:
            out.append(str(text))
    return " ".join(out)


def extract_email_body(msg: email.message.Message) -> str:
    """Extract plain text or HTML converted to text from an email.message object."""
    body_parts = []
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            content_disposition = str(part.get("Content-Disposition") or "")
            if "attachment" in content_disposition:
                continue
            if content_type == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    body_parts.append(payload.decode(part.get_content_charset() or "utf-8", errors="replace"))
            elif content_type == "text/html" and not body_parts:
                payload = part.get_payload(decode=True)
                if payload:
                    raw_html = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
                    clean = re.sub(r"<[^>]+>", " ", raw_html)
                    clean = re.sub(r"\s+", " ", clean).strip()
                    body_parts.append(clean)
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            raw = payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
            clean = re.sub(r"<[^>]+>", " ", raw)
            clean = re.sub(r"\s+", " ", clean).strip()
            body_parts.append(clean)

    return "\n".join(body_parts)


def update_sheet_status(company: str, new_status: str, notes: str = "", dry_run: bool = False) -> bool:
    """
    Search Google Sheet for the given company and update its status column.
    """
    from scripts.log_and_notify import _get_sheet, SHEET_COLUMNS

    try:
        sheet = _get_sheet()
        records = sheet.get_all_records()
        status_col_idx = SHEET_COLUMNS.index("status") + 1
        notes_col_idx = SHEET_COLUMNS.index("notes") + 1

        company_clean = company.lower().strip()
        matched_row = None

        for idx, r in enumerate(records, start=2):  # row 1 is header
            r_comp = str(r.get("company", "")).lower().strip()
            if r_comp and (r_comp in company_clean or company_clean in r_comp):
                matched_row = idx
                break

        if not matched_row:
            logger.info("No matching row in Google Sheet for company: '%s'", company)
            return False

        if dry_run:
            logger.info("[DRY RUN] Would update row %d (%s) -> status=%s", matched_row, company, new_status)
            return True

        sheet.update_cell(matched_row, status_col_idx, new_status)
        if notes:
            sheet.update_cell(matched_row, notes_col_idx, notes)
        logger.info("Updated Google Sheet row %d for %s: %s", matched_row, company, new_status)
        return True
    except Exception as e:
        logger.warning("Could not update Google Sheet status: %s", e)
        return False


def track_gmail_updates(days_back: int = 7, dry_run: bool = False) -> list[dict]:
    """
    Connect to Gmail via IMAP and scan incoming emails for job application updates.
    """
    user = os.getenv("GMAIL_USER")
    password = os.getenv("GMAIL_APP_PASSWORD")

    if not user or not password:
        logger.warning("GMAIL_USER or GMAIL_APP_PASSWORD not set. Skipping Gmail tracking.")
        return []

    processed_updates = []
    mail = None
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(user, password)
        mail.select("inbox")

        since_date = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime("%d-%b-%Y")
        search_criteria = f'(SINCE "{since_date}")'
        status, message_numbers = mail.search(None, search_criteria)

        if status != "OK" or not message_numbers[0]:
            logger.info("No emails found since %s", since_date)
            return []

        msg_ids = message_numbers[0].split()
        logger.info("Scanning %d recent emails...", len(msg_ids))

        # Check most recent emails (capped at 50 to maintain fast execution)
        for msg_id in reversed(msg_ids[-50:]):
            _, msg_data = mail.fetch(msg_id, "(RFC822)")
            for response_part in msg_data:
                if isinstance(response_part, tuple):
                    raw_email = response_part[1]
                    msg = email.message_from_bytes(raw_email)

                    subject = decode_str(msg.get("Subject"))
                    sender = decode_str(msg.get("From"))
                    body = extract_email_body(msg)

                    status_label, conf = classify_email(subject, body)
                    if not status_label:
                        continue

                    company = ""
                    if "@" in sender:
                        domain = sender.split("@")[-1].split(">")[0].strip()
                        comp_candidate = domain.split(".")[0].replace("-", " ").title()
                        if comp_candidate.lower() not in ("gmail", "yahoo", "outlook", "hotmail", "mail", "greenhouse", "lever", "ashbyhq"):
                            company = comp_candidate
                    if not company:
                        company = subject.split()[0] if subject else "Unknown"

                    logger.info("Detected update: [%s] from %s (Company: %s)", status_label, sender, company)
                    update_record = {
                        "company": company,
                        "subject": subject,
                        "status": status_label,
                        "confidence": conf,
                        "sender": sender,
                        "date": msg.get("Date"),
                    }
                    processed_updates.append(update_record)

                    if company:
                        notes_msg = f"Auto-detected {status_label} via email on {datetime.now(timezone.utc).strftime('%Y-%m-%d')}"
                        update_sheet_status(company, status_label, notes=notes_msg, dry_run=dry_run)

                    if status_label in ("interview_scheduled", "oa_received") and not dry_run:
                        try:
                            from scripts.log_and_notify import notify_telegram
                            alert_icon = "🎉 INTERVIEW ALERT" if status_label == "interview_scheduled" else "📝 CODING ASSESSMENT"
                            msg_text = (
                                f"{alert_icon}\n"
                                f"Company: {company}\n"
                                f"Subject: {subject}\n"
                                f"Status: {status_label}\n"
                                f"From: {sender}"
                            )
                            notify_telegram(msg_text)
                        except Exception as e:
                            logger.warning("Telegram notification failed: %s", e)

        return processed_updates
    except Exception as e:
        logger.error("Gmail tracker encountered error: %s", e)
        return []
    finally:
        if mail:
            try:
                mail.close()
                mail.logout()
            except Exception:
                pass


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    parser = argparse.ArgumentParser(description="Track job applications and email replies via Gmail")
    parser.add_argument("--days", type=int, default=7, help="Number of days back to scan")
    parser.add_argument("--dry-run", action="store_true", help="Inspect without modifying Google Sheets or sending alerts")
    args = parser.parse_args()

    results = track_gmail_updates(days_back=args.days, dry_run=args.dry_run)
    print(f"\nDone. Processed {len(results)} application status updates.")
