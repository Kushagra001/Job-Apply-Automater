"""
fetch_jds.py — Job Discovery
=============================
Fetches job listings from all configured sources, normalizes each listing to the
canonical JD schema defined in Agents.md, and returns a deduplicated, source-balanced list.

Sources
-------
Native ATS APIs:
  - Greenhouse  — boards-api.greenhouse.io/v1/boards/{company}/jobs
  - Ashby       — api.ashbyhq.com/posting-api/job-board/{company}
  - Lever       — api.lever.co/v0/postings/{company}?mode=json

Aggregator APIs:
  - RemoteOK    — https://remoteok.com/api
  - Remotive    — https://remotive.com/api/remote-jobs?category=software-dev
  - HackerNews  — Ask HN: Who is hiring? thread (direct ATS link extraction)

Filters
-------
1. Tech Relevance    : Job title must contain tech keywords.
2. Fresher Priority  : Excludes senior-only roles (Staff, Principal, Director, VP, Head of, EM).
3. Location Target   : Remote-first, or India-based office/hybrid roles.
4. Source Balancing  : Round-robin interleaves sources so no single source (e.g. Greenhouse)
                       monopolizes the pipeline queue.
"""

from __future__ import annotations

import collections
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import json
import logging
import sys
from pathlib import Path

# Add project root to path so scripts imports work when executed directly
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import os
import re
from typing import Any
import requests
from dotenv import load_dotenv
from tenacity import retry, wait_exponential, stop_after_attempt, retry_if_exception_type

load_dotenv()
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

REMOTEOK_URL = "https://remoteok.com/api"
REMOTIVE_URL = "https://remotive.com/api/remote-jobs"
JOBICY_URL = "https://jobicy.com/api/v2/remote-jobs"
ARBEITNOW_URL = "https://www.arbeitnow.com/api/job-board-api"
HIMALAYAS_URL = "https://himalayas.app/jobs/api"
SIMPLIFY_URL = "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/.github/scripts/listings.json"
REMOTEOK_LIMIT = 150
REMOTIVE_LIMIT = 150
JOBICY_LIMIT = 50

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    )
}

# ── ATS Curated Company Lists ─────────────────────────────────────────────────

GREENHOUSE_COMPANIES = [
    # Top tech / high-volume boards
    "vercel", "planetscale", "postman", "airtable", "intercom",
    "descript", "mercury", "lattice", "brex", "typeform",
    "stripe", "square", "robinhood", "coinbase", "plaid",
    "checkr", "gusto", "zendesk", "twilio", "okta",
    "datadog", "elastic", "segment", "mixpanel", "amplitude",
    "figma", "dropbox", "atlassian", "github", "gitlab",
    "hashicorp", "cloudflare", "mongodb", "confluent", "snowflake",
    "databricks", "dbt-labs", "hubspot", "shopify", "canva",
    # Verified India tech companies on Greenhouse
    "groww", "slice",
]

ASHBY_COMPANIES = [
    # Verified active Ashby boards (AI / modern devtools)
    "linear", "ramp", "supabase", "clerk", "notion",
    "openai", "cohere", "sentry", "temporal", "neon",
    "render", "railway", "weaviate", "pinecone", "langchain",
    "llamaindex", "anthropic", "mistral", "together-ai",
    "scale-ai", "huggingface", "grafana-labs", "cockroach-labs",
    "sourcegraph", "1password", "tailscale", "fly", "turso",
]

LEVER_COMPANIES = [
    # Verified active Lever boards (including India giants & global tech)
    "cred", "meesho", "paytm", "binance", "spotify",
]

YC_COMPANIES = [
    # Fast-growing YC AI and devtool startups on Ashby / Greenhouse
    "resend", "cursor", "firecrawl", "browserbase", "greptile",
    "baseten", "decagon", "tavus", "posthog", "dagger",
    "warp", "cartesia", "mendable", "superhuman", "modal-labs",
    "retool", "monzo", "anysphere",
]

# ── Relevance keyword filter ───────────────────────────────────────────────────

