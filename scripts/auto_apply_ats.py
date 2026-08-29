"""
auto_apply_ats.py — ATS Form Auto-Filler
==========================================
Playwright-driven form filler for ATS platforms. Detects the ATS from the
apply_url domain and routes to the appropriate flow.

Supported ATS platforms:
  Domain               | Flow
  ---------------------|------------------
  greenhouse.io        | Greenhouse
  lever.co             | Lever
  myworkdayjobs.com    | Workday
  ashbyhq.com          | Ashby

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

USER_PROFILE = {
    "first_name": "Kushagra",
    "last_name": "Singh Negi",
    "full_name": "Kushagra Singh Negi",
    "email": "kushagrasinghnegi9@gmail.com",
    "phone": "9521693663",
    "linkedin": "https://linkedin.com",
    "github": "https://github.com",
    "portfolio": "https://portfolio.com",
}


# ── Exceptions ────────────────────────────────────────────────────────────────

class ATSTimeoutError(Exception):
    """Raised when a Playwright selector exceeds SELECTOR_TIMEOUT_MS."""


class UnsupportedATSError(Exception):
    """Raised when the apply_url domain does not match any known ATS."""


# ── ATS detection ─────────────────────────────────────────────────────────────

_ATS_DOMAIN_MAP = {
    "greenhouse.io": "greenhouse",
    "lever.co": "lever",
    "myworkdayjobs.com": "workday",
    "ashbyhq.com": "ashby",
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
    page.wait_for_selector("form#application-form, form#application", timeout=SELECTOR_TIMEOUT_MS)
    
    page.fill('input[autocomplete="given-name"], input#first_name', USER_PROFILE["first_name"])
    page.fill('input[autocomplete="family-name"], input#last_name', USER_PROFILE["last_name"])
    page.fill('input[autocomplete="email"], input#email', USER_PROFILE["email"])
    page.fill('input[autocomplete="tel"], input#phone', USER_PROFILE["phone"])
    
    resume_input = page.locator('input[type="file"][data-source="resume"], input[type="file"][name="resume"]')
    if resume_input.count() > 0:
        resume_input.first.set_input_files(pdf_path)
    
    linkedin_input = page.locator('input[autocomplete="custom-question-linkedin-profile"]')
    if linkedin_input.count() > 0:
        linkedin_input.fill(USER_PROFILE["linkedin"])
        
    if not dry_run:
        logger.info("Submitting Greenhouse application...")
        page.click('button#submit_app')
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


def _apply_workday(page, jd: dict, pdf_path: str, dry_run: bool) -> bool:
    """Fill and optionally submit a Workday application form."""
    # TODO: implement
    raise NotImplementedError("Workday flow not yet implemented")


def _apply_ashby(page, jd: dict, pdf_path: str, dry_run: bool) -> bool:
    """Fill and optionally submit an Ashby application form."""
    # TODO: implement
    raise NotImplementedError("Ashby flow not yet implemented")


_ATS_FLOW_MAP = {
    "greenhouse": _apply_greenhouse,
    "lever": _apply_lever,
    "workday": _apply_workday,
    "ashby": _apply_ashby,
}


# ── Main entry ────────────────────────────────────────────────────────────────

def apply(jd: dict, pdf_path: str, dry_run: bool = False) -> bool:
    """
    Detect ATS and run the appropriate Playwright form-fill flow.

    Args:
        jd:       JD dict (from fetch_jds.py)
        pdf_path: Absolute path to the tailored resume PDF
        dry_run:  If True, fill fields but do not click submit

    Returns:
        True on success/dry-run completion, False on handled failure.
    """
    apply_url = jd.get("apply_url")
    if not apply_url:
        logger.error("No apply_url provided in JD.")
        return False
        
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context()
            page = context.new_page()
            
            logger.info("Navigating to %s", apply_url)
            page.goto(apply_url, wait_until="domcontentloaded", timeout=SELECTOR_TIMEOUT_MS)
            
            try:
                ats_name = detect_ats(page.url)
            except UnsupportedATSError as e:
                logger.warning(str(e))
                return False
                
            logger.info("Detected ATS: %s", ats_name)
            flow_fn = _ATS_FLOW_MAP[ats_name]
            
            try:
                return flow_fn(page, jd, pdf_path, dry_run)
            except PlaywrightTimeoutError as e:
                raise ATSTimeoutError(f"Timeout filling {ats_name} form: {e}")
            finally:
                browser.close()
    except Exception as e:
        logger.error("Exception in apply: %s", e)
        return False


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
