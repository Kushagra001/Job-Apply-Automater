"""
auto_apply_ats.py — ATS Form Auto-Filler
==========================================
Playwright-driven form filler for ATS platforms. Detects the ATS from the
apply_url domain and routes to the appropriate flow.

Supported ATS platforms:
  Domain               | Flow
  ---------------------|------------------
  boards.greenhouse.io | Greenhouse
  jobs.lever.co        | Lever
  jobs.ashbyhq.com     | Ashby

Non-negotiable rules (from Agents.md):
  - ONLY fill forms on the above ATS platforms.
  - --dry-run flag: fill all fields but do NOT click submit.
  - LinkedIn/Naukri apply is OUT OF SCOPE — do not add.
  - Every selector timeout: 15 seconds → raises ATSTimeoutError on miss.

Entry point:
  apply(jd: dict, pdf_path: str, dry_run: bool = False) -> bool

Returns True on successful submission (or dry-run completion), False on failure.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

SELECTOR_TIMEOUT_MS = 15_000  # 15 s hard limit on every Playwright selector wait
# Fix #14: page loads on ATS sites can take 30-45s; separate timeout from selector waits
PAGE_LOAD_TIMEOUT_MS = 45_000  # 45 s for full page navigation

USER_PROFILE = {
    "first_name": "Kushagra",
    "last_name": "Singh Negi",
    "full_name": "Kushagra Singh Negi",
    "email": "kushagrasinghnegi9@gmail.com",
    "phone": "9521693663",
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
    "greenhouse.io":       "greenhouse",
    "lever.co":            "lever",
    "ashbyhq.com":         "ashby",
    "remotive.com":        "remotive",
    "remoteok.com":        "remoteok",
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
    raise UnsupportedATSError(f"Unsupported ATS domain: {host}")


# ── ATS flows (Playwright) ────────────────────────────────────────────────────

def _apply_greenhouse(page, jd: dict, pdf_path: str, dry_run: bool) -> bool:
    """Fill and optionally submit a Greenhouse application form."""
    logger.info("Starting Greenhouse flow...")

    # Fix: broaden selector to handle both legacy id-based forms and modern
    # React-rendered Greenhouse boards that omit the id attribute entirely.
    page.wait_for_selector(
        "form#application-form, form#application, form[action*='applications'], div#application",
        timeout=SELECTOR_TIMEOUT_MS,
    )

    # Fix: page.fill() does NOT support comma-separated CSS selectors.
    # Use locator().first so Playwright evaluates the OR and picks the first match.
    first_name = page.locator('input[autocomplete="given-name"], input#first_name')
    if first_name.count() > 0:
        first_name.first.fill(USER_PROFILE["first_name"])

    last_name = page.locator('input[autocomplete="family-name"], input#last_name')
    if last_name.count() > 0:
        last_name.first.fill(USER_PROFILE["last_name"])

    email = page.locator('input[autocomplete="email"], input#email')
    if email.count() > 0:
        email.first.fill(USER_PROFILE["email"])

    phone = page.locator('input[autocomplete="tel"], input#phone')
    if phone.count() > 0:
        phone.first.fill(USER_PROFILE["phone"])

    resume_input = page.locator('input[type="file"][data-source="resume"], input[type="file"][name="resume"]')
    if resume_input.count() > 0:
        resume_input.first.set_input_files(pdf_path)

    linkedin_input = page.locator('input[autocomplete="custom-question-linkedin-profile"], input[name*="linkedin" i]')
    if linkedin_input.count() > 0:
        linkedin_input.first.fill(USER_PROFILE["linkedin"])

    if not dry_run:
        logger.info("Submitting Greenhouse application...")
        page.click('button#submit_app, button[type="submit"]')
        page.wait_for_load_state('networkidle')
    else:
        logger.info("--dry-run: Skipped submit click.")

    return True


def _apply_lever(page, jd: dict, pdf_path: str, dry_run: bool) -> bool:
    """Fill and optionally submit a Lever application form."""
    logger.info("Starting Lever flow...")
    
    apply_btn = page.locator('a.template-btn-submit')
    if apply_btn.count() > 0 and apply_btn.first.is_visible():
        apply_btn.first.click()
        
    page.wait_for_selector("form#application-form", timeout=SELECTOR_TIMEOUT_MS)
    
    page.fill('input[name="name"]', USER_PROFILE["full_name"])
    page.fill('input[name="email"]', USER_PROFILE["email"])
    page.fill('input[name="phone"]', USER_PROFILE["phone"])
    
    resume_input = page.locator('input[type="file"][name="resume"]')
    if resume_input.count() > 0:
        resume_input.first.set_input_files(pdf_path)
        
    linkedin_input = page.locator('input[name="urls[LinkedIn]"]')
    if linkedin_input.count() > 0:
        linkedin_input.fill(USER_PROFILE["linkedin"])
        
    github_input = page.locator('input[name="urls[GitHub]"]')
    if github_input.count() > 0:
        github_input.fill(USER_PROFILE["github"])

    portfolio_input = page.locator('input[name="urls[Portfolio]"]')
    if portfolio_input.count() > 0:
        portfolio_input.fill(USER_PROFILE["portfolio"])
        
    if not dry_run:
        logger.info("Submitting Lever application...")
        page.click('button.postings-btn-submit')
        page.wait_for_load_state('networkidle')
    else:
        logger.info("--dry-run: Skipped submit click.")
        
    return True


def _apply_ashby(page, jd: dict, pdf_path: str, dry_run: bool) -> bool:
    """Fill and optionally submit an Ashby application form."""
    logger.info("Starting Ashby flow...")
    
    # Ashby sometimes has an "Apply for this job" button before the form
    apply_btn = page.locator('button:has-text("Apply for this job"), a:has-text("Apply for this job")')
    if apply_btn.count() > 0 and apply_btn.first.is_visible():
        apply_btn.first.click()
        
    page.wait_for_selector('input[name="name"]', timeout=SELECTOR_TIMEOUT_MS)
    
    page.fill('input[name="name"]', USER_PROFILE["full_name"])
    page.fill('input[name="email"]', USER_PROFILE["email"])
    
    phone_input = page.locator('input[name="phone"]')
    if phone_input.count() > 0:
        phone_input.fill(USER_PROFILE["phone"])
        
    resume_input = page.locator('input[type="file"]')
    if resume_input.count() > 0:
        resume_input.first.set_input_files(pdf_path)
        
    # Check for common social fields
    linkedin_input = page.locator('input[name*="linkedin" i]')
    if linkedin_input.count() > 0:
        linkedin_input.fill(USER_PROFILE["linkedin"])
        
    github_input = page.locator('input[name*="github" i]')
    if github_input.count() > 0:
        github_input.fill(USER_PROFILE["github"])
        
    portfolio_input = page.locator('input[name*="portfolio" i], input[name*="website" i]')
    if portfolio_input.count() > 0:
        portfolio_input.fill(USER_PROFILE["portfolio"])
        
    if not dry_run:
        logger.info("Submitting Ashby application...")
        # Ashby submit buttons usually say "Submit Application"
        page.click('button[type="submit"]')
        page.wait_for_load_state('networkidle')
    else:
        logger.info("--dry-run: Skipped submit click.")
        
    return True



def _apply_remotive(page, jd: dict, pdf_path: str, dry_run: bool) -> bool:
    """
    Remotive listing pages show a 'Apply for this job' button that either:
      a) opens a modal with an embedded ATS form, or
      b) redirects to the company's own ATS page (greenhouse/lever).
    We click the button, wait for navigation, then re-detect the ATS.
    """
    logger.info("Starting Remotive flow...")

    # Click the primary apply button
    apply_btn = page.locator('a.apply-button, a[data-ga-label="apply"], a:has-text("Apply for this job")')
    if apply_btn.count() == 0:
        logger.warning("Remotive: no apply button found — skipping")
        return False

    with page.expect_popup() as popup_info:
        apply_btn.first.click()
    new_page = popup_info.value
    new_page.wait_for_load_state("domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)

    # Re-detect ATS on the new page
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
        result = flow_fn(new_page, jd, pdf_path, dry_run)
    finally:
        new_page.close()
    return result


def _apply_remoteok(page, jd: dict, pdf_path: str, dry_run: bool) -> bool:
    """
    RemoteOK listing pages have an 'Apply Now' button that redirects to the
    company's ATS. We click it and re-detect the ATS on the resulting page.
    """
    logger.info("Starting RemoteOK flow...")

    apply_btn = page.locator('a.button-apply, a:has-text("Apply Now"), a:has-text("Apply")')
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
        result = flow_fn(new_page, jd, pdf_path, dry_run)
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

def apply(jd: dict, pdf_path: str, dry_run: bool = False) -> tuple[bool, str]:
    """
    Detect ATS and run the appropriate Playwright form-fill flow.

    Args:
        jd:       JD dict (from fetch_jds.py)
        pdf_path: Absolute path to the tailored resume PDF
        dry_run:  If True, fill fields but do not click submit

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
            context = browser.new_context()
            page = context.new_page()

            logger.info("Navigating to %s", apply_url)
            # Fix #14: use the longer PAGE_LOAD_TIMEOUT_MS for page navigation
            page.goto(apply_url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)

            try:
                ats_name = detect_ats(page.url)
            except UnsupportedATSError as e:
                logger.warning(str(e))
                return False, str(e)

            logger.info("Detected ATS: %s", ats_name)
            flow_fn = _ATS_FLOW_MAP[ats_name]

            try:
                ok = flow_fn(page, jd, pdf_path, dry_run)
                return ok, "" if ok else "ATS flow returned False"
            except NotImplementedError as e:
                # Fix #4: Workday/Ashby raise NotImplementedError — surface it clearly
                logger.warning("ATS flow not implemented for '%s': %s", ats_name, e)
                return False, f"Not implemented: {e}"
            except PlaywrightTimeoutError as e:
                msg = f"Timeout filling {ats_name} form: {e}"
                raise ATSTimeoutError(msg)
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
    success = apply(jd, args.pdf_path, dry_run=args.dry_run)
    print("Result:", "success" if success else "failed")