TECH_TITLE_KEYWORDS: frozenset[str] = frozenset([
    # Generic engineering
    "software", "engineer", "developer", "dev", "programmer", "coder",
    # Frontend / Web
    "frontend", "front-end", "front end", "react", "next", "vue", "angular",
    "javascript", "typescript", "ui/ux", "ui engineer", "web developer",
    "svelte", "remix", "nuxt", "gatsby",
    # Backend
    "backend", "back-end", "back end", "python", "node", "django", "fastapi",
    "golang", "go ", "rust", "java ", "spring", "rails", "ruby",
    "elixir", "scala", "kotlin", "php", "laravel",
    # Full stack
    "fullstack", "full-stack", "full stack",
    # AI / ML / Data
    "ai ", "ml ", "llm", "machine learning", "deep learning", "data engineer",
    "data scientist", "nlp", "computer vision", "ai engineer", "generative ai",
    "genai", "prompt engineer", "rag", "fine-tuning", "mlops",
    "data analyst", "analytics engineer", "business intelligence", "bi ",
    # Platform / DevOps / Cloud / Security
    "devops", "platform", "infrastructure", "sre", "cloud", "kubernetes",
    "docker", "devsecops", "reliability", "site reliability",
    "security engineer", "appsec", "cloud engineer", "aws", "gcp", "azure",
    # Mobile
    "ios", "android", "mobile", "flutter", "react native", "swift",
    # Blockchain / Web3
    "blockchain", "web3", "smart contract", "solidity", "defi",
    # QA / Testing / SDET
    "qa", "qa engineer", "test engineer", "quality assurance", "automation engineer",
    "sdet", "software tester", "test analyst", "test automation", "qa tester", "tester",
    # Database / Systems
    "database", "dba", "postgres", "mysql", "embedded", "firmware", "systems engineer",
    # Solutions / Support / Integrations / Tech Adjacent
    "solutions engineer", "solution engineer", "developer advocate", "developer support",
    "technical support", "tech support", "technical writer", "integrations", "integration engineer",
    "support engineer", "application support", "client support engineer", "technical operations",
    "product manager", "product designer",
])

# Word-boundary patterns for seniority levels (e.g. SDE 2, SWE II, Level 3 vs SDE 1, SWE I)
SENIOR_LEVEL_PATTERN = re.compile(
    r"\b(?:sde|swe|engineer|developer|qa)\s*[-_ ]*(?:ii|iii|iv|v|2|3|4|5)\b|\b(?:level|ic|l)\s*[-_ ]*[2-6]\b|\b(?:level|ic)\s*[-_ ]*(?:ii|iii|iv|v)\b",
    re.IGNORECASE
)

JUNIOR_LEVEL_PATTERN = re.compile(
    r"\b(?:sde|swe|engineer|developer|qa)\s*[-_ ]*(?:i|1)\b|\b(?:level|ic|l)\s*[-_ ]*1\b|\b(?:level|ic)\s*[-_ ]*i\b",
    re.IGNORECASE
)

# Senior-only keywords: excluded to prioritize freshers (0-2 yrs)
SENIOR_ONLY_TITLE_KEYWORDS: frozenset[str] = frozenset([
    # Executive & leadership
    "staff", "principal", "distinguished", "fellow",
    "vp ", "vice president", "director", "head of", "chief",
    "c-level", "cto", "cso", "ciso", "cio",
    "senior manager", "engineering manager", "em,", " em ", "(em)",
    "lead ", "team lead", "tech lead", "lead engineer", "lead developer",
    "architect",
    # Senior / Mid-Senior explicit keywords
    "senior", "sr.", "sr ", "sr/", "sr-",
    "mid-senior", "mid senior", "mid-level", "mid level",
])

# Explicit entry/fresher signals: always kept even if overlapping with senior substring
JUNIOR_TITLE_KEYWORDS: frozenset[str] = frozenset([
    "junior", "jr.", " jr ", "jr-", "entry", "entry-level", "entry level",
    "associate", "associate engineer", "associate developer", "associate software",
    "intern", "internship", "graduate", "grad", "new grad", "early career",
    "0-2", "0 to 2", "fresher", "fresh graduate", "campus", "trainee",
])

# High-level management / architect titles that should never be overridden by junior tokens
SUPER_SENIOR: tuple[str, ...] = (
    "director", "vp ", "vice president", "head of", "chief", "cto", "cio", "cso",
    "architect", "staff", "principal", "distinguished", "fellow",
    "engineering manager", "senior manager",
)

# India target locations (precise tokens to avoid false matches on 'in' preposition or Indiana)
INDIA_KEYWORDS: frozenset[str] = frozenset([
    "india", "india,", ", in", "(in)", " - in", "/in", "bangalore", "bengaluru", "mumbai",
    "pune", "hyderabad", "chennai", "delhi", "gurugram", "noida",
    "kolkata", "ahmedabad", "jaipur", "rajasthan", "remote india", "india remote",
])

# Explicit foreign geo-fence phrases
EXCLUDED_GEO_KEYWORDS: tuple[str, ...] = (
    "us only", "u.s. only", "usa only", "united states only", "us-only",
    "remote - us", "remote (us)", "remote, us", "remote: us", "us remote",
    "remote - usa", "remote (usa)", "remote, usa", "remote: usa", "usa remote",
    "remote - united states", "remote (united states)", "remote, united states",
    "remote - canada", "remote (canada)", "remote, canada",
    "remote - uk", "remote (uk)", "remote, uk",
    "remote - europe", "remote (europe)", "remote, europe",
    "north america", "amer", "americas", "emea", "latam", "apac only",
    "uk only", "canada only", "europe only", "germany only",
    "must reside in", "must be located in", "us citizenship",
    "authorized to work in the us", "authorized to work in the united states",
)

