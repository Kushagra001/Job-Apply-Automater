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
import re
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

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

CANDIDATE_QA_PROFILE = {
    "full_name": USER_PROFILE["full_name"],
    "email": USER_PROFILE["email"],
    "phone": USER_PROFILE["phone_international"],
    "location": "Jaipur, Rajasthan, India",
    "willing_to_relocate": "Yes, open to relocation within India or full-time remote/EOR work globally",
    "work_authorization": "Authorized to work in India; available globally as remote full-time contractor or via EOR (does NOT hold US/UK/Canada/EU citizenship or local work visa)",
    "visa_sponsorship": "Requires sponsorship for onsite US/UK/CA/EU relocation; no sponsorship needed for India roles or remote contractor/EOR positions",
    "education": "Master of Computer Applications (MCA), Vellore Institute of Technology, Bhopal, CGPA: 9.03/10 (07/2023-07/2025); Bachelor of Computer Applications (BCA), JECRC University, Jaipur, CGPA: 8.20/10 (07/2019-07/2022)",
    "experience_years": "1-2 years of professional software engineering & development experience (early career / junior)",
    "notice_period": "Immediate / 0 days (available to join immediately)",
    "expected_salary": "Open / Negotiable — aligned with company band and market standards",
    "expected_salary_india": os.environ.get("EXPECTED_CTC_INR", "800000 - 1200000 INR per annum (negotiable)"),
    "expected_salary_remote_usd": os.environ.get("EXPECTED_SALARY_USD", "30000") + " USD per annum (negotiable)",
    "linkedin": USER_PROFILE["linkedin"],
    "github": USER_PROFILE["github"],
    "portfolio": USER_PROFILE["portfolio"],
    "core_skills": "Python, JavaScript, TypeScript, React, Next.js, Node.js, Django, Flask, PostgreSQL, MongoDB, Supabase, Docker, Git, REST APIs, Playwright, pytest, automated testing, CI/CD, OpenAI/Groq APIs",
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
    "arbeitnow.com":   "arbeitnow",
    "arbeitnow.co.uk": "arbeitnow",
    "jobicy.com":      "jobicy",
    "himalayas.app":   "himalayas",
}


def detect_ats(apply_url: str) -> str:
    """
    Return the ATS name for the given apply_url.
    Raises UnsupportedATSError if the domain is not in _ATS_DOMAIN_MAP.
    """
    host = (urlparse(apply_url).hostname or "").lower()
    for domain, name in _ATS_DOMAIN_MAP.items():
        if domain in host:
            return name

    # Support regional Arbeitnow ccTLDs (.fr, .nl, .ch, .de, .co.uk, etc.)
    if re.search(r"(?:^|\.)arbeitnow\.[a-z.]+$", host):
        return "arbeitnow"

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
    qs = parse_qs(parsed.query)

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

    # Greenhouse: boards.greenhouse.io/{company}/jobs/{id}, embed/job_app?for=...&token=..., or gh_jid=
    if "greenhouse.io" in host:
        has_token = bool(qs.get("token") and qs["token"][0])
        if "gh_jid=" not in apply_url and not has_token and not any(p == "jobs" for p in parts):
            return False, f"Greenhouse URL '{apply_url}' is a company index, not a specific job posting"

    return True, ""


# ── Helpers ───────────────────────────────────────────────────────────────────

def _human_type(locator, text: str, min_delay_ms: int = 15, max_delay_ms: int = 40) -> None:
    """
    Types text into locator with simulated human keystroke delays (15ms - 40ms)
    instead of instant value injection, evading heuristic bot detection.
    Guards against non-text input types (checkbox, radio, file, submit, button)
    so Playwright never throws 'Input of type checkbox cannot be filled'.
    """
    try:
        input_type = (locator.get_attribute("type") or "text").lower()
        if input_type in ("checkbox", "radio", "file", "button", "submit", "hidden"):
            logger.debug("Skipping _human_type for non-text input type: %s", input_type)
            return

        import random
        locator.focus()
        locator.clear()
        delay = random.randint(min_delay_ms, max_delay_ms)
        locator.press_sequentially(text, delay=delay)
    except Exception:
        try:
            locator.fill(text)
        except Exception as e:
            logger.debug("Defensive fill failed for locator: %s", e)


def _is_job_closed(page) -> tuple[bool, str]:
    """
    Instantly check if an ATS job posting is closed, expired, or returns a 404 SPA state.
    Allows immediate termination (0.5s) instead of waiting for 20s form selector timeouts.
    """
    try:
        title = ((page.title() if hasattr(page, "title") else "") or "").lower()
        if any(w in title for w in ["404", "page not found", "job not found"]):
            return True, f"Job 404 in page title: '{title}'"

        body = page.locator("body")
        if body.count() == 0:
            return False, ""

        body_text = body.inner_text().lower()
        closed_phrases = [
            "the page you requested was not found",
            "page not found",
            "job not found",
            "this job could not be found",
            "we couldn't find this page",
            "the job you are looking for",
            "this job is no longer available",
            "no longer accepting applications",
            "this position has been filled",
            "this listing has expired",
            "this job is closed",
            "this position is no longer open",
            "application is closed",
            "applications are closed",
            "this role has been filled",
            "posting is no longer active",
        ]
        for phrase in closed_phrases:
            if phrase in body_text:
                return True, f"Job closed/unavailable: '{phrase}'"

        return False, ""
    except Exception as e:
        logger.debug("Closed job check error: %s", e)
        return False, ""


def _detect_captcha(page) -> bool:
    """
    Check if the page has an active Cloudflare Turnstile, reCAPTCHA, hCaptcha,
    or blocking security challenge.
    """
    try:
        title = ((page.title() if hasattr(page, "title") else "") or "").lower()
        if any(w in title for w in ["just a moment", "attention required", "security check", "challenge"]):
            logger.warning("Active challenge page detected via title: '%s'", title)
            return True

        # Check all frames for explicit interactive challenge URLs (e.g. Lever hCaptcha challenge popup)
        if hasattr(page, "frames"):
            for f in page.frames:
                f_url = (getattr(f, "url", "") or "").lower()
                if any(k in f_url for k in [
                    "hcaptcha.html#frame=challenge",
                    "challenges.cloudflare.com",
                ]):
                    logger.warning("Active challenge frame detected via URL: %s", getattr(f, "url", "")[:80])
                    return True

        captcha_selectors = [
            "iframe[src*='challenges.cloudflare.com']",
            "iframe[src*='recaptcha']",
            "iframe[src*='hcaptcha']",
            "iframe[src*='arkoselabs']",
            ".cf-turnstile", "#cf-turnstile",
            ".g-recaptcha", ".h-captcha",
            "div[id*='captcha' i]", "div[class*='captcha' i]",
        ]
        for sel in captcha_selectors:
            loc = page.locator(sel)
            if loc.count() > 0:
                for el in loc.all():
                    try:
                        if el.is_visible():
                            box = el.bounding_box()
                            if box and box["width"] > 20 and box["height"] > 20:
                                # 1. Off-screen check: Google reCAPTCHA Enterprise keeps background evaluation
                                # bframe at top: -10000px, left: -10000px or off-screen.
                                x = box.get("x", 0)
                                y = box.get("y", 0)
                                if x < -100 or y < -100:
                                    continue
                                if (y + box["height"] <= 0) or (x + box["width"] <= 0):
                                    continue
                                # 2. Computed style check: ensure container is actually visible (not visibility: hidden, display: none, opacity: 0)
                                is_hidden = False
                                try:
                                    is_hidden = el.evaluate("""(e) => {
                                        function check(node) {
                                            if (!node || node.nodeType !== 1) return false;
                                            const s = window.getComputedStyle(node);
                                            return s.visibility === 'hidden' || s.display === 'none' || parseFloat(s.opacity || '1') === 0;
                                        }
                                        let cur = e;
                                        for (let i = 0; i < 4; i++) {
                                            if (!cur) break;
                                            if (check(cur)) return true;
                                            cur = cur.parentElement;
                                        }
                                        return false;
                                    }""")
                                except Exception:
                                    is_hidden = False
                                if is_hidden is True:
                                    continue
                                logger.warning(
                                    "Active visible captcha detected via selector: %s (%dx%d at x=%d, y=%d)",
                                    sel, int(box["width"]), int(box["height"]), int(x), int(y)
                                )
                                return True
                    except Exception:
                        continue

        return False
    except Exception as e:
        logger.debug("Captcha detection non-fatal error: %s", e)
        return False


