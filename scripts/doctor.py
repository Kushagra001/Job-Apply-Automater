"""
doctor.py — Pre-Flight Health & Connectivity Diagnostics
=========================================================
Audits system readiness before starting the automated job application pipeline.
Inspired by Agent-Reach's self-diagnostic tools: checks environment variables,
Groq AI inference, Google Sheets authentication, Telegram bot webhooks,
Playwright Chromium runtime, sourcing APIs, and ATS job board endpoints.

Usage:
  python scripts/doctor.py
  python scripts/run_pipeline.py --doctor
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import NamedTuple
import urllib.request
import requests

from dotenv import load_dotenv

# Ensure project root in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

load_dotenv()
logger = logging.getLogger("doctor")


class CheckResult(NamedTuple):
    name: str
    status: str  # "OK", "WARN", "FAIL"
    message: str
    latency_ms: float = 0.0


def _check_env_vars() -> list[CheckResult]:
    results = []

    # 1. Groq API Key
    groq_key = os.environ.get("GROQ_API_KEY", "").strip()
    if groq_key:
        masked = groq_key[:6] + "..." + groq_key[-4:] if len(groq_key) > 10 else "***"
        results.append(CheckResult("Env: GROQ_API_KEY", "OK", f"Configured ({masked})"))
    else:
        results.append(CheckResult("Env: GROQ_API_KEY", "FAIL", "Missing GROQ_API_KEY in environment"))

    # 2. Google Sheets Credentials & ID
    sheet_id = os.environ.get("GOOGLE_SHEET_ID", "").strip()
    sa_json = os.environ.get("GOOGLE_SA_JSON", "").strip()

    if sheet_id and sa_json:
        try:
            parsed = json.loads(sa_json)
            client_email = parsed.get("client_email", "unknown")
            results.append(CheckResult("Env: Google Sheets", "OK", f"Configured (SA: {client_email})"))
        except Exception:
            results.append(CheckResult("Env: Google Sheets", "FAIL", "GOOGLE_SA_JSON is not valid JSON"))
    elif sheet_id and not sa_json:
        results.append(CheckResult("Env: Google Sheets", "FAIL", "GOOGLE_SHEET_ID set but GOOGLE_SA_JSON missing"))
    else:
        results.append(CheckResult("Env: Google Sheets", "FAIL", "GOOGLE_SHEET_ID or GOOGLE_SA_JSON missing"))

    # 3. Telegram Bot Token & Chat ID
    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    tg_chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if tg_token and tg_chat:
        results.append(CheckResult("Env: Telegram Alerts", "OK", f"Configured (Chat ID: {tg_chat})"))
    elif tg_token or tg_chat:
        results.append(CheckResult("Env: Telegram Alerts", "WARN", "Partial config: token or chat ID missing"))
    else:
        results.append(CheckResult("Env: Telegram Alerts", "WARN", "Not configured (alerts will be skipped)"))

    # 4. Gmail IMAP (optional)
    gmail_user = os.environ.get("GMAIL_USER", "").strip()
    gmail_pw = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
    if gmail_user and gmail_pw:
        results.append(CheckResult("Env: Gmail Feedback", "OK", f"Configured ({gmail_user})"))
    else:
        results.append(CheckResult("Env: Gmail Feedback", "WARN", "Not configured (feedback listener disabled)"))

    return results


def _check_groq_api() -> CheckResult:
    groq_key = os.environ.get("GROQ_API_KEY", "").strip()
    if not groq_key:
        return CheckResult("Groq Inference API", "FAIL", "Skipped (GROQ_API_KEY not set)")

    start = time.perf_counter()
    try:
        from groq import Groq
        from scripts.score_and_pick import FALLBACK_MODELS
        client = Groq(api_key=groq_key, timeout=10.0)
        configured_model = os.environ.get("GROQ_MODEL")
        models_to_test = [configured_model] if configured_model else FALLBACK_MODELS

        last_err = None
        for model in models_to_test:
            try:
                resp = client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": "Ping. Respond with 'PONG'."}],
                    max_tokens=5,
                )
                elapsed = (time.perf_counter() - start) * 1000
                reply = resp.choices[0].message.content.strip().encode("ascii", "replace").decode("ascii")
                return CheckResult("Groq Inference API", "OK", f"Model '{model}' replied: '{reply}'", elapsed)
            except Exception as e:
                last_err = e

        elapsed = (time.perf_counter() - start) * 1000
        return CheckResult("Groq Inference API", "FAIL", f"All candidate models failed. Last error: {last_err}", elapsed)
    except Exception as e:
        elapsed = (time.perf_counter() - start) * 1000
        return CheckResult("Groq Inference API", "FAIL", f"Error: {e}", elapsed)


def _check_google_sheets() -> CheckResult:
    sheet_id = os.environ.get("GOOGLE_SHEET_ID", "").strip()
    sa_json = os.environ.get("GOOGLE_SA_JSON", "").strip()
    if not sheet_id or not sa_json:
        return CheckResult("Google Sheets API", "FAIL", "Skipped (credentials missing)")

    start = time.perf_counter()
    try:
        from scripts.log_and_notify import _get_sheet
        sheet = _get_sheet()
        title = sheet.spreadsheet.title
        row_count = sheet.row_count
        elapsed = (time.perf_counter() - start) * 1000
        return CheckResult("Google Sheets API", "OK", f"Connected to '{title}' ({row_count} rows)", elapsed)
    except Exception as e:
        elapsed = (time.perf_counter() - start) * 1000
        return CheckResult("Google Sheets API", "FAIL", f"Connection failed: {e}", elapsed)


def _check_telegram_bot() -> CheckResult:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        return CheckResult("Telegram Bot API", "WARN", "Skipped (TELEGRAM_BOT_TOKEN not set)")

    start = time.perf_counter()
    try:
        resp = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=8)
        elapsed = (time.perf_counter() - start) * 1000
        if resp.status_code == 200:
            data = resp.json().get("result", {})
            username = data.get("username", "unknown")
            return CheckResult("Telegram Bot API", "OK", f"Bot @{username} verified", elapsed)
        else:
            return CheckResult("Telegram Bot API", "FAIL", f"HTTP {resp.status_code}: {resp.text}", elapsed)
    except Exception as e:
        elapsed = (time.perf_counter() - start) * 1000
        return CheckResult("Telegram Bot API", "FAIL", f"Request failed: {e}", elapsed)


def _check_playwright() -> CheckResult:
    start = time.perf_counter()
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            val = page.evaluate("() => 2 + 2")
            browser.close()
            elapsed = (time.perf_counter() - start) * 1000
            if val == 4:
                return CheckResult("Playwright Chromium", "OK", "Headless browser launched & evaluated JS", elapsed)
            return CheckResult("Playwright Chromium", "WARN", f"JS evaluation returned {val}", elapsed)
    except Exception as e:
        elapsed = (time.perf_counter() - start) * 1000
        return CheckResult("Playwright Chromium", "FAIL", f"Launch failed (run `playwright install chromium`): {e}", elapsed)


def _check_resumes() -> CheckResult:
    resumes_dir = PROJECT_ROOT / "resumes"
    if not resumes_dir.exists():
        return CheckResult("Resume Variants", "FAIL", f"Directory {resumes_dir} not found")

    expected = ["frontend", "backend-node", "backend-python", "ai-engineer", "solutions-support", "sdet-qa"]
    found = []
    missing = []
    for exp in expected:
        path = resumes_dir / f"{exp}.json"
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if "experience" in data or "skills" in data or "name" in data:
                    found.append(exp)
                else:
                    missing.append(f"{exp} (invalid schema)")
            except Exception:
                missing.append(f"{exp} (parse error)")
        else:
            missing.append(exp)

    if not missing:
        return CheckResult("Resume Variants", "OK", f"All {len(found)} core variant profiles valid ({', '.join(found)})")
    elif found:
        return CheckResult("Resume Variants", "WARN", f"Found {len(found)}/{len(expected)}. Missing: {', '.join(missing)}")
    else:
        return CheckResult("Resume Variants", "FAIL", f"No valid variants found in {resumes_dir}")


def _check_sourcing_endpoints() -> list[CheckResult]:
    endpoints = [
        ("Greenhouse API", "https://boards-api.greenhouse.io", {"User-Agent": "Mozilla/5.0"}),
        ("Lever API", "https://api.lever.co", {"User-Agent": "Mozilla/5.0"}),
        ("Ashby API", "https://api.ashbyhq.com", {"User-Agent": "Mozilla/5.0"}),
        ("Remotive API", "https://remotive.com/api/remote-jobs?limit=1", {"User-Agent": "Mozilla/5.0"}),
        ("RemoteOK API", "https://remoteok.com/api", {"User-Agent": "Mozilla/5.0"}),
        ("HackerNews Firebase", "https://hacker-news.firebaseio.com/v0/maxitem.json", {"User-Agent": "Mozilla/5.0"}),
        ("SimplifyJobs New Grad", "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/README.md", {"User-Agent": "Mozilla/5.0"}),
        ("Jina Reader Engine", "https://r.jina.ai/https://example.com", {"User-Agent": "Mozilla/5.0", "Accept": "text/plain"}),
    ]

    results = []
    for name, url, headers in endpoints:
        start = time.perf_counter()
        try:
            resp = requests.get(url, headers=headers, timeout=6.0, allow_redirects=True)
            elapsed = (time.perf_counter() - start) * 1000
            # Consider HTTP 200-499 as reachable host (endpoints might require query params or return 401/404 on base path)
            if resp.status_code < 500:
                results.append(CheckResult(f"Endpoint: {name}", "OK", f"HTTP {resp.status_code}", elapsed))
            else:
                results.append(CheckResult(f"Endpoint: {name}", "WARN", f"HTTP {resp.status_code} (Server Error)", elapsed))
        except Exception as e:
            elapsed = (time.perf_counter() - start) * 1000
            results.append(CheckResult(f"Endpoint: {name}", "FAIL", f"Unreachable: {type(e).__name__}", elapsed))

    return results


def run_doctor() -> bool:
    """
    Run all diagnostic probes, display formatted report, and return True if healthy.
    """
    print("=" * 66, flush=True)
    print("        AUTOMATED JOB APPLY — PRE-FLIGHT HEALTH DOCTOR", flush=True)
    print("=" * 66, flush=True)

    all_checks: list[CheckResult] = []

    # 1. Env vars
    all_checks.extend(_check_env_vars())

    # 2. Resumes
    all_checks.append(_check_resumes())

    # 3. Groq API
    all_checks.append(_check_groq_api())

    # 4. Google Sheets
    all_checks.append(_check_google_sheets())

    # 5. Telegram
    all_checks.append(_check_telegram_bot())

    # 6. Playwright
    all_checks.append(_check_playwright())

    # 7. Sourcing Endpoints
    all_checks.extend(_check_sourcing_endpoints())

    # Format output
    status_icons = {
        "OK": "[  OK  ]",
        "WARN": "[ WARN ]",
        "FAIL": "[ FAIL ]",
    }

    ok_count = 0
    warn_count = 0
    fail_count = 0

    for check in all_checks:
        tag = status_icons.get(check.status, "[ ???? ]")
        latency_str = f"({check.latency_ms:.0f}ms)" if check.latency_ms > 0 else ""
        print(f"{tag} {check.name:<25} {check.message:<40} {latency_str}", flush=True)

        if check.status == "OK":
            ok_count += 1
        elif check.status == "WARN":
            warn_count += 1
        elif check.status == "FAIL":
            fail_count += 1

    print("-" * 66, flush=True)
    summary_line = f"Summary: {ok_count} passed, {warn_count} warnings, {fail_count} failed"
    print(summary_line, flush=True)

    is_healthy = (fail_count == 0)
    if is_healthy:
        print("Status : ALL SYSTEMS OPERATIONAL — Pipeline is ready to run.", flush=True)
    else:
        print("Status : CRITICAL ISSUES DETECTED — Please resolve FAIL items above.", flush=True)
    print("=" * 66, flush=True)

    return is_healthy


if __name__ == "__main__":
    healthy = run_doctor()
    sys.exit(0 if healthy else 1)