# Standalone foreign country / region names for geo-fenced remote listings
EXCLUDED_COUNTRY_RESTRICTED: frozenset[str] = frozenset([
    "us", "u.s.", "usa", "u.s.a.", "united states", "united states of america",
    "canada", "uk", "u.k.", "united kingdom", "great britain", "england",
    "germany", "deutschland", "france", "australia", "netherlands",
    "brazil", "poland", "spain", "singapore", "mexico", "czechia",
    "hungary", "ukraine", "malaysia", "argentina", "europe", "emea", "latam",
    "amer", "americas", "north america", "south america", "apac",
])

# Regex patterns detecting explicit requirement for >= 3 years experience in JD text
SENIOR_EXP_PATTERNS = [
    re.compile(r"(?:minimum|at least|requires?|have|with)\s*([3-9]|\d{2})\s*\+?\s*(?:years?|yrs?)\s*(?:of)?\s*(?:experience|exp)", re.IGNORECASE),
    re.compile(r"([3-9]|\d{2})\s*\+?\s*(?:years?|yrs?)\s*(?:of)?\s*(?:experience|exp)\s*(?:required|minimum)", re.IGNORECASE),
    re.compile(r"experience\s*(?:required|level)?\s*:\s*([3-9]|\d{2})\s*\+?\s*(?:years?|yrs?)", re.IGNORECASE),
]


def _is_tech_job(title: str) -> bool:
    """Return True if the job title contains at least one tech keyword."""
    t = title.lower()
    return any(kw in t for kw in TECH_TITLE_KEYWORDS)


def _is_senior_only(title: str) -> bool:
    """Return True if title indicates an explicitly senior/staff/lead role, unless junior signals are present."""
    t = title.lower()
    # Executive and architecture roles are always senior, even if 'associate' appears
    if any(kw in t for kw in SUPER_SENIOR):
        return True
    # Level numerals: SDE 2, SWE II, Level 3, etc. are senior
    if SENIOR_LEVEL_PATTERN.search(t):
        return True
    # Explicit entry/fresher signals protect roles like 'Associate Software Engineer' or 'SDE 1'
    if JUNIOR_LEVEL_PATTERN.search(t) or any(kw in t for kw in JUNIOR_TITLE_KEYWORDS):
        return False
    # Standard senior signals (e.g. 'Senior Engineer')
    if any(kw in t for kw in SENIOR_ONLY_TITLE_KEYWORDS):
        return True
    return False


def _requires_senior_experience(description: str) -> bool:
    """Return True if the description explicitly demands >= 3 years of experience."""
    if not description:
        return False
    for pat in SENIOR_EXP_PATTERNS:
        m = pat.search(description)
        if m:
            try:
                yrs = int(m.group(1))
                if yrs >= 3:
                    return True
            except (ValueError, IndexError):
                continue
    return False


def is_location_ok(location: str, remote: bool) -> bool:
    """
    Return True if the job is accessible to an India-based applicant:
    - Located in India (office, hybrid, or remote), OR
    - Remote WITHOUT restrictive foreign geo-fencing (e.g. US/EU/AMER/EMEA only).
    """
    loc = (location or "").lower().strip()

    # Explicit India location is always OK (avoiding Indiana / Indianapolis false positives)
    if "indiana" not in loc and "indianapolis" not in loc:
        if any(kw in loc for kw in INDIA_KEYWORDS):
            return True

    # If not remote and not in India, cannot work onsite
    if not remote:
        return False

    # For remote roles: check for negative geo-restrictions
    if any(kw in loc for kw in EXCLUDED_GEO_KEYWORDS):
        return False

    if loc in EXCLUDED_COUNTRY_RESTRICTED:
        return False

    # If remote with explicit global signals or empty location
    if loc in ("", "remote", "worldwide", "anywhere", "global", "work from anywhere", "wfa"):
        return True

    if any(kw in loc for kw in ("worldwide", "anywhere", "global", "wfa", "all countries")):
        return True

    # Check if location tokens specify a foreign country
    foreign_tokens = set(re.findall(r"\b[a-zA-Z]+\b", loc))
    if foreign_tokens & EXCLUDED_COUNTRY_RESTRICTED:
        return False

    return True


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _strip_html(text: str) -> str:
    """Remove HTML tags and collapse whitespace."""
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _is_remote(location: str) -> bool:
    """Heuristic: treat 'remote', blank, 'worldwide', 'anywhere' as remote=True."""
    loc = (location or "").lower().strip()
    return loc in ("", "remote", "worldwide", "anywhere", "global") or "remote" in loc


def _dedup(listings: list[dict]) -> list[dict]:
    """Remove duplicates by apply_url then (company, title)."""
    seen_urls: set[str] = set()
    seen_pairs: set[tuple[str, str]] = set()
    out: list[dict] = []
    for jd in listings:
        url = (jd.get("apply_url") or "").strip()
        pair = (
            jd.get("company", "").lower().strip(),
            jd.get("title", "").lower().strip(),
        )
        if url and url in seen_urls:
            continue
        if pair in seen_pairs:
            continue
        if url:
            seen_urls.add(url)
        seen_pairs.add(pair)
        out.append(jd)
    return out