def _fill_social_fields(page, field_keyword: str, value: str) -> None:
    """
    Safely fill a social URL field (linkedin, github, portfolio, website).
    Supports legacy [name*=...] inputs AND modern React [id*=...], [aria-label*=...],
    and <label for=...> associated inputs while excluding checkbox/radio/hidden inputs.
    """
    loc = page.locator(
        f"input[type='text'][name*='{field_keyword}' i], "
        f"input[type='url'][name*='{field_keyword}' i], "
        f"input:not([type='checkbox']):not([type='radio']):not([type='hidden'])[name*='{field_keyword}' i], "
        f"input:not([type='checkbox']):not([type='radio']):not([type='hidden'])[id*='{field_keyword}' i], "
        f"input:not([type='checkbox']):not([type='radio']):not([type='hidden'])[aria-label*='{field_keyword}' i]"
    )
    if loc.count() > 0:
        _human_type(loc.first, value)
        return

    # Modern React Greenhouse fallback: match <label> inner text -> target input via 'for' attribute
    try:
        labels = page.locator("label").all()
        if isinstance(labels, list):
            kw_low = field_keyword.lower()
            for lbl in labels:
                txt = (lbl.inner_text() or "").lower()
                if kw_low in txt:
                    target_id = lbl.get_attribute("for")
                    if target_id:
                        target = page.locator(f"input#{target_id}:not([type='checkbox']):not([type='radio']):not([type='hidden'])")
                        if target.count() > 0:
                            _human_type(target.first, value)
                            return
    except Exception:
        pass


_FOREIGN_AUTH_COUNTRIES_RE = re.compile(
    r"\b(?:united\s+states|u\.?s\.?a?\.?|usa|canada|uk|united\s+kingdom|european\s+union|eu|australia|germany|singapore|ireland|netherlands)\b",
    re.IGNORECASE,
)
_INDIA_AUTH_RE = re.compile(r"\b(?:india|remote|anywhere|global|worldwide)\b", re.IGNORECASE)


def _get_element_question_context(page, el) -> str:
    """Safely extract combined name, aria-label, associated <label>, and container question text."""
    parts: list[str] = []
    try:
        for attr in ("name", "aria-label", "id"):
            val = el.get_attribute(attr)
            if isinstance(val, str) and val.strip():
                parts.append(val.strip().lower())
        id_attr = el.get_attribute("id")
        if isinstance(id_attr, str) and id_attr.strip():
            lbl = page.locator(f"label[for='{id_attr.strip()}']")
            if lbl.count() > 0:
                txt = lbl.first.inner_text()
                if isinstance(txt, str) and txt.strip():
                    parts.append(txt.strip().lower())
        parent_field = el.locator(
            "xpath=ancestor::*[self::fieldset or contains(@class,'field') or contains(@class,'question') or contains(@class,'form-group')][1]"
        )
        if parent_field.count() > 0:
            ptxt = parent_field.first.inner_text()
            if isinstance(ptxt, str) and ptxt.strip():
                parts.append(ptxt.strip().lower()[:250])
    except Exception:
        pass
    return " ".join(parts)


def _detect_foreign_work_auth_knockout(page) -> tuple[bool, str]:
    """
    Detect mandatory US/UK/Canada/EU legal work-authorization questions on live forms
    where India/remote is not allowed, so we abort cleanly instead of submitting false answers.
    Supports native <select>, <input type='radio'>, and React Select <input role='combobox'>.
    """
    auth_markers = ("authorized", "legally authorized", "right to work", "eligible to work", "work authorization", "work auth")
    try:
        for sel_query in ("select", "input[type='radio']", "input[role='combobox']"):
            elements = page.locator(sel_query).all()
            if not isinstance(elements, list):
                continue
            for el in elements:
                ctx = _get_element_question_context(page, el)
                if not ctx:
                    continue
                if any(k in ctx for k in auth_markers):
                    if _FOREIGN_AUTH_COUNTRIES_RE.search(ctx) and not _INDIA_AUTH_RE.search(ctx):
                        return True, f"Form requires foreign work authorization: {ctx[:100]}"
    except Exception as e:
        logger.debug("Foreign work-auth knockout check non-fatal error: %s", e)
    return False, ""


def _fill_react_comboboxes(page) -> None:
    """
    Handle modern React Select <input role='combobox'> dropdowns (used on job-boards.greenhouse.io
    and embed/job_app forms where native <select> count is 0).
    """
    try:
        combos = page.locator("input[role='combobox']").all()
        if not isinstance(combos, list) or not combos:
            return
        for cb in combos:
            try:
                ctx = _get_element_question_context(page, cb)
                if not ctx:
                    continue
                is_foreign_specific = bool(_FOREIGN_AUTH_COUNTRIES_RE.search(ctx)) and not bool(_INDIA_AUTH_RE.search(ctx))

                desired_prefixes: tuple[str, ...] = ()
                type_hint = ""
                if any(k in ctx for k in ("authorized", "legally authorized", "work authorization", "work auth", "right to work", "eligible to work")):
                    desired_prefixes = ("no",) if is_foreign_specific else ("yes",)
                    type_hint = "No" if is_foreign_specific else "Yes"
                elif any(k in ctx for k in ("sponsorship", "sponsor", "require sponsorship", "visa")):
                    desired_prefixes = ("yes",) if is_foreign_specific else ("no",)
                    type_hint = "Yes" if is_foreign_specific else "No"
                elif "country" in ctx:
                    desired_prefixes = ("india",)
                    type_hint = "India"
                elif any(k in ctx for k in ("gender", "race", "ethnicity", "veteran", "disability")):
                    desired_prefixes = ("decline", "prefer not", "choose not", "i do not wish", "i don't wish")

                if not desired_prefixes:
                    continue

                cb.click(timeout=5_000)
                if hasattr(page, "wait_for_timeout"):
                    page.wait_for_timeout(150)

                def _try_pick_option() -> bool:
                    opts = page.locator("[role='option']").all()
                    if not isinstance(opts, list):
                        return False
                    for opt in opts:
                        txt = (opt.inner_text() or "").strip().lower()
                        if any(txt.startswith(p) or (p in ("india", "decline", "prefer not", "choose not") and p in txt) for p in desired_prefixes):
                            opt.click(timeout=5_000)
                            return True
                    return False

                if not _try_pick_option() and type_hint:
                    cb.fill(type_hint)
                    if hasattr(page, "wait_for_timeout"):
                        page.wait_for_timeout(200)
                    if not _try_pick_option() and hasattr(cb, "press"):
                        cb.press("Escape")
            except Exception:
                continue
    except Exception as e:
        logger.debug("Non-fatal React combobox fill error: %s", e)


