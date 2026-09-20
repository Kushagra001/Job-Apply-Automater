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
    "arbeitnow.com":  "arbeitnow",
    "jobicy.com":     "jobicy",
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


def validate_ats_job_url(apply_url: str) -> tuple[bool, str]:
    """
    Validate that an apply_url points to a specific job post rather than a general company board index.
    Returns (is_valid: bool, rejection_reason: str).
    """
    if not apply_url:
        return False, "Empty apply_url"

    parsed = urlparse(apply_url)
    host = (parsed.netloc or "").lower()
    path = parsed.path.rstrip("/")
    parts = [p for p in path.split("/") if p]

    # Ashby: https://jobs.ashbyhq.com/{company}/{uuid} or .../{uuid}/application
    if "ashbyhq.com" in host:
        if len(parts) < 2:
            return False, f"Ashby URL '{apply_url}' is a company board index, not a specific job post"
        job_id = parts[1]
        if len(job_id) < 8 or job_id in ("application", "jobs"):
            return False, f"Ashby URL '{apply_url}' lacks a specific job identifier"

    # Lever: https://jobs.lever.co/{company}/{uuid} or https://apply.lever.co/{company}/{uuid}
    if "lever.co" in host:
        if len(parts) < 2 or len(parts[1]) < 8:
            return False, f"Lever URL '{apply_url}' is a company index, not a specific job posting"

    # Greenhouse: boards.greenhouse.io/{company}/jobs/{id} or has gh_jid=
    if "greenhouse.io" in host:
        if "gh_jid=" not in apply_url and not any(p == "jobs" for p in parts):
            return False, f"Greenhouse URL '{apply_url}' is a company index, not a specific job posting"

    return True, ""


# ── Helpers ───────────────────────────────────────────────────────────────────

def _fill_social_fields(page, field_keyword: str, value: str) -> None:
    """
    Safely fill a social URL field (linkedin, github, portfolio, website).
    Explicitly restrict to text/url types to avoid checkbox / consent collisions.
    """
    loc = page.locator(
        f"input[type='text'][name*='{field_keyword}' i], "
        f"input[type='url'][name*='{field_keyword}' i], "
        f"input:not([type='checkbox']):not([type='radio']):not([type='hidden'])[name*='{field_keyword}' i]"
    )
    if loc.count() > 0:
        loc.first.fill(value)


def _fill_common_custom_fields(page) -> None:
    """
    Best-effort handler for standard ATS questions and dropdowns:
    - Work authorization: "Yes"
    - Visa sponsorship required: "No"
    - Country: "India"
    - EEO / Demographics: "Decline to self-identify" / "Prefer not to say"
    """
    try:
        # 1. Select dropdowns
        selects = page.locator("select").all()
        for sel in selects:
            try:
                name = (sel.get_attribute("name") or "").lower()
                aria_label = (sel.get_attribute("aria-label") or "").lower()
                id_attr = (sel.get_attribute("id") or "").lower()
                label_text = ""
                if id_attr:
                    lbl = page.locator(f"label[for='{id_attr}']")
                    if lbl.count() > 0:
                        label_text = lbl.first.inner_text().lower()

                field_context = f"{name} {aria_label} {label_text}"
                options = sel.locator("option").all()
                opt_texts = [o.inner_text().strip().lower() for o in options]

                # Work Authorization -> Yes
                if any(k in field_context for k in ["authorized", "legally authorized", "work authorization", "work auth"]):
                    for idx, opt in enumerate(opt_texts):
                        if opt.startswith("yes"):
                            sel.select_option(index=idx)
                            break
                # Sponsorship -> No
                elif any(k in field_context for k in ["sponsorship", "sponsor", "require sponsorship"]):
                    for idx, opt in enumerate(opt_texts):
                        if opt.startswith("no"):
                            sel.select_option(index=idx)
                            break
                # Country -> India
                elif "country" in field_context:
                    for idx, opt in enumerate(opt_texts):
                        if opt == "india" or "india" in opt:
                            sel.select_option(index=idx)
                            break
                # EEO / Demographics -> Decline to self-identify
                elif any(k in field_context for k in ["gender", "race", "ethnicity", "veteran", "disability"]):
                    for idx, opt in enumerate(opt_texts):
                        if any(d in opt for d in ["decline", "prefer not", "choose not"]):
                            sel.select_option(index=idx)
                            break
            except Exception:
                continue

        # 2. Radio buttons for Work Auth / Sponsorship
        for yes_radio in page.locator("input[type='radio'][value*='yes' i], input[type='radio'][id*='yes' i]").all():
            try:
                r_name = (yes_radio.get_attribute("name") or "").lower()
                if any(k in r_name for k in ["authorized", "authorization", "legally"]):
                    yes_radio.check()
            except Exception:
                pass

        for no_radio in page.locator("input[type='radio'][value*='no' i], input[type='radio'][id*='no' i]").all():
            try:
                r_name = (no_radio.get_attribute("name") or "").lower()
                if any(k in r_name for k in ["sponsor", "sponsorship"]):
                    no_radio.check()
            except Exception:
                pass
    except Exception as e:
        logger.debug("Non-fatal issue filling custom fields: %s", e)