def _jd(
    *,
    title: str,
    company: str,
    location: str,
    remote: bool,
    description: str,
    apply_url: str,
    source: str,
) -> dict[str, Any]:
    """Build a validated JD dict matching the canonical schema."""
    return {
        "title": title.strip(),
        "company": company.strip(),
        "location": location.strip(),
        "remote": remote,
        "description": _strip_html(description),
        "apply_url": apply_url.strip(),
        "source": source,
        "fetched_at": _now_iso(),
    }


# ── Greenhouse Fetcher ────────────────────────────────────────────────────────

def _fetch_single_greenhouse(slug: str) -> list[dict]:
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
    results: list[dict] = []
    try:
        resp = requests.get(url, headers=HEADERS, timeout=10)
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        data = resp.json()
        jobs = data.get("jobs", [])
        company_name = slug.replace("-", " ").title()
        for job in jobs:
            title = job.get("title", "").strip()
            if not title or not _is_tech_job(title):
                continue
            if _is_senior_only(title):
                continue
            loc_data = job.get("location", {})
            location = loc_data.get("name", "") if isinstance(loc_data, dict) else str(loc_data)
            remote = _is_remote(location)
            if not is_location_ok(location, remote):
                continue
            apply_url = job.get("absolute_url", "")
            if not apply_url:
                continue
            description = _strip_html(job.get("content", ""))
            if _requires_senior_experience(description):
                continue
            results.append(_jd(
                title=title,
                company=company_name,
                location=location,
                remote=remote,
                description=description,
                apply_url=apply_url,
                source="greenhouse",
            ))
    except Exception as e:
        logger.debug("greenhouse:%s error: %s", slug, e)
    return results


def fetch_greenhouse(companies: list[str] | None = None) -> list[dict]:
    companies = companies or GREENHOUSE_COMPANIES
    listings: list[dict] = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(_fetch_single_greenhouse, slug) for slug in companies]
        for f in as_completed(futures):
            listings.extend(f.result())
    logger.info("fetch_greenhouse — %d total listings", len(listings))
    return listings


# ── Ashby Fetcher ─────────────────────────────────────────────────────────────

def _fetch_single_ashby(slug: str) -> list[dict]:
    url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
    results: list[dict] = []
    try:
        resp = requests.get(url, headers=HEADERS, timeout=10)
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        data = resp.json()
        jobs = data.get("jobs", [])
        company_name = slug.replace("-", " ").title()
        for job in jobs:
            if not job.get("isListed", True):
                continue
            title = job.get("title", "").strip()
            if not title or not _is_tech_job(title):
                continue
            if _is_senior_only(title):
                continue
            location = job.get("location", "") or ""
            is_remote_flag = job.get("isRemote", False)
            workplace_type = (job.get("workplaceType") or "").lower()
            remote = is_remote_flag or "remote" in workplace_type or _is_remote(location)
            if not is_location_ok(location, remote):
                continue
            apply_url = job.get("applyUrl", "") or job.get("jobUrl", "")
            if not apply_url:
                continue
            description = _strip_html(
                job.get("descriptionHtml", "") or job.get("descriptionPlain", "")
            )
            if _requires_senior_experience(description):
                continue
            results.append(_jd(
                title=title,
                company=company_name,
                location=location,
                remote=remote,
                description=description,
                apply_url=apply_url,
                source="ashby",
            ))
    except Exception as e:
        logger.debug("ashby:%s error: %s", slug, e)
    return results


def fetch_ashby(companies: list[str] | None = None) -> list[dict]:
    companies = companies or ASHBY_COMPANIES
    listings: list[dict] = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(_fetch_single_ashby, slug) for slug in companies]
        for f in as_completed(futures):
            listings.extend(f.result())
    logger.info("fetch_ashby — %d total listings", len(listings))
    return listings


# ── Lever Fetcher ─────────────────────────────────────────────────────────────

def _fetch_single_lever(slug: str) -> list[dict]:
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json&limit=50"
    results: list[dict] = []
    try:
        resp = requests.get(url, headers=HEADERS, timeout=10)
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        jobs = resp.json()
        if not isinstance(jobs, list):
            jobs = jobs.get("data", [])
        company_name = slug.replace("-", " ").title()
        for job in jobs:
            title = job.get("text", "").strip()
            if not title or not _is_tech_job(title):
                continue
            if _is_senior_only(title):
                continue
            categories = job.get("categories", {})
            location = categories.get("location", "") or categories.get("commitment", "")
            remote = _is_remote(location)
            if not is_location_ok(location, remote):
                continue
            apply_url = job.get("applyUrl", "") or job.get("hostedUrl", "")
            if not apply_url:
                continue
            description = _strip_html(
                job.get("descriptionBody", "") or job.get("description", "")
            )
            if _requires_senior_experience(description):
                continue
            results.append(_jd(
                title=title,
                company=company_name,
                location=location,
                remote=remote,
                description=description,
                apply_url=apply_url,
                source="lever",
            ))
    except Exception as e:
        logger.debug("lever:%s error: %s", slug, e)
    return results