def _fill_common_custom_fields(page) -> None:
    """
    Best-effort truthful handler for standard ATS questions and dropdowns:
    - Work authorization: "Yes" ONLY for India or general remote-compatible forms;
      "No" when a form explicitly asks about US/UK/Canada/EU legal authorization.
    - Visa sponsorship required: "No" for India/global contractor; "Yes" when foreign work-visa sponsorship asked explicitly.
    - Country: "India"
    - EEO / Demographics: "Decline to self-identify" / "Prefer not to say"
    """
    try:
        # 1. Select dropdowns
        selects = page.locator("select").all()
        for sel in selects:
            try:
                field_context = _get_element_question_context(page, sel)
                if not field_context:
                    continue
                options = sel.locator("option").all()
                opt_texts = [o.inner_text().strip().lower() for o in options]

                is_foreign_specific = bool(_FOREIGN_AUTH_COUNTRIES_RE.search(field_context)) and not bool(_INDIA_AUTH_RE.search(field_context))

                # Work Authorization -> truthful country check
                if any(k in field_context for k in ["authorized", "legally authorized", "work authorization", "work auth", "right to work", "eligible to work"]):
                    target_prefix = "no" if is_foreign_specific else "yes"
                    for idx, opt in enumerate(opt_texts):
                        if opt.startswith(target_prefix):
                            sel.select_option(index=idx)
                            break
                # Sponsorship -> "Yes" if US/UK/CA/EU sponsorship required, else "No"
                elif any(k in field_context for k in ["sponsorship", "sponsor", "require sponsorship", "visa"]):
                    target_prefix = "yes" if is_foreign_specific else "no"
                    for idx, opt in enumerate(opt_texts):
                        if opt.startswith(target_prefix):
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

        # 1b. Modern React Select <input role="combobox"> controls
        _fill_react_comboboxes(page)

        # 2. Radio buttons for Work Auth / Sponsorship (inspect full question context, not just name)
        for radio in page.locator("input[type='radio']").all():
            try:
                r_ctx = _get_element_question_context(page, radio)
                if not r_ctx:
                    continue
                r_val = (radio.get_attribute("value") or "").strip().lower()
                r_id = (radio.get_attribute("id") or "").strip().lower()
                r_label = ""
                if r_id:
                    lbl = page.locator(f"label[for='{r_id}']")
                    if lbl.count() > 0:
                        raw_l = lbl.first.inner_text()
                        r_label = raw_l.strip().lower() if isinstance(raw_l, str) else ""
                is_yes = r_val.startswith("yes") or "yes" in r_id or r_label.startswith("yes")
                is_no = r_val.startswith("no") or "_no" in r_id or r_id.endswith("no") or r_label.startswith("no")
                if not (is_yes or is_no):
                    continue

                is_foreign_specific = bool(_FOREIGN_AUTH_COUNTRIES_RE.search(r_ctx)) and not bool(_INDIA_AUTH_RE.search(r_ctx))

                if any(k in r_ctx for k in ["authorized", "authorization", "legally", "right to work", "eligible to work"]):
                    if (is_no and is_foreign_specific) or (is_yes and not is_foreign_specific):
                        radio.check()
                elif any(k in r_ctx for k in ["sponsor", "sponsorship"]):
                    if (is_yes and is_foreign_specific) or (is_no and not is_foreign_specific):
                        radio.check()
            except Exception:
                pass

        # 3. Checkboxes for Consent, Privacy Policy, Terms, or Required confirmations
        for cb in page.locator("input[type='checkbox']").all():
            try:
                name = (cb.get_attribute("name") or "").lower()
                id_attr = (cb.get_attribute("id") or "").lower()
                aria_label = (cb.get_attribute("aria-label") or "").lower()
                is_req = cb.get_attribute("required") is not None or cb.get_attribute("aria-required") == "true"

                label_text = ""
                if id_attr:
                    lbl = page.locator(f"label[for='{id_attr}']")
                    if lbl.count() > 0:
                        label_text = lbl.first.inner_text().lower()
                if not label_text:
                    try:
                        parent = cb.locator("xpath=..")
                        if parent.count() > 0:
                            label_text = parent.first.inner_text().lower()
                    except Exception:
                        pass

                cb_context = f"{name} {id_attr} {aria_label} {label_text}"
                consent_keywords = [
                    "agree", "consent", "privacy", "terms", "policy", "acknowledge",
                    "confirm", "authorized", "gdpr", "data processing", "accurate",
                    "declaration", "accept"
                ]
                if is_req or any(k in cb_context for k in consent_keywords):
                    if not cb.is_checked():
                        cb.check()
                        logger.debug("Checked required/consent checkbox: %s", cb_context[:50])
            except Exception:
                pass

        # 4. Button toggles for Yes/No questions (Ashby, modern ATS forms)
        _fill_button_toggles(page)
    except Exception as e:
        logger.debug("Non-fatal issue filling custom fields: %s", e)


def _dismiss_cookie_banner(page) -> None:
    """Dismiss cookie banners or privacy modals that could intercept clicks or overlay submit buttons."""
    try:
        cookie_btns = page.locator(
            "button:has-text('Necessary Only'), button:has-text('Accept All'), "
            "button:has-text('Accept all cookies'), button#onetrust-accept-btn-handler, "
            "button:has-text('I Agree'), button:has-text('Got it')"
        )
        if cookie_btns.count() > 0 and cookie_btns.first.is_visible():
            cookie_btns.first.click(timeout=3_000)
    except Exception:
        pass


def _fill_button_toggles(page) -> None:
    """
    Handle Yes/No button toggles commonly used on Ashby and modern ATS forms.
    Inspects question containers containing Yes/No button pairs and evaluates
    them truthfully based on candidate's location and foreign work-auth context.
    """
    try:
        containers = page.locator("div[class*='container' i], div[class*='Container' i], div[class*='field' i]").all()
        for c in containers:
            try:
                buttons = c.locator("button").all()
                if len(buttons) != 2:
                    continue
                b_texts = [b.inner_text().strip().lower() for b in buttons]
                if set(b_texts) != {"yes", "no"}:
                    continue

                ctx = (c.inner_text() or "").lower()
                is_foreign = bool(_FOREIGN_AUTH_COUNTRIES_RE.search(ctx)) and not bool(_INDIA_AUTH_RE.search(ctx))

                target_option = None
                if any(k in ctx for k in ("authorized", "legally authorized", "right to work", "eligible to work", "work authorization", "work auth")):
                    target_option = "no" if is_foreign else "yes"
                elif any(k in ctx for k in ("sponsorship", "sponsor", "require sponsorship", "visa")):
                    target_option = "yes" if is_foreign else "no"

                if target_option:
                    btn_to_click = buttons[0] if b_texts[0] == target_option else buttons[1]
                    is_pressed = btn_to_click.get_attribute("aria-pressed") == "true" or "_active" in (btn_to_click.get_attribute("class") or "")
                    if not is_pressed:
                        btn_to_click.click()
                        logger.info("Clicked '%s' button toggle for: %s", target_option.upper(), ctx[:60])
            except Exception:
                continue
    except Exception as e:
        logger.debug("Non-fatal issue filling button toggles: %s", e)