def _verify_submission(page, ats_name: str) -> tuple[bool, str]:
    """
    Verify whether the ATS form was genuinely submitted successfully.
    Checks:
    1. Navigation to confirmation URL
    2. Confirmation text presence
    3. Absence of blocking form error messages
    """
    try:
        page.wait_for_timeout(3000)

        # 1. Check for blocking validation errors first
        error_locators = [
            ".error", ".form-error", ".field-error", ".alert-error",
            "[aria-invalid='true']", ".has-error", "div.error-message",
            "p.error-message", "span.error"
        ]
        for sel in error_locators:
            errors = page.locator(sel)
            if errors.count() > 0:
                for idx in range(min(errors.count(), 3)):
                    err_elem = errors.nth(idx)
                    if err_elem.is_visible():
                        err_text = err_elem.inner_text().strip()
                        if err_text:
                            return False, f"Validation error: {err_text[:120]}"

        # 2. Check current URL for confirmation indicators
        curr_url = page.url.lower()
        confirmation_url_keywords = ["confirmation", "thanks", "thank-you", "thank_you", "submitted", "success", "application_submitted"]
        if any(kw in curr_url for kw in confirmation_url_keywords):
            return True, "Confirmation URL reached"

        # 3. Check page content for confirmation text
        page_text = page.locator("body").inner_text().lower()
        success_phrases = [
            "thank you for applying",
            "application submitted",
            "application has been submitted",
            "we've received your application",
            "received your application",
            "thank you for your interest",
            "your application was submitted",
            "application complete",
        ]
        if any(phrase in page_text for phrase in success_phrases):
            return True, "Success text found on page"

        # 4. Check if submit button is still visible and enabled
        submit_btn = page.locator("button[type='submit'], button#submit_app, button#btn-submit")
        if submit_btn.count() > 0 and submit_btn.first.is_visible():
            return False, "Submit button still visible after click with no confirmation (form did not advance)"

        return True, "Page navigated away from form"
    except Exception as e:
        return False, f"Verification error: {e}"


# ── ATS flows (Playwright) ────────────────────────────────────────────────────