def fetch_lever(companies: list[str] | None = None) -> list[dict]:
    companies = companies or LEVER_COMPANIES
    listings: list[dict] = []
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(_fetch_single_lever, slug) for slug in companies]
        for f in as_completed(futures):
            listings.extend(f.result())
    logger.info("fetch_lever — %d total listings", len(listings))
    return listings


# ── Aggregators (RemoteOK, Remotive, HackerNews) ──────────────────────────────

@retry(
    wait=wait_exponential(multiplier=1, min=2, max=10),
    stop=stop_after_attempt(3),
    retry=retry_if_exception_type(requests.RequestException)
)
def fetch_remoteok() -> list[dict]:
    resp = requests.get(
        REMOTEOK_URL,
        params={"limit": REMOTEOK_LIMIT},
        headers=HEADERS,
        timeout=20,
    )
    resp.raise_for_status()
    raw: list[dict] = resp.json()

    listings: list[dict] = []
    for item in raw:
        if not isinstance(item, dict) or "slug" not in item:
            continue
        title = item.get("position", "").strip()
        company = item.get("company", "").strip()
        if not title or not company or not _is_tech_job(title) or _is_senior_only(title):
            continue
        description = item.get("description", "")
        if _requires_senior_experience(description):
            continue
        location = item.get("location", "") or ""
        apply_url = item.get("apply_url") or item.get("url", "")
        if not apply_url:
            continue
        listings.append(_jd(
            title=title,
            company=company,
            location=location,
            remote=True,
            description=description,
            apply_url=apply_url,
            source="remoteok",
        ))
    logger.info("fetch_remoteok — %d listings", len(listings))
    return listings


@retry(
    wait=wait_exponential(multiplier=1, min=2, max=10),
    stop=stop_after_attempt(3),
    retry=retry_if_exception_type(requests.RequestException)
)
def fetch_remotive() -> list[dict]:
    resp = requests.get(
        REMOTIVE_URL,
        params={"limit": REMOTIVE_LIMIT, "category": "software-dev"},
        headers=HEADERS,
        timeout=20,
    )
    resp.raise_for_status()
    payload: dict = resp.json()
    raw: list[dict] = payload.get("jobs", [])

    listings: list[dict] = []
    for item in raw:
        title = item.get("title", "").strip()
        company = item.get("company_name", "").strip()
        if not title or not company or not _is_tech_job(title) or _is_senior_only(title):
            continue
        description = item.get("description", "")
        if _requires_senior_experience(description):
            continue
        location = item.get("candidate_required_location", "") or ""
        remote = _is_remote(location) or not location.strip()
        loc_lower = location.lower()
        is_worldwide = "worldwide" in loc_lower or "anywhere" in loc_lower
        if not is_worldwide and not is_location_ok(location, remote):
            continue
        apply_url = item.get("url", "")
        if not apply_url:
            continue
        listings.append(_jd(
            title=title,
            company=company,
            location=location,
            remote=remote,
            description=description,
            apply_url=apply_url,
            source="remotive",
        ))
    logger.info("fetch_remotive — %d listings", len(listings))
    return listings


@retry(
    wait=wait_exponential(multiplier=1, min=2, max=10),
    stop=stop_after_attempt(3),
    retry=retry_if_exception_type(requests.RequestException)
)
def fetch_jobicy() -> list[dict]:
    """Fetch 100% remote job listings from Jobicy API."""
    listings: list[dict] = []
    try:
        resp = requests.get(
            JOBICY_URL,
            params={"count": JOBICY_LIMIT, "industry": "engineering"},
            headers=HEADERS,
            timeout=20,
        )
        if resp.status_code != 200:
            logger.warning("fetch_jobicy — HTTP %s", resp.status_code)
            return []
        data = resp.json()
        jobs = data.get("jobs", [])
        for item in jobs:
            title = (item.get("jobTitle") or "").strip()
            company = (item.get("companyName") or "").strip()
            if not title or not company or not _is_tech_job(title) or _is_senior_only(title):
                continue
            location = item.get("jobGeo", "") or ""
            # Jobicy is 100% remote
            remote = True
            loc_lower = location.lower()
            is_worldwide = "anywhere" in loc_lower or "worldwide" in loc_lower or "global" in loc_lower
            if not is_worldwide and not is_location_ok(location, remote):
                continue
            description = _strip_html(item.get("jobDescription", ""))
            if _requires_senior_experience(description):
                continue
            apply_url = (item.get("url") or "").strip()
            if not apply_url:
                continue
            listings.append(_jd(
                title=title,
                company=company,
                location=location,
                remote=remote,
                description=description,
                apply_url=apply_url,
                source="jobicy",
            ))
    except Exception as e:
        logger.error("fetch_jobicy — error: %s", e)

    logger.info("fetch_jobicy — %d listings", len(listings))
    return listings


