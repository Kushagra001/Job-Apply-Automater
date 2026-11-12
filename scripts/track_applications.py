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
import html
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
        re.compile(r"\b(?:next round of interviews|advancing to the next round|moving forward in the interview process)\b", re.I),
        re.compile(r"\bcalendly\.com/[^\s]+|hire\.lever\.co/[^\s]+|goodtime\.io/[^\s]+", re.I),
    ],
    "oa_received": [
        re.compile(r"\b(?:online assessment|coding challenge|technical assessment|take-home|technical test)\b", re.I),
        re.compile(r"\b(?:hackerrank|codesignal|codility|byteboard|karat|testgorilla)\b", re.I),
        re.compile(r"\b(?:complete the assessment within|assessment link)\b", re.I),
    ],
    "rejection": [
        re.compile(r"\b(?:unfortunately|not moving forward|won't be moving forward|will not be moving forward|not to move forward|pursue other candidates|decided not to move forward)\b", re.I),
        re.compile(r"\b(?:at this time we have decided|decided to proceed with other|not selected for this role|moved forward with other candidates)\b", re.I),
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
    Checks explicit rejections first so post-interview or post-OA rejection emails
    mentioning 'technical interview' or 'not moving forward' are never misclassified
    as interview invitations.
    """
    full_text = f"{subject}\n{body}".lower()

    # 1. Explicit Rejections (prevents post-interview rejection false positives)
    for pat in STATUS_PATTERNS["rejection"]:
        if pat.search(full_text):
            return "rejection", 0.85

    # 2. Interview Invitations
    for pat in STATUS_PATTERNS["interview_scheduled"]:
        if pat.search(full_text):
            return "interview_scheduled", 0.95

    # 3. Online Assessments / Coding Challenges
    for pat in STATUS_PATTERNS["oa_received"]:
        if pat.search(full_text):
            return "oa_received", 0.90

    # 4. Confirmations of receipt
    for pat in STATUS_PATTERNS["application_received"]:
        if pat.search(full_text):
            return "application_received", 0.80

    return None, 0.0


_GENERIC_EMAIL_DOMAINS = frozenset({
    "gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "icloud.com",
    "greenhouse.io", "greenhouse-mail.io", "lever.co", "ashbyhq.com",
    "workablemail.com", "bamboohr.com", "smartrecruiters.com", "icims.com",
    "myworkday.com", "rippling.com", "dover.com",
})
_GENERIC_SENDER_WORDS = frozenset({
    "no-reply", "noreply", "no reply", "donotreply", "do-not-reply",
    "notifications", "notification", "recruiting", "talent", "careers",
    "jobs", "hiring", "hr", "team", "people", "mail", "email", "app", "us", "eu", "hire",
    "greenhouse", "lever", "ashby", "ashbyhq", "workable", "unknown",
})
_GENERIC_SUBJECT_PREFIXES = frozenset({
    "re", "fwd", "fw", "update", "next", "invitation", "your", "thank", "thanks", "application",
})


def extract_company_from_email(
    sender: str,
    subject: str,
    body: str = "",
    known_companies: list[str] | None = None,
) -> str:
    """
    Extract target company name from an ATS or recruiter email.
    Priority: sender display name -> subject patterns -> known Sheet companies in
    sender/subject -> non-ATS sender domain -> longest exact-cased known company in body.
    """
    sender = sender or ""
    subject = subject or ""

    # 1. Sender display name before <email>
    if "<" in sender:
        display = sender.split("<", 1)[0].strip().strip('"').strip("'")
        display = re.sub(r"\s+via\s+(?:ashby|greenhouse|lever|workable|rippling).*$", "", display, flags=re.IGNORECASE).strip()
        display = re.sub(r"\s+(?:recruiting|talent(?:\s+acquisition)?|careers|hiring|jobs|team|hr)\s*$", "", display, flags=re.IGNORECASE).strip()
        if display and len(display) >= 2 and display.lower() not in _GENERIC_SENDER_WORDS:
            return display

    # 2. Subject company phrasing (e.g. "Thank you for applying to Stripe!", "Interview with Acme AI")
    subj_patterns = (
        re.compile(r"\b(?:applying\s+to|application\s+(?:at|to|with|for)|interview\s+(?:at|with)|from)\s+([A-Z][A-Za-z0-9&.\- ]{1,40}?)(?:\s+for\s+|\s*[-–—:|!,.]|$)"),
        re.compile(r"^([A-Z][A-Za-z0-9&.\- ]{1,35}?)\s*(?:[-–—:|])\s+"),
    )
    for pat in subj_patterns:
        m = pat.search(subject)
        if m:
            candidate = m.group(1).strip()
            first_word = candidate.split()[0].lower() if candidate else ""
            if candidate and candidate.lower() not in _GENERIC_SENDER_WORDS and first_word not in _GENERIC_SUBJECT_PREFIXES:
                return candidate

    # 3. Word-boundary match against known Sheet companies in sender/subject ONLY
    if known_companies:
        header_haystack = f"{sender}\n{subject}"
        for comp in sorted({c.strip() for c in known_companies if c and len(c.strip()) >= 3}, key=len, reverse=True):
            if re.search(r"(?<!\w)" + re.escape(comp) + r"(?!\w)", header_haystack, re.IGNORECASE):
                return comp

    # 4. Company domain fallback (skip ATS mailers & generic subdomains like mail./us./hire.)
    if "@" in sender:
        domain = sender.split("@")[-1].split(">")[0].strip().lower()
        is_generic_domain = any(domain == gd or domain.endswith("." + gd) for gd in _GENERIC_EMAIL_DOMAINS)
        if not is_generic_domain and "." in domain:
            labels = [lbl for lbl in domain.split(".") if lbl]
            # Strip trailing TLD (handle .co.uk / .co.in simple 2-part ccTLD too)
            if len(labels) >= 3 and labels[-2] in ("co", "com", "org", "net") and len(labels[-1]) == 2:
                comp_label = labels[-3]
            elif len(labels) >= 2:
                comp_label = labels[-2]
            else:
                comp_label = labels[0]
            if comp_label and comp_label not in _GENERIC_SENDER_WORDS:
                return comp_label.replace("-", " ").title()

    # 5. Known Sheet companies in email body (exact case for short words so "fly"/"us" don't false-match)
    if known_companies and body:
        body_slice = (body or "")[:600]
        for comp in sorted({c.strip() for c in known_companies if c and len(c.strip()) >= 4}, key=len, reverse=True):
            flags = 0 if len(comp) <= 5 else re.IGNORECASE
            if re.search(r"(?<!\w)" + re.escape(comp) + r"(?!\w)", body_slice, flags):
                return comp

    return "Unknown"


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
                    clean = re.sub(r"<[^>]+>", " ", html.unescape(raw_html))
                    clean = re.sub(r"\s+", " ", clean).strip()
                    body_parts.append(clean)
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            raw = payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
            clean = re.sub(r"<[^>]+>", " ", html.unescape(raw))
            clean = re.sub(r"\s+", " ", clean).strip()
            body_parts.append(clean)

    return "\n".join(body_parts)


_ACTIVE_APPLICATION_STATUSES = frozenset({
    "apply_success_tailored",
    "apply_success_base",
    "apply_success",
    "application_received",
    "oa_received",
    "interview_scheduled",
    "dry_run_success",
})
_STATUS_PROGRESSION = {
    "skipped_low_score": 0,
    "skipped_senior_role": 0,
    "skipped_unsupported_ats": 0,
    "skipped_unsupported": 0,
    "skipped_geo_knockout": 0,
    "skipped_company_cooldown": 0,
    "job_closed": 0,
    "requires_captcha": 1,
    "apply_failed": 1,
    "apply_error": 1,
    "render_error": 1,
    "dry_run_success": 2,
    "apply_success": 3,
    "apply_success_base": 3,
    "apply_success_tailored": 3,
    "application_received": 4,
    "oa_received": 5,
    "interview_scheduled": 6,
    "rejection": 6,
}


def update_sheet_status(company: str, new_status: str, notes: str = "", dry_run: bool = False) -> bool:
    """
    Search Google Sheet for the best-matching active row for the given company and
    update its status column without downgrading higher-stage outcomes.
    """
    from scripts.log_and_notify import _get_sheet, SHEET_COLUMNS

    company_clean = (company or "").lower().strip()
    if not company_clean or company_clean == "unknown":
        return False

    try:
        sheet = _get_sheet()
        records = sheet.get_all_records()
        status_col_idx = SHEET_COLUMNS.index("status") + 1
        notes_col_idx = SHEET_COLUMNS.index("notes") + 1

        candidates = []
        for idx, r in enumerate(records, start=2):  # row 1 is header
            r_comp = str(r.get("company", "")).lower().strip()
            if not r_comp:
                continue

            exact = (r_comp == company_clean)
            boundary_match = False
            if not exact and len(company_clean) >= 3 and len(r_comp) >= 3:
                boundary_match = bool(
                    re.search(r"(?<!\w)" + re.escape(company_clean) + r"(?!\w)", r_comp)
                    or re.search(r"(?<!\w)" + re.escape(r_comp) + r"(?!\w)", company_clean)
                )
            if not (exact or boundary_match):
                continue

            cur_status = str(r.get("status", "")).strip()
            is_active = 1 if cur_status in _ACTIVE_APPLICATION_STATUSES else (0 if cur_status.startswith("skipped") or cur_status == "job_closed" else 0)
            score_rank = (1 if exact else 0, is_active, _STATUS_PROGRESSION.get(cur_status, 0), idx)
            candidates.append((score_rank, idx, cur_status))

        if not candidates:
            logger.info("No matching row in Google Sheet for company: '%s'", company)
            return False

        candidates.sort(key=lambda item: item[0], reverse=True)
        _, matched_row, current_status = candidates[0]

        # Do not let older/lower-stage emails overwrite equal or higher-stage outcomes
        cur_rank = _STATUS_PROGRESSION.get(current_status, 0)
        new_rank = _STATUS_PROGRESSION.get(new_status, 0)
        if cur_rank >= 4 and (new_rank < cur_rank or new_status == current_status):
            logger.info("Skipping downgrade on row %d (%s): %s -> %s", matched_row, company, current_status, new_status)
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

    known_companies: list[str] = []
    try:
        from scripts.log_and_notify import _get_sheet
        known_companies = [
            str(r.get("company", "")).strip()
            for r in _get_sheet().get_all_records()
            if r.get("company")
        ]
    except Exception:
        pass

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

                    company = extract_company_from_email(sender, subject, body, known_companies=known_companies)

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

                    if company and company != "Unknown":
                        notes_msg = f"Auto-detected {status_label} via email on {datetime.now(timezone.utc).strftime('%Y-%m-%d')}"
                        update_sheet_status(company, status_label, notes=notes_msg, dry_run=dry_run)

                    if status_label in ("interview_scheduled", "oa_received") and not dry_run:
                        try:
                            from scripts.log_and_notify import notify_telegram
                            alert_icon = "🎉 INTERVIEW ALERT" if status_label == "interview_scheduled" else "📝 CODING ASSESSMENT"
                            msg_text = (
                                f"<b>{alert_icon}</b>\n"
                                f"<b>Company:</b> {html.escape(company)}\n"
                                f"<b>Subject:</b> {html.escape(subject)}\n"
                                f"<b>Status:</b> {html.escape(status_label)}\n"
                                f"<b>From:</b> {html.escape(sender)}"
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