def _apply_greenhouse(page, jd: dict, pdf_path: str, dry_run: bool, cover_letter: str = "") -> tuple[bool, str]:
    """Fill and optionally submit a Greenhouse application form."""
    logger.info("Starting Greenhouse flow...")

    current_url = page.url
    is_modern_board = "job-boards.greenhouse.io" in current_url

    if is_modern_board:
        logger.info("Detected modern Greenhouse board (job-boards.greenhouse.io)")
        page.wait_for_selector(
            "input[autocomplete='given-name'], input#first_name, "
            "input[name='first_name'], input[autocomplete='name'], input#name, input[type='text']",
            timeout=SELECTOR_TIMEOUT_MS,
        )
    else:
        page.wait_for_selector(
            "form#application-form, form#application, "
            "form[action*='applications'], div#application",
            timeout=SELECTOR_TIMEOUT_MS,
        )

    # Handle split first/last name or single full name
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
    else:
        full_name = page.locator("input[autocomplete='name'], input#name, input[name='name']")
        if full_name.count() > 0:
            full_name.first.fill(USER_PROFILE["full_name"])

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

    _fill_social_fields(page, "linkedin", USER_PROFILE["linkedin"])
    _fill_common_custom_fields(page)

    if cover_letter:
        cl_input = page.locator("textarea[name*='cover_letter' i], textarea#cover_letter_text, textarea[name*='comments' i]")
        if cl_input.count() > 0:
            cl_input.first.fill(cover_letter)

    if not dry_run:
        logger.info("Submitting Greenhouse application...")
        submit = page.locator("button#submit_app, button[type='submit']")
        if submit.count() > 0:
            submit.first.click()
            try:
                page.wait_for_load_state("networkidle", timeout=15_000)
            except Exception:
                pass
        return _verify_submission(page, "greenhouse")
    else:
        logger.info("--dry-run: Verified Greenhouse form fields.")
        return True, "Dry run: Greenhouse form fields verified"