@retry(
    wait=wait_exponential(multiplier=1, min=2, max=10),
    stop=stop_after_attempt(3),
    retry=retry_if_exception_type(requests.RequestException)
)
def fetch_arbeitnow() -> list[dict]:
    """Fetch tech job listings from Arbeitnow API."""
    listings: list[dict] = []
    try:
        resp = requests.get(ARBEITNOW_URL, headers=HEADERS, timeout=20)
        if resp.status_code != 200:
            logger.warning("fetch_arbeitnow — HTTP %s", resp.status_code)
            return []
        data = resp.json()
        jobs = data.get("data", [])
        for item in jobs:
            title = (item.get("title") or "").strip()
            company = (item.get("company_name") or "").strip()
            if not title or not company or not _is_tech_job(title) or _is_senior_only(title):
                continue
            location = (item.get("location") or "").strip()
            remote = bool(item.get("remote", False)) or _is_remote(location)
            loc_lower = location.lower()
            is_worldwide = "anywhere" in loc_lower or "worldwide" in loc_lower or "global" in loc_lower
            if not is_worldwide and not is_location_ok(location, remote):
                continue
            description = _strip_html(item.get("description", ""))
            if _requires_senior_experience(description):
                continue
            apply_url = (item.get("url") or "").strip()
            if not apply_url:
                continue
            listings.append(_jd(
                title=title,
                company=company,
                location=location,
                remote=remote,
                description=description,
                apply_url=apply_url,
                source="arbeitnow",
            ))
    except Exception as e:
        logger.error("fetch_arbeitnow — error: %s", e)

    logger.info("fetch_arbeitnow — %d listings", len(listings))
    return listings


def fetch_hackernews() -> list[dict]:
    listings: list[dict] = []
    try:
        url = "https://hn.algolia.com/api/v1/search_by_date"
        params = {"tags": "story,author_whoishiring", "query": "Ask HN: Who is hiring?", "hitsPerPage": 1}
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        hits = resp.json().get("hits", [])
        if not hits:
            logger.warning("hackernews: no 'Who is hiring' thread found")
            return []
        story_id = hits[0]["objectID"]

        c_url = "https://hn.algolia.com/api/v1/search"
        c_params = {"tags": f"comment,story_{story_id}", "hitsPerPage": 1000}
        c_resp = requests.get(c_url, params=c_params, timeout=15)
        c_resp.raise_for_status()
        all_comments = c_resp.json().get("hits", [])

        ats_pattern = re.compile(r"greenhouse\.io|lever\.co|ashbyhq\.com", re.IGNORECASE)
        url_regex = re.compile(r"https?://\S+")

        import html

        for comment in all_comments:
            raw_text = comment.get("comment_text", "")
            text = html.unescape(raw_text)
            if not ats_pattern.search(text):
                continue
            stripped = _strip_html(text)
            if _requires_senior_experience(stripped):
                continue
            first_line = stripped.split("\n")[0].strip()
            parts = [p.strip() for p in first_line.split("|")]
            company = parts[0] if parts else "HN Startup"

            if len(parts) >= 2 and _is_tech_job(parts[1]):
                title = parts[1]
            elif _is_tech_job(first_line):
                title = first_line[:100]
            else:
                title = "Software Engineer"

            if _is_senior_only(title):
                continue

            location = parts[2] if len(parts) >= 3 else "Remote"
            is_remote_hn = "remote" in first_line.lower()

            if not is_location_ok(location, is_remote_hn):
                continue

            from scripts.auto_apply_ats import _ATS_DOMAIN_MAP, validate_ats_job_url

            for raw_url in url_regex.findall(text):
                clean_url = raw_url.strip(').,;:>"\'')
                if any(domain in clean_url for domain in _ATS_DOMAIN_MAP.keys()):
                    valid, _ = validate_ats_job_url(clean_url)
                    if not valid:
                        continue
                    listings.append(_jd(
                        title=title,
                        company=company,
                        location=location,
                        remote=is_remote_hn,
                        description=stripped,
                        apply_url=clean_url,
                        source="hackernews",
                    ))
                    break
    except Exception as e:
        logger.error("fetch_hackernews — error: %s", e)

    logger.info("fetch_hackernews — %d listings", len(listings))
    return listings


# ── Himalayas Remote Fetcher ──────────────────────────────────────────────────