def _answer_custom_questions(page, jd: dict, zero_llm_mode: bool = False) -> None:
    """
    Inspects modern ATS application forms (Ashby, Greenhouse, Lever) for unfilled
    custom or mandatory fields (text inputs, textareas, selects) and uses Groq
    to generate concise, truthful answers matching the candidate's profile.
    Skipped immediately when zero_llm_mode is active.
    """
    if zero_llm_mode:
        logger.info("Zero-LLM mode active — skipping Groq custom question autofill.")
        return

    try:
        # 1. Identify unfilled interactive textareas, inputs, and selects via client-side DOM query
        elements_data = page.evaluate("""() => {
            const results = [];
            let counter = 0;

            function getLabel(el) {
                if (el.id) {
                    const l = document.querySelector(`label[for='${el.id}']`);
                    if (l && l.innerText.trim()) return l.innerText.trim();
                }
                const parentLabel = el.closest('label');
                if (parentLabel && parentLabel.innerText.trim()) return parentLabel.innerText.trim();
                const parentField = el.closest('.field, .form-group, div[class*="field"], div[class*="question"], div[data-testid*="question"]');
                if (parentField) {
                    const labelEl = parentField.querySelector('label, h3, h4, span[class*="label"], div[class*="label"], p[class*="label"]');
                    if (labelEl && labelEl.innerText.trim()) return labelEl.innerText.trim();
                }
                return el.getAttribute('aria-label') || el.getAttribute('placeholder') || el.getAttribute('name') || '';
            }

            const standardKeywords = ['first_name', 'last_name', 'email', 'phone', 'resume', 'cv', 'linkedin', 'github', 'portfolio', 'website'];

            // Visible empty textareas
            document.querySelectorAll('textarea').forEach(ta => {
                if (ta.offsetParent !== null && !ta.value.trim()) {
                    const name = (ta.name || '').toLowerCase();
                    const label = getLabel(ta);
                    const labelLower = label.toLowerCase();
                    if (name.includes('cover') || name.includes('letter') || labelLower.includes('cover letter')) return;
                    if (label && label.length > 2) {
                        const qId = 'custom_field_' + (counter++);
                        ta.setAttribute('data-ag-id', qId);
                        results.push({ id: qId, tag: 'textarea', label: label.slice(0, 200) });
                    }
                }
            });

            // Visible empty text/number inputs (skip React Select comboboxes handled by option picker)
            document.querySelectorAll('input[type="text"], input[type="number"], input:not([type])').forEach(inp => {
                if (inp.getAttribute('role') === 'combobox') return;
                if (inp.offsetParent !== null && !inp.value.trim()) {
                    const name = (inp.name || '').toLowerCase();
                    const idAttr = (inp.id || '').toLowerCase();
                    if (standardKeywords.some(k => name.includes(k) || idAttr.includes(k))) return;
                    const label = getLabel(inp);
                    if (label && label.length > 2) {
                        const qId = 'custom_field_' + (counter++);
                        inp.setAttribute('data-ag-id', qId);
                        results.push({ id: qId, tag: 'input', label: label.slice(0, 200) });
                    }
                }
            });

            // Visible unselected dropdowns
            document.querySelectorAll('select').forEach(sel => {
                if (sel.offsetParent !== null) {
                    const name = (sel.name || '').toLowerCase();
                    if (standardKeywords.some(k => name.includes(k))) return;
                    const selectedOpt = sel.options[sel.selectedIndex];
                    const val = selectedOpt ? selectedOpt.value.trim() : '';
                    const text = selectedOpt ? selectedOpt.text.trim().toLowerCase() : '';
                    if (!val || text.includes('select') || text.includes('choose')) {
                        const label = getLabel(sel);
                        const options = Array.from(sel.options).map(o => o.text.trim()).filter(t => t && !t.toLowerCase().includes('select'));
                        if (label && options.length > 0) {
                            const qId = 'custom_field_' + (counter++);
                            sel.setAttribute('data-ag-id', qId);
                            results.push({ id: qId, tag: 'select', label: label.slice(0, 200), options: options.slice(0, 10) });
                        }
                    }
                }
            });

            return results;
        }""")

        if not elements_data:
            logger.debug("No unfilled custom questions detected on page.")
            return

        logger.info("Found %d unfilled custom questions: %s", len(elements_data), [e["label"][:40] for e in elements_data])

        # 2. Query Groq with candidate profile in a single fast call
        from groq import Groq
        groq_client = Groq()
        questions_payload = [
            {"id": e["id"], "type": e["tag"], "question": e["label"], "options": e.get("options", [])}
            for e in elements_data
        ]

        prompt = f"""
You are an expert candidate assistant filling out ATS job application questions.
Candidate Profile:
{json.dumps(CANDIDATE_QA_PROFILE, indent=2)}

Job Details:
Title: {jd.get('title', '')}
Company: {jd.get('company', '')}
Location: {jd.get('location', '')}
Remote: {jd.get('remote', False)}

Questions to answer:
{json.dumps(questions_payload, indent=2)}

Instructions:
1. Provide accurate, truthful, professional, and concise answers for each question based strictly on the candidate's profile.
2. For textarea questions (e.g. 'Why do you want to work here?'), write 2-3 specific sentences linking a real candidate project (Stackform, BidCopy, GYMYAK, Medical-Chatbot, or Automated Job Pipeline) to this role — avoid generic filler.
3. For select/dropdown questions, return EXACTLY one of the provided options that best matches.
4. For salary / compensation questions:
   - If location is India: state "900000" (or "8-12 LPA INR, negotiable" if non-numeric text) unless another INR range fits better.
   - If remote outside India: state "Open / Negotiable — aligned with market compensation band" for text fields, or "{os.environ.get('EXPECTED_SALARY_USD', '30000')}" if numeric-only.
5. Never falsely claim US/UK/Canada/EU citizenship or local work authorization if asked specifically about those jurisdictions; answer truthfully that candidate is based in India (available remotely via contractor/EOR or for India roles).
6. Return JSON format mapping question ID to the answer string:
   {{"custom_field_0": "answer", ...}}
7. Keep all text answers concise and direct (under 2 sentences per question) to ensure the JSON document completes cleanly.
"""
        try:
            from scripts.score_and_pick import FALLBACK_MODELS as _SHARED_FALLBACK_MODELS, _clean_llm_json
        except Exception:
            _SHARED_FALLBACK_MODELS = ["openai/gpt-oss-20b", "openai/gpt-oss-120b", "qwen/qwen3.8-27b"]
            _clean_llm_json = lambda s: (s or "{}").strip()

        forced_model = os.environ.get("GROQ_MODEL")
        models_to_try = [forced_model] if forced_model else _SHARED_FALLBACK_MODELS
        answers = {}
        for m in models_to_try:
            try:
                resp = groq_client.chat.completions.create(
                    model=m,
                    messages=[
                        {"role": "system", "content": "You output JSON only."},
                        {"role": "user", "content": prompt}
                    ],
                    response_format={"type": "json_object"},
                    temperature=0.1,
                    max_tokens=800,
                )
                answers = json.loads(_clean_llm_json(resp.choices[0].message.content))
                if isinstance(answers, list) and answers:
                    answers = answers[0]
                break
            except Exception as exc:
                logger.warning("Groq custom QA call with model %s failed: %s", m, exc)

        # 3. Fill the answers into the DOM elements
        for item in elements_data:
            q_id = item["id"]
            ans = answers.get(q_id)
            if not ans:
                continue

            tag = item["tag"]
            loc = page.locator(f"[data-ag-id='{q_id}']")
            if loc.count() == 0:
                continue

            try:
                if tag in ("textarea", "input"):
                    _human_type(loc.first, str(ans), min_delay_ms=10, max_delay_ms=25)
                    logger.info("Filled custom field [%s]: %s", item['label'][:30], str(ans)[:50])
                elif tag == "select":
                    sel = loc.first
                    options = sel.locator("option").all()
                    matched = False
                    ans_lower = str(ans).lower().strip()
                    for idx, opt in enumerate(options):
                        opt_text = opt.inner_text().strip().lower()
                        if opt_text == ans_lower or ans_lower in opt_text:
                            sel.select_option(index=idx)
                            matched = True
                            break
                    if not matched and len(options) > 1:
                        sel.select_option(index=1)
            except Exception as fill_err:
                logger.debug("Failed to fill custom field %s: %s", q_id, fill_err)

    except Exception as e:
        logger.warning("Error during custom question answering: %s", e)