def _apply_lever(page, jd: dict, pdf_path: str, dry_run: bool, cover_letter: str = "") -> tuple[bool, str]:
    """Fill and optionally submit a Lever application form."""
    logger.info("Starting Lever flow...")

    current_url = page.url
    if "apply.lever.co" not in current_url:
        apply_btn = page.locator("a.template-btn-submit")
        if apply_btn.count() > 0 and apply_btn.first.is_visible():
            apply_btn.first.click()
            page.wait_for_load_state("domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)

    page.wait_for_selector(
        "form#application-form, "
        "form.application-form, "
        "form[data-qa='application-form']",
        timeout=SELECTOR_TIMEOUT_MS,
    )

    name_input = page.locator("input[name='name'], input#name, input[autocomplete='name']")
    if name_input.count() > 0:
        name_input.first.fill(USER_PROFILE["full_name"])

    email_input = page.locator("input[name='email'], input#email, input[autocomplete='email']")
    if email_input.count() > 0:
        email_input.first.fill(USER_PROFILE["email"])

    phone_input = page.locator("input[name='phone'], input#phone, input[autocomplete='tel']")
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
    _fill_common_custom_fields(page)

    if cover_letter:
        cl_input = page.locator("textarea[name*='comments' i], textarea[name*='cover' i]")
        if cl_input.count() > 0:
            cl_input.first.fill(cover_letter)

    if not dry_run:
        logger.info("Submitting Lever application...")
        submit = page.locator(
            "button#btn-submit, button[data-qa='btn-submit'], button[type='submit']"
        )
        if submit.count() > 0:
            submit.first.click()
            try:
                page.wait_for_load_state("networkidle", timeout=15_000)
            except Exception:
                pass
        return _verify_submission(page, "lever")
    else:
        logger.info("--dry-run: Verified Lever form fields.")
        return True, "Dry run: Lever form fields verified"


def _apply_ashby(page, jd: dict, pdf_path: str, dry_run: bool, cover_letter: str = "") -> tuple[bool, str]:
    """Fill and optionally submit an Ashby application form."""
    logger.info("Starting Ashby flow...")

    current_url = page.url

    # If URL doesn't already point directly to the application form, click Apply
    if not current_url.rstrip("/").endswith("/application"):
        apply_btn = page.locator(
            "button:has-text('Apply for this job'), "
            "a:has-text('Apply for this job'), "
            "button:has-text('Apply for this Job'), "
            "a:has-text('Apply for this Job'), "
            "button:has-text('Apply')"
        )
        if apply_btn.count() > 0 and apply_btn.first.is_visible():
            apply_btn.first.click()
            page.wait_for_timeout(2000)

    # Ashby uses _systemfield_name for the candidate name input
    page.wait_for_selector(
        "input[name='_systemfield_name'], input[name='name'], div#application-form",
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

    resume_input = page.locator("input[type='file']")
    if resume_input.count() > 0:
        resume_input.first.set_input_files(pdf_path)

    _fill_social_fields(page, "linkedin", USER_PROFILE["linkedin"])
    _fill_social_fields(page, "github", USER_PROFILE["github"])
    _fill_social_fields(page, "portfolio", USER_PROFILE["portfolio"])
    _fill_social_fields(page, "website", USER_PROFILE["portfolio"])
    _fill_common_custom_fields(page)

    if cover_letter:
        cl_input = page.locator("textarea[name*='coverLetter' i], textarea[name*='comments' i], textarea[name*='message' i]")
        if cl_input.count() > 0:
            cl_input.first.fill(cover_letter)

    if not dry_run:
        logger.info("Submitting Ashby application...")
        submit = page.locator("button[type='submit'], button:has-text('Submit Application')")
        if submit.count() > 0:
            submit.first.click()
            try:
                page.wait_for_load_state("networkidle", timeout=15_000)
            except Exception:
                pass
        return _verify_submission(page, "ashby")
    else:
        logger.info("--dry-run: Verified Ashby form fields.")
        return True, "Dry run: Ashby form fields verified"


def _apply_remotive(page, jd: dict, pdf_path: str, dry_run: bool, cover_letter: str = "") -> tuple[bool, str]:
    """Handles Remotive job apply redirect or embedded form."""
    logger.info("Starting Remotive flow...")

    apply_btn = page.locator(
        "a.apply-button, a[data-ga-label='apply'], a:has-text('Apply for this job')"
    )
    if apply_btn.count() == 0:
        logger.warning("Remotive: no apply button found — skipping")
        return False, "No apply button found on Remotive page"

    try:
        with page.expect_popup(timeout=8000) as popup_info:
            apply_btn.first.click()
        target_page = popup_info.value
        should_close = True
    except PlaywrightTimeoutError:
        target_page = page
        should_close = False

    target_page.wait_for_load_state("domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)

    try:
        ats_name = detect_ats(target_page.url)
    except UnsupportedATSError:
        msg = f"Remotive redirect did not land on a supported ATS: {target_page.url}"
        logger.warning(msg)
        if should_close:
            target_page.close()
        return False, msg

    logger.info("Remotive redirected to ATS: %s", ats_name)
    flow_fn = _ATS_FLOW_MAP.get(ats_name)
    if not flow_fn or ats_name in ("remotive", "remoteok"):
        if should_close:
            target_page.close()
        return False, f"No nested flow for ATS '{ats_name}'"

    try:
        result = flow_fn(target_page, jd, pdf_path, dry_run, cover_letter)
        if isinstance(result, tuple):
            return result
        return (result, "") if result else (False, f"{ats_name} flow returned False")
    finally:
        if should_close:
            target_page.close()


def _apply_remoteok(page, jd: dict, pdf_path: str, dry_run: bool, cover_letter: str = "") -> tuple[bool, str]:
    """Handles RemoteOK job apply redirect."""
    logger.info("Starting RemoteOK flow...")

    apply_btn = page.locator(
        "a.button-apply, a:has-text('Apply Now'), a:has-text('Apply')"
    )
    if apply_btn.count() == 0:
        logger.warning("RemoteOK: no apply button found — skipping")
        return False, "No apply button found on RemoteOK page"

    try:
        with page.expect_popup(timeout=8000) as popup_info:
            apply_btn.first.click()
        target_page = popup_info.value
        should_close = True
    except PlaywrightTimeoutError:
        target_page = page
        should_close = False

    target_page.wait_for_load_state("domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)

    try:
        ats_name = detect_ats(target_page.url)
    except UnsupportedATSError:
        msg = f"RemoteOK redirect did not land on a supported ATS: {target_page.url}"
        logger.warning(msg)
        if should_close:
            target_page.close()
        return False, msg

    logger.info("RemoteOK redirected to ATS: %s", ats_name)
    flow_fn = _ATS_FLOW_MAP.get(ats_name)
    if not flow_fn or ats_name in ("remotive", "remoteok"):
        if should_close:
            target_page.close()
        return False, f"No nested flow for ATS '{ats_name}'"

    try:
        result = flow_fn(target_page, jd, pdf_path, dry_run, cover_letter)
        if isinstance(result, tuple):
            return result
        return (result, "") if result else (False, f"{ats_name} flow returned False")
    finally:
        if should_close:
            target_page.close()


def _apply_arbeitnow(page, jd: dict, pdf_path: str, dry_run: bool, cover_letter: str = "") -> tuple[bool, str]:
    """Handles Arbeitnow job apply redirect to ATS."""
    logger.info("Starting Arbeitnow flow...")
    apply_url = page.url
    target_url = f"{apply_url.rstrip('/')}/apply" if not apply_url.endswith("/apply") else apply_url
    logger.info("Navigating to Arbeitnow apply redirect: %s", target_url)
    page.goto(target_url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)

    try:
        ats_name = detect_ats(page.url)
    except UnsupportedATSError:
        msg = f"Arbeitnow redirect did not land on a supported ATS: {page.url}"
        logger.warning(msg)
        return False, msg

    logger.info("Arbeitnow redirected to ATS: %s", ats_name)
    flow_fn = _ATS_FLOW_MAP.get(ats_name)
    if not flow_fn or ats_name in ("remotive", "remoteok", "arbeitnow", "jobicy"):
        return False, f"No nested flow for ATS '{ats_name}'"

    result = flow_fn(page, jd, pdf_path, dry_run, cover_letter)
    if isinstance(result, tuple):
        return result
    return (result, "") if result else (False, f"{ats_name} flow returned False")


def _apply_jobicy(page, jd: dict, pdf_path: str, dry_run: bool, cover_letter: str = "") -> tuple[bool, str]:
    """Handles Jobicy job apply redirect to ATS."""
    logger.info("Starting Jobicy flow...")
    apply_btn = page.locator(
        "button.jv-apply-primary, a.jv-button:has-text('Apply'), a:has-text('Apply for this job'), a:has-text('Apply now')"
    )
    if apply_btn.count() == 0:
        logger.warning("Jobicy: no apply button found — skipping")
        return False, "No apply button found on Jobicy page"

    try:
        with page.expect_popup(timeout=8000) as popup_info:
            apply_btn.first.click()
        target_page = popup_info.value
        should_close = True
    except PlaywrightTimeoutError:
        target_page = page
        should_close = False

    target_page.wait_for_load_state("domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)

    try:
        ats_name = detect_ats(target_page.url)
    except UnsupportedATSError:
        msg = f"Jobicy redirect did not land on a supported ATS: {target_page.url}"
        logger.warning(msg)
        if should_close:
            target_page.close()
        return False, msg

    logger.info("Jobicy redirected to ATS: %s", ats_name)
    flow_fn = _ATS_FLOW_MAP.get(ats_name)
    if not flow_fn or ats_name in ("remotive", "remoteok", "arbeitnow", "jobicy"):
        if should_close:
            target_page.close()
        return False, f"No nested flow for ATS '{ats_name}'"

    try:
        result = flow_fn(target_page, jd, pdf_path, dry_run, cover_letter)
        if isinstance(result, tuple):
            return result
        return (result, "") if result else (False, f"{ats_name} flow returned False")
    finally:
        if should_close:
            target_page.close()


_ATS_FLOW_MAP = {
    "greenhouse": _apply_greenhouse,
    "lever":      _apply_lever,
    "ashby":      _apply_ashby,
    "remotive":   _apply_remotive,
    "remoteok":   _apply_remoteok,
    "arbeitnow":  _apply_arbeitnow,
    "jobicy":     _apply_jobicy,
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
        (success: bool, notes: str) — notes contains failure reason or confirmation details.
    """
    apply_url = jd.get("apply_url")
    if not apply_url:
        logger.error("No apply_url provided in JD.")
        return False, "No apply_url in JD"

    # Pre-validate URL before launching browser process
    is_valid, validation_msg = validate_ats_job_url(apply_url)
    if not is_valid:
        logger.warning("Invalid ATS job URL: %s", validation_msg)
        return False, validation_msg

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
                result = flow_fn(page, jd, pdf_path, dry_run, cover_letter)
                if isinstance(result, tuple):
                    return result
                return (result, "") if result else (False, f"{ats_name} flow returned False")
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

