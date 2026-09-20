"""
auto_apply_ats.py — ATS Form Auto-Filler
==========================================
Playwright-driven form filler for ATS platforms. Detects the ATS from the
apply_url domain and routes to the appropriate flow.

Supported ATS platforms:
  Domain                    | Flow
  --------------------------|------------------
  boards.greenhouse.io      | Greenhouse (legacy)
  job-boards.greenhouse.io  | Greenhouse (React/modern)
  jobs.lever.co             | Lever
  apply.lever.co            | Lever (apply subdomain)
  jobs.ashbyhq.com          | Ashby

Non-negotiable rules (from Agents.md):
  - ONLY fill forms on the above ATS platforms.
  - --dry-run flag: fill all fields but do NOT click submit.
  - LinkedIn/Naukri apply is OUT OF SCOPE — do not add.
  - Every selector timeout: 20 seconds → raises ATSTimeoutError on miss.

Entry point:
  apply(jd: dict, pdf_path: str, dry_run: bool = False) -> tuple[bool, str]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
from playwright_stealth import Stealth

from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

SELECTOR_TIMEOUT_MS = 20_000   # 20 s — bumped from 15 to give React forms more time
PAGE_LOAD_TIMEOUT_MS = 45_000  # 45 s for full page navigation

USER_PROFILE = {
    "first_name": "Kushagra",
    "last_name": "Singh Negi",
    "full_name": "Kushagra Singh Negi",
    "email": "kushagrasinghnegi9@gmail.com",
    "phone": "9521693663",
    "phone_international": "+919521693663",
    "linkedin": "https://www.linkedin.com/in/kushh01",
    "github": "https://github.com/Kushagra001",
    "portfolio": "https://www.stack-form.dev/",
}


# ── Exceptions ────────────────────────────────────────────────────────────────

class ATSTimeoutError(Exception):
    """Raised when a Playwright selector exceeds SELECTOR_TIMEOUT_MS."""


class UnsupportedATSError(Exception):
    """Raised when the apply_url domain does not match any known ATS."""


# ── ATS detection ─────────────────────────────────────────────────────────────

_ATS_DOMAIN_MAP = {
    "greenhouse.io":  "greenhouse",
    "lever.co":       "lever",
    "ashbyhq.com":    "ashby",
    "remotive.com":   "remotive",
    "remoteok.com":   "remoteok",
}


def detect_ats(apply_url: str) -> str:
    """
    Return the ATS name for the given apply_url.
    Raises UnsupportedATSError if the domain is not in _ATS_DOMAIN_MAP.
    """
    host = urlparse(apply_url).hostname or ""
    for domain, name in _ATS_DOMAIN_MAP.items():
        if domain in host:
            return name

    # Support custom Greenhouse domains (e.g. stripe.com) that embed gh_jid=
    if "gh_jid=" in apply_url or "greenhouse" in apply_url:
        return "greenhouse"

    raise UnsupportedATSError(f"Unsupported ATS domain: {host}")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _fill_social_fields(page, field_keyword: str, value: str) -> None:
    """
    Safely fill a social URL field (linkedin, github, portfolio, website).
    Fix: Explicitly restrict to text/url types to avoid the "cannot fill checkbox" crash
    that occurs when a GDPR consent checkbox has 'linkedin' in its name attribute.
    """
    loc = page.locator(
        f"input[type='text'][name*='{field_keyword}' i], "
        f"input[type='url'][name*='{field_keyword}' i], "
        f"input:not([type='checkbox']):not([type='radio']):not([type='hidden'])[name*='{field_keyword}' i]"
    )
    if loc.count() > 0:
        loc.first.fill(value)


# ── ATS flows (Playwright) ────────────────────────────────────────────────────

def _apply_greenhouse(page, jd: dict, pdf_path: str, dry_run: bool, cover_letter: str = "") -> bool:
    """Fill and optionally submit a Greenhouse application form.

    Handles both legacy boards (boards.greenhouse.io) and modern React-rendered
    boards (job-boards.greenhouse.io) which have a different DOM structure.
    """
    logger.info("Starting Greenhouse flow...")

    current_url = page.url
    is_modern_board = "job-boards.greenhouse.io" in current_url

    if is_modern_board:
        # Modern React GH board — wait for any visible name/email input
        logger.info("Detected modern Greenhouse board (job-boards.greenhouse.io)")
        page.wait_for_selector(
            "input[autocomplete='given-name'], input#first_name, "
            "input[name='first_name'], input[type='text']",
            timeout=SELECTOR_TIMEOUT_MS,
        )
    else:
        # Legacy Greenhouse board
        page.wait_for_selector(
            "form#application-form, form#application, "
            "form[action*='applications'], div#application",
            timeout=SELECTOR_TIMEOUT_MS,
        )

    first_name = page.locator(
        "input[autocomplete='given-name'], input#first_name, input[name='first_name']"
    )
    if first_name.count() > 0:
        first_name.first.fill(USER_PROFILE["first_name"])

    last_name = page.locator(
        "input[autocomplete='family-name'], input#last_name, input[name='last_name']"
    )
    if last_name.count() > 0:
        last_name.first.fill(USER_PROFILE["last_name"])

    email = page.locator(
        "input[autocomplete='email'], input#email, input[name='email']"
    )
    if email.count() > 0:
        email.first.fill(USER_PROFILE["email"])

    phone = page.locator(
        "input[autocomplete='tel'], input#phone, input[name='phone']"
    )
    if phone.count() > 0:
        phone.first.fill(USER_PROFILE["phone_international"])

    resume_input = page.locator(
        "input[type='file'][data-source='resume'], "
        "input[type='file'][name='resume'], "
        "input[type='file']"
    )
    if resume_input.count() > 0:
        resume_input.first.set_input_files(pdf_path)

    # Fix: use _fill_social_fields to avoid checkbox crash
    _fill_social_fields(page, "linkedin", USER_PROFILE["linkedin"])

    if cover_letter:
        cl_input = page.locator("textarea[name*='cover_letter' i], textarea#cover_letter_text, textarea[name*='comments' i]")
        if cl_input.count() > 0:
            cl_input.first.fill(cover_letter)

    if not dry_run:
        logger.info("Submitting Greenhouse application...")
        submit = page.locator("button#submit_app, button[type='submit']")
        submit.first.click()
        page.wait_for_load_state("networkidle")
    else:
        logger.info("--dry-run: Skipped submit click.")

    return True


def _apply_lever(page, jd: dict, pdf_path: str, dry_run: bool, cover_letter: str = "") -> bool:
    """Fill and optionally submit a Lever application form.

    Handles both jobs.lever.co (listing page → apply button → form) and
    apply.lever.co (direct form URL) structures.
    """
    logger.info("Starting Lever flow...")

    current_url = page.url

    # Click "Apply" button only if we're on the listing page, not the form
    if "apply.lever.co" not in current_url:
        apply_btn = page.locator("a.template-btn-submit")
        if apply_btn.count() > 0 and apply_btn.first.is_visible():
            apply_btn.first.click()
            page.wait_for_load_state("domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)

    # Wait for form — works on both jobs.lever.co and apply.lever.co
    page.wait_for_selector(
        "form#application-form, "
        "form.application-form, "
        "form[data-qa='application-form']",
        timeout=SELECTOR_TIMEOUT_MS,
    )

    name_input = page.locator("input[name='name']")
    if name_input.count() > 0:
        name_input.first.fill(USER_PROFILE["full_name"])

    email_input = page.locator("input[name='email']")
    if email_input.count() > 0:
        email_input.first.fill(USER_PROFILE["email"])

    phone_input = page.locator("input[name='phone']")
    if phone_input.count() > 0:
        phone_input.first.fill(USER_PROFILE["phone"])

    resume_input = page.locator(
        "input[type='file'][name='resume'], input[type='file']"
    )
    if resume_input.count() > 0:
        resume_input.first.set_input_files(pdf_path)

    _fill_social_fields(page, "LinkedIn", USER_PROFILE["linkedin"])
    _fill_social_fields(page, "GitHub", USER_PROFILE["github"])
    _fill_social_fields(page, "Portfolio", USER_PROFILE["portfolio"])

    if cover_letter:
        cl_input = page.locator("textarea[name*='comments' i], textarea[name*='cover' i]")
        if cl_input.count() > 0:
            cl_input.first.fill(cover_letter)

    if not dry_run:
        logger.info("Submitting Lever application...")
        # Fix: Lever's actual submit button uses id=btn-submit and data-qa=btn-submit
        # The type is "button" not "submit" — must target by id/data-qa
        submit = page.locator(
            "button#btn-submit, button[data-qa='btn-submit']"
        )
        submit.first.click()
        page.wait_for_load_state("networkidle")
    else:
        logger.info("--dry-run: Skipped submit click.")

    return True


def _apply_ashby(page, jd: dict, pdf_path: str, dry_run: bool, cover_letter: str = "") -> bool:
    """Fill and optionally submit an Ashby application form.

    Ashby is a React SPA. The form renders asynchronously after a button click,
    so we must wait for networkidle before querying the DOM.
    Ashby uses _systemfield_ prefix for core form fields.
    """
    logger.info("Starting Ashby flow...")

    current_url = page.url

    # If URL doesn't already point directly to the application form, click Apply
    if not current_url.rstrip("/").endswith("/application"):
        apply_btn = page.locator(
            "button:has-text('Apply for this job'), "
            "a:has-text('Apply for this job'), "
            "button:has-text('Apply')"
        )
        if apply_btn.count() > 0 and apply_btn.first.is_visible():
            apply_btn.first.click()
            # Critical: wait for the SPA to finish rendering the form
            page.wait_for_load_state("networkidle", timeout=PAGE_LOAD_TIMEOUT_MS)

    # Ashby uses _systemfield_name for the candidate name input
    page.wait_for_selector(
        "input[name='_systemfield_name'], input[name='name']",
        timeout=SELECTOR_TIMEOUT_MS,
    )

    name_input = page.locator(
        "input[name='_systemfield_name'], input[name='name']"
    )
    if name_input.count() > 0:
        name_input.first.fill(USER_PROFILE["full_name"])

    email_input = page.locator(
        "input[name='_systemfield_email'], input[name='email']"
    )
    if email_input.count() > 0:
        email_input.first.fill(USER_PROFILE["email"])

    phone_input = page.locator(
        "input[name='_systemfield_phone'], input[name='phone']"
    )
    if phone_input.count() > 0:
        phone_input.first.fill(USER_PROFILE["phone"])

    # Ashby hides the file input visually; set_input_files still works
    resume_input = page.locator("input[type='file']")
    if resume_input.count() > 0:
        resume_input.first.set_input_files(pdf_path)

    # Fix: narrow social selectors to text/url types only — never checkboxes
    _fill_social_fields(page, "linkedin", USER_PROFILE["linkedin"])
    _fill_social_fields(page, "github", USER_PROFILE["github"])
    _fill_social_fields(page, "portfolio", USER_PROFILE["portfolio"])
    _fill_social_fields(page, "website", USER_PROFILE["portfolio"])

    if cover_letter:
        # Ashby often uses standard names or _systemfield_ cover letter equivalents
        cl_input = page.locator("textarea[name*='coverLetter' i], textarea[name*='comments' i], textarea[name*='message' i]")
        if cl_input.count() > 0:
            cl_input.first.fill(cover_letter)

    if not dry_run:
        logger.info("Submitting Ashby application...")
        page.locator("button[type='submit']").first.click()
        page.wait_for_load_state("networkidle")
    else:
        logger.info("--dry-run: Skipped submit click.")

    return True


def _apply_remotive(page, jd: dict, pdf_path: str, dry_run: bool, cover_letter: str = "") -> bool:
    """
    Remotive listing pages show a 'Apply for this job' button that either:
      a) opens a modal with an embedded ATS form, or
      b) redirects to the company's own ATS page (greenhouse/lever).
    We click the button, wait for navigation, then re-detect the ATS.
    """
    logger.info("Starting Remotive flow...")

    apply_btn = page.locator(
        "a.apply-button, a[data-ga-label='apply'], a:has-text('Apply for this job')"
    )
    if apply_btn.count() == 0:
        logger.warning("Remotive: no apply button found — skipping")
        return False

    with page.expect_popup() as popup_info:
        apply_btn.first.click()
    new_page = popup_info.value
    new_page.wait_for_load_state("domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)

    try:
        ats_name = detect_ats(new_page.url)
    except UnsupportedATSError:
        logger.warning("Remotive redirect did not land on a supported ATS: %s", new_page.url)
        new_page.close()
        return False

    logger.info("Remotive redirected to ATS: %s", ats_name)
    flow_fn = _ATS_FLOW_MAP.get(ats_name)
    if not flow_fn or ats_name in ("remotive", "remoteok"):
        logger.warning("No nested flow for ATS '%s'", ats_name)
        new_page.close()
        return False

    try:
        result = flow_fn(new_page, jd, pdf_path, dry_run, cover_letter)
    finally:
        new_page.close()
    return result


def _apply_remoteok(page, jd: dict, pdf_path: str, dry_run: bool, cover_letter: str = "") -> bool:
    """
    RemoteOK listing pages have an 'Apply Now' button that redirects to the
    company's ATS. We click it and re-detect the ATS on the resulting page.
    """
    logger.info("Starting RemoteOK flow...")

    apply_btn = page.locator(
        "a.button-apply, a:has-text('Apply Now'), a:has-text('Apply')"
    )
    if apply_btn.count() == 0:
        logger.warning("RemoteOK: no apply button found — skipping")
        return False

    with page.expect_popup() as popup_info:
        apply_btn.first.click()
    new_page = popup_info.value
    new_page.wait_for_load_state("domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)

    try:
        ats_name = detect_ats(new_page.url)
    except UnsupportedATSError:
        logger.warning("RemoteOK redirect did not land on a supported ATS: %s", new_page.url)
        new_page.close()
        return False

    logger.info("RemoteOK redirected to ATS: %s", ats_name)
    flow_fn = _ATS_FLOW_MAP.get(ats_name)
    if not flow_fn or ats_name in ("remotive", "remoteok"):
        logger.warning("No nested flow for ATS '%s'", ats_name)
        new_page.close()
        return False

    try:
        result = flow_fn(new_page, jd, pdf_path, dry_run, cover_letter)
    finally:
        new_page.close()
    return result


_ATS_FLOW_MAP = {
    "greenhouse": _apply_greenhouse,
    "lever":      _apply_lever,
    "ashby":      _apply_ashby,
    "remotive":   _apply_remotive,
    "remoteok":   _apply_remoteok,
}


# ── Main entry ────────────────────────────────────────────────────────────────

def apply(jd: dict, pdf_path: str, dry_run: bool = False, cover_letter: str = "") -> tuple[bool, str]:
    """
    Detect ATS and run the appropriate Playwright form-fill flow.

    Args:
        jd:           JD dict (from fetch_jds.py)
        pdf_path:     Absolute path to the tailored resume PDF
        dry_run:      If True, fill fields but do not click submit
        cover_letter: Generated cover letter text to paste into textarea

    Returns:
        (success: bool, notes: str) — notes contains the failure reason on
        failure so the pipeline can surface it in the Google Sheet notes column.
    """
    apply_url = jd.get("apply_url")
    if not apply_url:
        logger.error("No apply_url provided in JD.")
        return False, "No apply_url in JD"

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/125.0.0.0 Safari/537.36"
                )
            )
            page = context.new_page()
            Stealth().apply_stealth_sync(page)

            logger.info("Navigating to %s", apply_url)
            page.goto(apply_url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)

            try:
                ats_name = detect_ats(page.url)
            except UnsupportedATSError as e:
                logger.warning(str(e))
                return False, str(e)

            logger.info("Detected ATS: %s", ats_name)
            flow_fn = _ATS_FLOW_MAP[ats_name]

            try:
                ok = flow_fn(page, jd, pdf_path, dry_run, cover_letter)
                return (ok, "") if ok else (False, "ATS flow returned False")
            except PlaywrightTimeoutError as e:
                msg = f"Timeout filling {ats_name} form: {e}"
                logger.warning(msg)
                return False, msg
            except Exception as e:
                msg = f"Error in {ats_name} flow: {e}"
                logger.error(msg)
                return False, msg
            finally:
                browser.close()
    except Exception as e:
        logger.error("Exception in apply: %s", e)
        return False, str(e)


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description="ATS auto-apply")
    parser.add_argument("jd_json", help="Path to JD JSON file")
    parser.add_argument("pdf_path", help="Path to tailored resume PDF")
    parser.add_argument("--dry-run", action="store_true",
                        help="Fill form but do NOT submit")
    args = parser.parse_args()

    jd = json.loads(Path(args.jd_json).read_text())
    success, notes = apply(jd, args.pdf_path, dry_run=args.dry_run)
    print("Result:", "success" if success else f"failed — {notes}")