def _verify_submission(page, ats_name: str = "", scope=None) -> tuple[bool, str]:
    """
    Verify whether the ATS form was genuinely submitted successfully.
    Checks:
    1. Post-submit security challenge / captcha
    2. Absence of blocking form error messages
    3. Navigation to confirmation URL
    4. Confirmation text presence (on page or embedded iframe)
    5. Checks if submit button is still visible and enabled
    """
    try:
        confirmation_url_keywords = [
            "confirmation", "thanks", "thank-you", "thank_you", "submitted", "success", "application_submitted"
        ]
        success_phrases = [
            "thank you for applying",
            "thank you for submitting",
            "application submitted",
            "application has been submitted",
            "application has been received",
            "your application has been received",
            "we've received your application",
            "received your application",
            "thank you for your interest",
            "your application was submitted",
            "application complete",
            "submission received",
            "application received",
        ]
        submit_btn_selectors = [
            "button[type='submit']",
            "button#submit_app",
            "button#btn-submit",
            "button.ashby-application-form-submit-button",
            "button[data-qa='btn-submit']",
            "button:has-text('Submit Application')",
            "button:has-text('SUBMIT APPLICATION')",
            "button:has-text('Submit')",
        ]

        scopes_to_check = [page]
        if scope is not None and scope is not page:
            scopes_to_check.insert(0, scope)

        error_locators = [
            ".error", ".form-error", ".field-error", ".alert-error",
            "[aria-invalid='true']", ".has-error", "div.error-message",
            "p.error-message", "span.error", "div.grecaptcha-error"
        ]

        # Poll for up to 8 seconds for the SPA/form submission network round-trip
        for _ in range(8):
            page.wait_for_timeout(1000)

            # 1. Check for blocking validation errors first
            for target in scopes_to_check:
                try:
                    for sel in error_locators:
                        errors = target.locator(sel)
                        if errors.count() > 0:
                            for idx in range(min(errors.count(), 3)):
                                err_elem = errors.nth(idx)
                                if err_elem.is_visible():
                                    err_text = err_elem.inner_text().strip()
                                    if err_text:
                                        return False, f"Validation error: {err_text[:120]}"
                except Exception:
                    continue

            # 2. Check current URL for confirmation indicators
            curr_url = page.url.lower()
            if any(kw in curr_url for kw in confirmation_url_keywords):
                return True, "Confirmation URL reached"

            # 3. Check page/iframe content for confirmation text
            for target in scopes_to_check:
                try:
                    body = target.locator("body")
                    if body.count() > 0:
                        page_text = body.inner_text().lower()
                        if any(phrase in page_text for phrase in success_phrases):
                            return True, "Success text found on page"
                except Exception:
                    continue

            # 4. Check platform-specific success containers
            if ats_name == "ashby":
                for sel in [".ashby-application-form-success", "div[class*='Success' i]", "svg[class*='success' i]", "div:has-text('Application Submitted')"]:
                    if page.locator(sel).count() > 0:
                        return True, "Ashby confirmation container found"
            elif ats_name == "lever":
                if page.locator(".application-confirmation, div.thanks, div:has-text('Application submitted')").count() > 0:
                    return True, "Lever confirmation container reached"

            # 5. Check for genuine active security challenge if no success confirmation yet
            if _detect_captcha(page):
                return False, f"requires_captcha: Security challenge appeared after submit on {ats_name}"

            # 5. Check if submit button has disappeared
            btn_visible = False
            for target in scopes_to_check:
                for sel in submit_btn_selectors:
                    try:
                        b = target.locator(sel)
                        if b.count() > 0 and b.first.is_visible():
                            btn_visible = True
                            break
                    except Exception:
                        continue
                if btn_visible:
                    break

            if not btn_visible:
                # Submit button vanished and no validation errors; wait 1 more second and re-check text
                page.wait_for_timeout(1000)
                for target in scopes_to_check:
                    try:
                        body = target.locator("body")
                        if body.count() > 0:
                            page_text = body.inner_text().lower()
                            if any(phrase in page_text for phrase in success_phrases):
                                return True, "Success text found on page"
                    except Exception:
                        continue
                if ats_name == "ashby":
                    for sel in [".ashby-application-form-success", "div[class*='Success' i]", "svg[class*='success' i]", "div:has-text('Application Submitted')"]:
                        if page.locator(sel).count() > 0:
                            return True, "Ashby confirmation container found"
                elif ats_name == "lever":
                    if page.locator(".application-confirmation, div.thanks, div:has-text('Application submitted')").count() > 0:
                        return True, "Lever confirmation container reached"

        # If submit button is still visible after polling, submission did not advance
        for target in scopes_to_check:
            for sel in submit_btn_selectors:
                try:
                    b = target.locator(sel)
                    if b.count() > 0 and b.first.is_visible():
                        return False, "Submit button still visible after click with no confirmation (form did not advance)"
                except Exception:
                    continue

        return False, "Submission unverified: no confirmation URL or success text detected"
    except Exception as e:
        return False, f"Verification error: {e}"


# ── ATS flows (Playwright) ────────────────────────────────────────────────────