@retry(
    wait=wait_exponential(multiplier=1, min=2, max=10),
    stop=stop_after_attempt(3),
    retry=retry_if_exception_type(requests.RequestException)
)
def fetch_himalayas(limit: int = 40) -> list[dict]:
    """
    Fetch remote tech jobs from Himalayas API.
    Queries entry-level and junior roles, normalizes to canonical JD schema,
    and filters for tech relevance, location suitability, and experience caps.
    """
    listings: list[dict] = []
    offsets = [0, 20] if limit > 20 else [0]
    for offset in offsets:
        try:
            params = {"seniority": "entry-level,junior", "offset": offset}
            resp = requests.get(HIMALAYAS_URL, params=params, headers=HEADERS, timeout=20)
            if resp.status_code != 200:
                logger.warning("fetch_himalayas — HTTP %s at offset %s", resp.status_code, offset)
                continue
            data = resp.json()
            jobs = data.get("jobs", [])
            for item in jobs:
                title = (item.get("title") or "").strip()
                company = (item.get("companyName") or "").strip()
                if not title or not company or not _is_tech_job(title) or _is_senior_only(title):
                    continue
                loc_restrictions = item.get("locationRestrictions") or []
                location = ", ".join(loc_restrictions) if loc_restrictions else "Worldwide"
                remote = True
                if not is_location_ok(location, remote):
                    continue
                description = _strip_html(item.get("description", "") or item.get("excerpt", ""))
                if _requires_senior_experience(description):
                    continue
                apply_url = (item.get("applicationLink") or "").strip()
                if not apply_url:
                    continue
                listings.append(_jd(
                    title=title,
                    company=company,
                    location=location,
                    remote=remote,
                    description=description,
                    apply_url=apply_url,
                    source="himalayas",
                ))
        except Exception as e:
            logger.error("fetch_himalayas — offset %s error: %s", offset, e)
    logger.info("fetch_himalayas — %d listings", len(listings))
    return listings


# ── SimplifyJobs Early-Career Feed ────────────────────────────────────────────

@retry(
    wait=wait_exponential(multiplier=1, min=2, max=10),
    stop=stop_after_attempt(3),
    retry=retry_if_exception_type(requests.RequestException)
)
def fetch_simplify_jobs() -> list[dict]:
    """
    Fetch active new-grad / entry-level positions from SimplifyJobs public feed.
    Pre-validates that apply URLs point to legitimate ATS postings (Ashby, Greenhouse, Lever).
    """
    from scripts.auto_apply_ats import validate_ats_job_url
    listings: list[dict] = []
    try:
        resp = requests.get(SIMPLIFY_URL, headers=HEADERS, timeout=25)
        if resp.status_code != 200:
            logger.warning("fetch_simplify_jobs — HTTP %s", resp.status_code)
            return []
        items = resp.json()
        for item in items:
            if not item.get("active") or not item.get("is_visible", True):
                continue
            title = (item.get("title") or "").strip()
            company = (item.get("company_name") or "").strip()
            if not title or not company or not _is_tech_job(title) or _is_senior_only(title):
                continue
            locations = item.get("locations") or []
            loc_str = ", ".join(locations) if locations else "Remote"
            remote = _is_remote(loc_str) or any("remote" in l.lower() for l in locations)
            if not is_location_ok(loc_str, remote):
                continue
            apply_url = (item.get("url") or "").strip()
            is_valid, _ = validate_ats_job_url(apply_url)
            if not is_valid:
                continue
            category = item.get("category", "Software Engineering")
            description = f"Company: {company}. Role: {title}. Category: {category}. Locations: {loc_str}."
            listings.append(_jd(
                title=title,
                company=company,
                location=loc_str,
                remote=remote,
                description=description,
                apply_url=apply_url,
                source="simplify",
            ))
    except Exception as e:
        logger.error("fetch_simplify_jobs — error: %s", e)
    logger.info("fetch_simplify_jobs — %d listings", len(listings))
    return listings


# ── Y-Combinator High-Growth Startups Discovery ────────────────────────────────

def fetch_yc_jobs(slugs: list[str] | None = None) -> list[dict]:
    """
    Discover tech jobs from high-growth Y-Combinator startups hosted on Ashby and Greenhouse.
    Concurrently queries posting APIs and normalizes them into canonical JDs.
    """
    slugs = slugs or YC_COMPANIES
    listings: list[dict] = []

    def _query_yc_board(slug: str) -> list[dict]:
        found: list[dict] = []
        ashby_jobs = _fetch_single_ashby(slug)
        for j in ashby_jobs:
            j["source"] = "yc_startups"
            found.append(j)
        gh_jobs = _fetch_single_greenhouse(slug)
        for j in gh_jobs:
            j["source"] = "yc_startups"
            found.append(j)
        return found

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(_query_yc_board, slug) for slug in slugs]
        for f in as_completed(futures):
            listings.extend(f.result())

    listings = _dedup(listings)
    logger.info("fetch_yc_jobs — %d total listings from %d YC companies", len(listings), len(slugs))
    return listings


# ── Main fetch_all with Source Balancing ─────────────────────────────────────