def _apply_greenhouse(page, jd: dict, pdf_path: str, dry_run: bool, cover_letter: str = "", zero_llm_mode: bool = False) -> tuple[bool, str]:
    """Fill and optionally submit a Greenhouse application form."""
    logger.info("Starting Greenhouse flow...")

    _dismiss_cookie_banner(page)

    if _detect_captcha(page):
        logger.warning("Active captcha or challenge detected on Greenhouse form for %s", jd.get("company"))
        return False, "requires_captcha: Cloudflare or reCAPTCHA detected"

    closed, close_reason = _is_job_closed(page)
    if closed:
        logger.warning("Greenhouse job closed/unavailable for %s: %s", jd.get("company"), close_reason)
        return False, f"job_closed: {close_reason}"

    current_url = page.url
    is_modern_board = ("job-boards.greenhouse.io" in current_url) or ("job-boards.eu.greenhouse.io" in current_url)
    form_scope = page

    if is_modern_board:
        logger.info("Detected modern Greenhouse board (%s)", current_url[:60])
        page.wait_for_selector(
            "input[autocomplete='given-name'], input#first_name, "
            "input[name='first_name'], input[autocomplete='name'], input#name, input[type='text']",
            timeout=SELECTOR_TIMEOUT_MS,
        )
    else:
        page.wait_for_selector(
            "form#application-form, form#application_form, form#application, "
            "form[action*='applications'], div#application, iframe#grnhse_iframe, a[href*='/apply' i]",
            timeout=SELECTOR_TIMEOUT_MS,
        )
        top_form = page.locator(
            "form#application-form, form#application_form, form#application, "
            "form[action*='applications'], input#first_name, input[name='first_name']"
        )
        iframe_loc = page.locator("iframe#grnhse_iframe")
        if top_form.count() == 0 and iframe_loc.count() == 0:
            apply_cta = page.locator("a[href*='/apply' i], a[href*='greenhouse.io/embed' i], a:has-text('Apply Now'), a:has-text('Apply for this')")
            if apply_cta.count() > 0:
                href = apply_cta.first.get_attribute("href")
                if href:
                    page.goto(urljoin(current_url, href), wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)
                else:
                    apply_cta.first.click()
                    page.wait_for_load_state("domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)
                top_form = page.locator(
                    "form#application-form, form#application_form, form#application, "
                    "form[action*='applications'], input#first_name, input[name='first_name']"
                )
                iframe_loc = page.locator("iframe#grnhse_iframe")
        if top_form.count() == 0 and iframe_loc.count() > 0:
            iframe_el = iframe_loc.first.element_handle(timeout=SELECTOR_TIMEOUT_MS)
            frame = iframe_el.content_frame() if iframe_el else None
            if frame is not None:
                logger.info("Detected embedded Greenhouse iframe#grnhse_iframe — switching form scope")
                form_scope = frame
                form_scope.wait_for_selector(
                    "form#application-form, form#application_form, form#application, input#first_name, input[name='first_name'], input#name",
                    timeout=SELECTOR_TIMEOUT_MS,
                )
                if _detect_captcha(form_scope):
                    return False, "requires_captcha: Cloudflare or reCAPTCHA detected inside iframe"
                closed_iframe, close_reason_iframe = _is_job_closed(form_scope)
                if closed_iframe:
                    return False, f"job_closed: {close_reason_iframe}"

    # Handle split first/last name or single full name
    first_name = form_scope.locator(
        "input[autocomplete='given-name'], input#first_name, input[name='first_name']"
    )
    if first_name.count() > 0:
        _human_type(first_name.first, USER_PROFILE["first_name"])
        last_name = form_scope.locator(
            "input[autocomplete='family-name'], input#last_name, input[name='last_name']"
        )
        if last_name.count() > 0:
            _human_type(last_name.first, USER_PROFILE["last_name"])
    else:
        full_name = form_scope.locator("input[autocomplete='name'], input#name, input[name='name']")
        if full_name.count() > 0:
            _human_type(full_name.first, USER_PROFILE["full_name"])

    email = form_scope.locator(
        "input[autocomplete='email'], input#email, input[name='email']"
    )
    if email.count() > 0:
        _human_type(email.first, USER_PROFILE["email"])

    phone = form_scope.locator(
        "input[autocomplete='tel'], input#phone, input[name='phone']"
    )
    if phone.count() > 0:
        _human_type(phone.first, USER_PROFILE["phone_international"])

    resume_input = form_scope.locator(
        "input[type='file'][data-source='resume'], "
        "input[type='file'][name='resume'], "
        "input[type='file']"
    )
    if resume_input.count() > 0:
        resume_input.first.set_input_files(pdf_path)

    _fill_social_fields(form_scope, "linkedin", USER_PROFILE["linkedin"])
    _fill_social_fields(form_scope, "github", USER_PROFILE["github"])
    _fill_social_fields(form_scope, "portfolio", USER_PROFILE["portfolio"])
    _fill_social_fields(form_scope, "website", USER_PROFILE["portfolio"])
    _fill_common_custom_fields(form_scope)

    has_foreign_wall, wall_reason = _detect_foreign_work_auth_knockout(form_scope)
    if has_foreign_wall:
        logger.info("Skipping Greenhouse form due to geo work-auth wall: %s", wall_reason)
        return False, f"skipped_geo_knockout: {wall_reason}"

    if cover_letter:
        cl_input = form_scope.locator("textarea[name*='cover_letter' i], textarea#cover_letter_text, textarea[name*='comments' i]")
        if cl_input.count() > 0:
            _human_type(cl_input.first, cover_letter, min_delay_ms=5, max_delay_ms=15)

    _answer_custom_questions(form_scope, jd, zero_llm_mode=zero_llm_mode)

    if _detect_captcha(page) or (form_scope is not page and _detect_captcha(form_scope)):
        logger.warning("Active captcha detected on Greenhouse form for %s", jd.get("company"))
        return False, "requires_captcha: Cloudflare or reCAPTCHA detected"

    if not dry_run:
        logger.info("Submitting Greenhouse application...")
        submit_btn = None
        for s in form_scope.locator("button#submit_app, button[type='submit'], button:has-text('Submit Application')").all():
            if s.is_visible():
                submit_btn = s
                break
        if submit_btn:
            submit_btn.scroll_into_view_if_needed()
            submit_btn.click(timeout=10_000)
            try:
                page.wait_for_load_state("networkidle", timeout=15_000)
            except Exception:
                pass
            return _verify_submission(page, "greenhouse", scope=form_scope)
        else:
            return False, "Submit button not found on Greenhouse form"
    else:
        logger.info("--dry-run: Verified Greenhouse form fields.")
        return True, "Dry run: Greenhouse form fields verified"


def _apply_lever(page, jd: dict, pdf_path: str, dry_run: bool, cover_letter: str = "", zero_llm_mode: bool = False) -> tuple[bool, str]:
    """Fill and optionally submit a Lever application form."""
    logger.info("Starting Lever flow...")

    _dismiss_cookie_banner(page)

    if _detect_captcha(page):
        logger.warning("Active captcha or challenge detected on Lever form for %s", jd.get("company"))
        return False, "requires_captcha: Cloudflare or reCAPTCHA detected"

    closed, close_reason = _is_job_closed(page)
    if closed:
        logger.warning("Lever job closed/unavailable for %s: %s", jd.get("company"), close_reason)
        return False, f"job_closed: {close_reason}"

    current_url = page.url
    if "apply.lever.co" not in current_url and not current_url.rstrip("/").endswith("/apply"):
        apply_btn = page.locator("a.template-btn-submit, a:has-text('Apply for this job'), a:has-text('Apply')")
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
        _human_type(name_input.first, USER_PROFILE["full_name"])

    email_input = page.locator("input[name='email'], input#email, input[autocomplete='email']")
    if email_input.count() > 0:
        _human_type(email_input.first, USER_PROFILE["email"])

    phone_input = page.locator("input[name='phone'], input#phone, input[autocomplete='tel']")
    if phone_input.count() > 0:
        _human_type(phone_input.first, USER_PROFILE["phone"])

    resume_input = page.locator(
        "input[type='file'][name='resume'], input[type='file']"
    )
    if resume_input.count() > 0:
        resume_input.first.set_input_files(pdf_path)

    _fill_social_fields(page, "LinkedIn", USER_PROFILE["linkedin"])
    _fill_social_fields(page, "GitHub", USER_PROFILE["github"])
    _fill_social_fields(page, "Portfolio", USER_PROFILE["portfolio"])
    _fill_common_custom_fields(page)

    has_foreign_wall, wall_reason = _detect_foreign_work_auth_knockout(page)
    if has_foreign_wall:
        logger.info("Skipping Lever form due to geo work-auth wall: %s", wall_reason)
        return False, f"skipped_geo_knockout: {wall_reason}"

    if cover_letter:
        cl_input = page.locator("textarea[name*='comments' i], textarea[name*='cover' i]")
        if cl_input.count() > 0:
            _human_type(cl_input.first, cover_letter, min_delay_ms=5, max_delay_ms=15)

    _answer_custom_questions(page, jd, zero_llm_mode=zero_llm_mode)

    if _detect_captcha(page):
        logger.warning("Active captcha detected on Lever form for %s", jd.get("company"))
        return False, "requires_captcha: Cloudflare or reCAPTCHA detected"

    if not dry_run:
        logger.info("Submitting Lever application...")
        submit_btn = None
        for s in page.locator(
            "button#btn-submit, button[data-qa='btn-submit'], button[type='submit'], button:has-text('SUBMIT APPLICATION')"
        ).all():
            if s.is_visible():
                submit_btn = s
                break
        if submit_btn:
            submit_btn.scroll_into_view_if_needed()
            try:
                submit_btn.click(timeout=10_000)
            except Exception as click_err:
                if _detect_captcha(page):
                    return False, "requires_captcha: Lever hCaptcha challenge intercepted submit"
                return False, f"Lever submit click failed: {click_err}"
            try:
                page.wait_for_load_state("networkidle", timeout=10_000)
            except Exception:
                pass
            if _detect_captcha(page):
                return False, "requires_captcha: Lever hCaptcha challenge appeared after submit"
            return _verify_submission(page, "lever")
        else:
            return False, "Visible submit button not found on Lever form"
    else:
        logger.info("--dry-run: Verified Lever form fields.")
        return True, "Dry run: Lever form fields verified"


def _wait_ashby_hydration(page, timeout_ms: int = 8_000) -> None:
    """Wait for Ashby SPA React root to hydrate (inputs present OR closed/error message rendered, past 'Loading…')."""
    try:
        if hasattr(page, "wait_for_function"):
            page.wait_for_function(
                "() => {"
                "  const txt = (document.body && document.body.innerText) || '';"
                "  if (txt.includes('Page not found') || txt.includes('not be found') || txt.includes('no longer')) return true;"
                "  if (document.querySelector('input, textarea, select, #application-form')) return true;"
                "  return !txt.trim().startsWith('Loading') && txt.length > 80;"
                "}",
                timeout=timeout_ms,
            )
    except Exception:
        pass


def _apply_ashby(page, jd: dict, pdf_path: str, dry_run: bool, cover_letter: str = "", zero_llm_mode: bool = False) -> tuple[bool, str]:
    """Fill and optionally submit an Ashby application form."""
    logger.info("Starting Ashby flow...")

    _dismiss_cookie_banner(page)
    _wait_ashby_hydration(page)

    if _detect_captcha(page):
        logger.warning("Active captcha or challenge detected on Ashby form for %s", jd.get("company"))
        return False, "requires_captcha: Cloudflare or reCAPTCHA detected"

    closed, close_reason = _is_job_closed(page)
    if closed:
        logger.warning("Ashby job closed/unavailable for %s: %s", jd.get("company"), close_reason)
        return False, f"job_closed: {close_reason}"

    current_url = page.url

    # If URL doesn't already point directly to /application, navigate directly or click the Application tab/button
    clean_url = current_url.split("?")[0].rstrip("/")
    if not clean_url.endswith("/application"):
        app_url = clean_url + "/application"
        logger.info("Directly navigating to Ashby application endpoint: %s", app_url)
        try:
            page.goto(app_url, wait_until="domcontentloaded", timeout=15_000)
            _wait_ashby_hydration(page)
        except Exception:
            pass

    if not page.url.rstrip("/").endswith("/application"):
        apply_btn = page.locator(
            "button:has-text('Application'), a:has-text('Application'), "
            "[role='tab']:has-text('Application'), "
            "button:has-text('Apply for this job'), a:has-text('Apply for this job'), "
            "button:has-text('Apply for this Job'), a:has-text('Apply for this Job'), "
            "button:has-text('Apply')"
        )
        if apply_btn.count() > 0 and apply_btn.first.is_visible():
            apply_btn.first.click()
            page.wait_for_timeout(1500)
            _wait_ashby_hydration(page)

    # Re-check closed status after navigation
    closed, close_reason = _is_job_closed(page)
    if closed:
        logger.warning("Ashby job closed/unavailable after navigation for %s: %s", jd.get("company"), close_reason)
        return False, f"job_closed: {close_reason}"

    # Ashby uses _systemfield_name or standard text input for the candidate name
    page.wait_for_selector(
        "input[name='_systemfield_name'], input[name='name'], input[autocomplete='name'], input[autocomplete='given-name'], div#application-form",
        timeout=SELECTOR_TIMEOUT_MS,
    )

    name_input = page.locator(
        "input[name='_systemfield_name'], input[name='name'], input[autocomplete='name']"
    )
    if name_input.count() > 0:
        _human_type(name_input.first, USER_PROFILE["full_name"])
    else:
        first_name = page.locator("input[autocomplete='given-name']")
        if first_name.count() > 0:
            _human_type(first_name.first, USER_PROFILE["first_name"])
            last_name = page.locator("input[autocomplete='family-name']")
            if last_name.count() > 0:
                _human_type(last_name.first, USER_PROFILE["last_name"])

    email_input = page.locator(
        "input[name='_systemfield_email'], input[name='email'], input[type='email'], input[autocomplete='email']"
    )
    if email_input.count() > 0:
        _human_type(email_input.first, USER_PROFILE["email"])

    # Ashby often assigns a dynamic UUID to phone inputs — match type='tel' as well
    phone_input = page.locator(
        "input[name='_systemfield_phone'], input[name='phone'], input[type='tel'], input[autocomplete='tel']"
    )
    if phone_input.count() > 0:
        _human_type(phone_input.first, USER_PROFILE["phone"])

    # Target real resume uploader (#_systemfield_resume) BEFORE Ashby's 'Autofill with Resume' file input
    resume_input = page.locator(
        "input[type='file']#_systemfield_resume, "
        "input[type='file'][id*='resume' i], "
        "input[type='file'][name*='resume' i], "
        "input[type='file'][data-source='resume'], "
        "input[type='file']"
    )
    if resume_input.count() > 0:
        resume_input.first.set_input_files(pdf_path)

    _fill_social_fields(page, "linkedin", USER_PROFILE["linkedin"])
    _fill_social_fields(page, "github", USER_PROFILE["github"])
    _fill_social_fields(page, "portfolio", USER_PROFILE["portfolio"])
    _fill_social_fields(page, "website", USER_PROFILE["portfolio"])
    _fill_common_custom_fields(page)

    has_foreign_wall, wall_reason = _detect_foreign_work_auth_knockout(page)
    if has_foreign_wall:
        logger.info("Skipping Ashby form due to geo work-auth wall: %s", wall_reason)
        return False, f"skipped_geo_knockout: {wall_reason}"

    if cover_letter:
        cl_input = page.locator("textarea[name*='coverLetter' i], textarea[name*='comments' i], textarea[name*='message' i]")
        if cl_input.count() > 0:
            _human_type(cl_input.first, cover_letter, min_delay_ms=5, max_delay_ms=15)

    _answer_custom_questions(page, jd, zero_llm_mode=zero_llm_mode)
    _fill_button_toggles(page)
    _dismiss_cookie_banner(page)

    if _detect_captcha(page):
        logger.warning("Active captcha detected on Ashby form for %s", jd.get("company"))
        return False, "requires_captcha: Cloudflare or reCAPTCHA detected"

    if not dry_run:
        logger.info("Submitting Ashby application...")
        submit_btn = None
        for s in page.locator(
            "button.ashby-application-form-submit-button, button[type='submit'], button:has-text('Submit Application'), button:has-text('Submit')"
        ).all():
            if s.is_visible():
                submit_btn = s
                break

        if not submit_btn:
            return False, "Submit button not found on Ashby form"

        submit_btn.scroll_into_view_if_needed()

        # Wait briefly for React validation state to sync and enable submit button
        for _ in range(6):
            if not submit_btn.is_disabled():
                break
            page.wait_for_timeout(500)

        if submit_btn.is_disabled():
            empty_req_count = page.evaluate(
                "() => document.querySelectorAll(\"[aria-invalid='true']\").length + "
                "Array.from(document.querySelectorAll('input[required], textarea[required], select[required]')).filter(el => !el.value).length"
            )
            err_msg = f"Submit button disabled due to unfilled required fields ({empty_req_count} detected)"
            logger.warning(err_msg)
            return False, err_msg

        submit_btn.click(timeout=10_000)
        try:
            page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            pass
        return _verify_submission(page, "ashby")
    else:
        logger.info("--dry-run: Verified Ashby form fields.")
        return True, "Dry run: Ashby form fields verified"


_AGGREGATOR_ATS = {"remotive", "remoteok", "arbeitnow", "jobicy", "himalayas"}


def _resolve_aggregator_target(page, apply_btn, aggregator_name: str):
    """Click an aggregator CTA button and follow popup or explicit href redirect fallback."""
    href = ""
    try:
        href = apply_btn.first.get_attribute("href") or ""
    except Exception:
        href = ""

    try:
        with page.expect_popup(timeout=8000) as popup_info:
            apply_btn.first.click()
        target_page = popup_info.value
        should_close = True
    except PlaywrightTimeoutError:
        target_page = page
        should_close = False

    target_page.wait_for_load_state("domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)

    # If still stuck on the aggregator page despite click (common on RemoteOK/Jobicy wrappers), follow href
    try:
        cur_ats = detect_ats(target_page.url)
    except UnsupportedATSError:
        cur_ats = ""

    if cur_ats == aggregator_name and href and not href.startswith("#") and not href.lower().startswith("javascript:"):
        next_url = urljoin(page.url, href)
        if next_url != target_page.url:
            target_page.goto(next_url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)

    return target_page, should_close


def _apply_remotive(page, jd: dict, pdf_path: str, dry_run: bool, cover_letter: str = "", zero_llm_mode: bool = False) -> tuple[bool, str]:
    """Handles Remotive job apply redirect or embedded form."""
    logger.info("Starting Remotive flow...")

    apply_btn = page.locator(
        "a.apply-button, a[data-ga-label='apply'], a:has-text('Apply for this job')"
    )
    if apply_btn.count() == 0:
        logger.warning("Remotive: no apply button found — skipping")
        return False, "No apply button found on Remotive page"

    target_page, should_close = _resolve_aggregator_target(page, apply_btn, "remotive")

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
    if not flow_fn or ats_name in _AGGREGATOR_ATS:
        if should_close:
            target_page.close()
        return False, f"No nested flow for ATS '{ats_name}'"

    try:
        result = flow_fn(target_page, jd, pdf_path, dry_run, cover_letter, zero_llm_mode=zero_llm_mode)
        if isinstance(result, tuple):
            return result
        return (result, "") if result else (False, f"{ats_name} flow returned False")
    finally:
        if should_close:
            target_page.close()


def _apply_remoteok(page, jd: dict, pdf_path: str, dry_run: bool, cover_letter: str = "", zero_llm_mode: bool = False) -> tuple[bool, str]:
    """Handles RemoteOK job apply redirect."""
    logger.info("Starting RemoteOK flow...")

    apply_btn = page.locator(
        "a.button-apply, a:has-text('Apply Now'), a:has-text('Apply')"
    )
    if apply_btn.count() == 0:
        logger.warning("RemoteOK: no apply button found — skipping")
        return False, "No apply button found on RemoteOK page"

    target_page, should_close = _resolve_aggregator_target(page, apply_btn, "remoteok")

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
    if not flow_fn or ats_name in _AGGREGATOR_ATS:
        if should_close:
            target_page.close()
        return False, f"No nested flow for ATS '{ats_name}'"

    try:
        result = flow_fn(target_page, jd, pdf_path, dry_run, cover_letter, zero_llm_mode=zero_llm_mode)
        if isinstance(result, tuple):
            return result
        return (result, "") if result else (False, f"{ats_name} flow returned False")
    finally:
        if should_close:
            target_page.close()


def _apply_arbeitnow(page, jd: dict, pdf_path: str, dry_run: bool, cover_letter: str = "", zero_llm_mode: bool = False) -> tuple[bool, str]:
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
    if not flow_fn or ats_name in _AGGREGATOR_ATS:
        return False, f"No nested flow for ATS '{ats_name}'"

    result = flow_fn(page, jd, pdf_path, dry_run, cover_letter, zero_llm_mode=zero_llm_mode)
    if isinstance(result, tuple):
        return result
    return (result, "") if result else (False, f"{ats_name} flow returned False")


def _apply_jobicy(page, jd: dict, pdf_path: str, dry_run: bool, cover_letter: str = "", zero_llm_mode: bool = False) -> tuple[bool, str]:
    """Handles Jobicy job apply redirect to ATS."""
    logger.info("Starting Jobicy flow...")
    apply_btn = page.locator(
        "button.jv-apply-primary, a.jv-button:has-text('Apply'), a:has-text('Apply for this job'), a:has-text('Apply now')"
    )
    if apply_btn.count() == 0:
        logger.warning("Jobicy: no apply button found — skipping")
        return False, "No apply button found on Jobicy page"

    target_page, should_close = _resolve_aggregator_target(page, apply_btn, "jobicy")

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
    if not flow_fn or ats_name in _AGGREGATOR_ATS:
        if should_close:
            target_page.close()
        return False, f"No nested flow for ATS '{ats_name}'"

    try:
        result = flow_fn(target_page, jd, pdf_path, dry_run, cover_letter, zero_llm_mode=zero_llm_mode)
        if isinstance(result, tuple):
            return result
        return (result, "") if result else (False, f"{ats_name} flow returned False")
    finally:
        if should_close:
            target_page.close()


def _apply_himalayas(page, jd: dict, pdf_path: str, dry_run: bool, cover_letter: str = "", zero_llm_mode: bool = False) -> tuple[bool, str]:
    """Handles Himalayas job apply redirect or embedded application."""
    logger.info("Starting Himalayas flow...")
    if _detect_captcha(page):
        return False, "requires_captcha: Security challenge detected on Himalayas page"

    apply_btn = page.locator(
        "a[href*='apply'], a:has-text('Apply for this job'), a:has-text('Apply on company website'), button:has-text('Apply')"
    )
    if apply_btn.count() == 0:
        logger.warning("Himalayas: no apply button found on page %s", page.url)
        return False, "No apply button found on Himalayas page"

    target_page, should_close = _resolve_aggregator_target(page, apply_btn, "himalayas")

    try:
        ats_name = detect_ats(target_page.url)
    except UnsupportedATSError:
        msg = f"Himalayas redirect did not land on a supported ATS: {target_page.url}"
        logger.warning(msg)
        if should_close:
            target_page.close()
        return False, msg

    logger.info("Himalayas redirected to ATS: %s", ats_name)
    flow_fn = _ATS_FLOW_MAP.get(ats_name)
    if not flow_fn or ats_name in _AGGREGATOR_ATS:
        if should_close:
            target_page.close()
        return False, f"No nested flow for ATS '{ats_name}'"

    try:
        result = flow_fn(target_page, jd, pdf_path, dry_run, cover_letter, zero_llm_mode=zero_llm_mode)
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
    "himalayas":  _apply_himalayas,
}


# ── Main entry ────────────────────────────────────────────────────────────────

def apply(
    jd: dict,
    pdf_path: str,
    dry_run: bool = False,
    cover_letter: str = "",
    use_persistent_profile: bool = False,
    zero_llm_mode: bool = False,
) -> tuple[bool, str]:
    """
    Detect ATS and run the appropriate Playwright form-fill flow.

    Args:
        jd:                     JD dict (from fetch_jds.py)
        pdf_path:               Absolute path to the tailored resume PDF
        dry_run:                If True, fill fields but do not click submit
        cover_letter:           Generated cover letter text to paste into textarea
        use_persistent_profile: If True, uses a persistent Chrome user data directory
                                to reuse saved sessions/cookies and minimize captcha challenges.
        zero_llm_mode:          If True, skip Groq custom-question autofill calls.

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
            user_agent = (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            )
            persistent_env = os.environ.get("PLAYWRIGHT_PERSISTENT_PROFILE", "").lower() in ("true", "1", "yes")
            browser = None

            if use_persistent_profile or persistent_env:
                profile_dir = Path(__file__).resolve().parent.parent / "data" / "browser_profile"
                profile_dir.mkdir(parents=True, exist_ok=True)
                logger.info("Launching Playwright with persistent profile at %s", profile_dir)
                context = p.chromium.launch_persistent_context(
                    user_data_dir=str(profile_dir),
                    headless=True,
                    user_agent=user_agent,
                )
                page = context.pages[0] if context.pages else context.new_page()
            else:
                browser = p.chromium.launch(headless=True)
                context = browser.new_context(user_agent=user_agent)
                page = context.new_page()

            try:
                Stealth().apply_stealth_sync(page)

                logger.info("Navigating to %s", apply_url)
                page.goto(apply_url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)

                # Early security challenge check (Cloudflare Turnstile, Just a moment, bot challenge)
                if _detect_captcha(page):
                    logger.warning("Active security challenge or captcha detected immediately on %s", page.url)
                    return False, "requires_captcha: Cloudflare or reCAPTCHA detected"

                # Early closed job check (404, Page not found, Position closed)
                closed, close_reason = _is_job_closed(page)
                if closed:
                    logger.warning("Job is closed or unavailable immediately on %s: %s", page.url, close_reason)
                    return False, f"job_closed: {close_reason}"

                try:
                    ats_name = detect_ats(page.url)
                except UnsupportedATSError as e:
                    logger.warning(str(e))
                    return False, str(e)

                logger.info("Detected ATS: %s", ats_name)
                flow_fn = _ATS_FLOW_MAP[ats_name]

                result = flow_fn(page, jd, pdf_path, dry_run, cover_letter, zero_llm_mode=zero_llm_mode)
                if isinstance(result, tuple):
                    return result
                return (result, "") if result else (False, f"{ats_name} flow returned False")
            except PlaywrightTimeoutError as e:
                try:
                    if _detect_captcha(page):
                        return False, "requires_captcha: Security challenge detected during form fill"
                    closed, close_reason = _is_job_closed(page)
                    if closed:
                        return False, f"job_closed: {close_reason}"
                except Exception:
                    pass
                msg = f"Timeout filling {ats_name if 'ats_name' in locals() else 'ATS'} form: {e}"
                logger.warning(msg)
                return False, msg
            except Exception as e:
                msg = f"Error in {ats_name if 'ats_name' in locals() else 'ATS'} flow: {e}"
                logger.error(msg)
                return False, msg
            finally:
                if browser:
                    browser.close()
                else:
                    context.close()
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
    parser.add_argument("--persistent-profile", action="store_true",
                        help="Use persistent browser user-data directory for cookies/sessions")
    args = parser.parse_args()

    jd = json.loads(Path(args.jd_json).read_text(encoding="utf-8"))
    success, notes = apply(
        jd,
        args.pdf_path,
        dry_run=args.dry_run,
        use_persistent_profile=args.persistent_profile,
    )
    print("Result:", "success" if success else f"failed — {notes}")