def fetch_all() -> list[dict]:
    """
    Fetch job listings from all sources, filter against processed URLs, and
    return a source-balanced, remote-first interleaved list.

    Round-Robin Interleaving ensures Greenhouse DOES NOT starve Ashby, Lever,
    Jobicy, Arbeitnow, RemoteOK, Remotive, or HackerNews!
    """
    sources = [
        ("ashby",       fetch_ashby),
        ("lever",       fetch_lever),
        ("jobicy",      fetch_jobicy),
        ("arbeitnow",   fetch_arbeitnow),
        ("greenhouse",  fetch_greenhouse),
        ("remoteok",    fetch_remoteok),
        ("remotive",    fetch_remotive),
        ("hackernews",  fetch_hackernews),
        ("himalayas",   fetch_himalayas),
        ("simplify",    fetch_simplify_jobs),
        ("yc_startups", fetch_yc_jobs),
    ]

    all_by_source: dict[str, list[dict]] = collections.defaultdict(list)

    for name, fn in sources:
        try:
            items = fn()
            logger.info("fetch:%s — retrieved %d items", name, len(items))
            all_by_source[name] = items
        except Exception as exc:
            try:
                from scripts.log_and_notify import log_failure
                log_failure(name, exc)
            except Exception:
                logger.error("fetch:%s — error: %s", name, exc, exc_info=True)

    # Flatten and deduplicate
    combined = []
    for items in all_by_source.values():
        combined.extend(items)
    deduped = _dedup(combined)

    # Validate all ATS URLs unconditionally before queuing
    from scripts.auto_apply_ats import validate_ats_job_url
    before_val_len = len(deduped)
    deduped = [jd for jd in deduped if validate_ats_job_url(jd.get("apply_url", ""))[0]]
    if before_val_len != len(deduped):
        logger.info("fetch_all — filtered out %d invalid/board-only ATS URLs", before_val_len - len(deduped))

    # Filter against persistent store (Google Sheets)
    try:
        from scripts.log_and_notify import get_processed_urls
        processed_urls = get_processed_urls()
        if processed_urls:
            before_len = len(deduped)
            deduped = [jd for jd in deduped if jd.get("apply_url", "") not in processed_urls]
            logger.info("fetch_all — filtered out %d already processed URLs", before_len - len(deduped))
    except Exception:
        logger.warning("fetch_all — could not load processed URLs; continuing without filter")

    # Group deduplicated list by source
    grouped: dict[str, list[dict]] = collections.defaultdict(list)
    for jd in deduped:
        grouped[jd.get("source", "other")].append(jd)

    # Sort each source group: remote jobs first!
    for src_list in grouped.values():
        src_list.sort(key=lambda j: 0 if j.get("remote") else 1)

    # Round-Robin Interleave across sources
    source_order = [
        "ashby", "lever", "jobicy", "arbeitnow", "remoteok", "remotive",
        "greenhouse", "hackernews", "himalayas", "simplify", "yc_startups",
    ]
    interleaved: list[dict] = []
    max_count = max((len(lst) for lst in grouped.values()), default=0)

    for i in range(max_count):
        for src in source_order:
            if i < len(grouped[src]):
                interleaved.append(grouped[src][i])

    # Remote-First Prioritization: place all remote listings at the front of the queue
    remote_jobs = [j for j in interleaved if j.get("remote")]
    onsite_jobs = [j for j in interleaved if not j.get("remote")]
    interleaved = remote_jobs + onsite_jobs

    logger.info(
        "fetch_all — %d total fetched → %d deduplicated & balanced across sources (%d remote, %d onsite): %s",
        len(combined),
        len(interleaved),
        len(remote_jobs),
        len(onsite_jobs),
        {k: len(v) for k, v in grouped.items()},
    )
    return interleaved


if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    if len(sys.argv) == 2:
        source = sys.argv[1].lower()
        fn_map = {
            "greenhouse":  fetch_greenhouse,
            "lever":       fetch_lever,
            "ashby":       fetch_ashby,
            "hackernews":  fetch_hackernews,
            "remoteok":    fetch_remoteok,
            "remotive":    fetch_remotive,
            "jobicy":      fetch_jobicy,
            "arbeitnow":   fetch_arbeitnow,
            "himalayas":   fetch_himalayas,
            "simplify":    fetch_simplify_jobs,
            "yc_startups": fetch_yc_jobs,
        }
        if source not in fn_map:
            print(f"Unknown source '{source}'. Choose from: {', '.join(fn_map)}")
            sys.exit(1)
        results = fn_map[source]()
    else:
        results = fetch_all()

    print(f"\nSample of first 10 balanced jobs:")
    for idx, r in enumerate(results[:10]):
        print(f"{idx+1}. [{r.get('source')}] {r.get('company')} — {r.get('title')} (remote={r.get('remote')}, loc={r.get('location')})")
    print(f"\n... {len(results)} total listings")
