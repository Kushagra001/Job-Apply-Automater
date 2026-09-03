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
REMOTEOK_LIMIT = 150
REMOTIVE_LIMIT = 150

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
    # QA / Testing
    "qa engineer", "test engineer", "quality assurance", "automation engineer", "sdet",
    # Database / Systems
    "database", "dba", "postgres", "mysql", "embedded", "firmware", "systems engineer",
    # Product / Technical adjacent
    "product manager", "product designer", "solutions engineer", "developer advocate",
    "technical support", "technical writer", "integrations",
])

# Senior-only keywords: excluded to prioritize freshers (0-2 yrs)
SENIOR_ONLY_TITLE_KEYWORDS: frozenset[str] = frozenset([
    "staff", "principal", "distinguished", "fellow",
    "vp ", "vice president", "director", "head of", "chief",
    "c-level", "cto", "cso", "ciso",
    "senior manager", "engineering manager", "em,", " em ", "(em)",
    "lead ", "architect",
])

# Explicit entry/fresher signals: always kept
JUNIOR_TITLE_KEYWORDS: frozenset[str] = frozenset([
    "junior", "jr.", " jr ", "entry", "associate engineer", "intern", "graduate",
    "new grad", "early career", "0-2", "0 to 2", "fresher",
])

# India target locations
INDIA_KEYWORDS: frozenset[str] = frozenset([
    "india", " in ", "india,", "bangalore", "bengaluru", "mumbai",
    "pune", "hyderabad", "chennai", "delhi", "gurugram", "noida",
    "kolkata", "ahmedabad", "remote india", "india remote",
])


def _is_tech_job(title: str) -> bool:
    """Return True if the job title contains at least one tech keyword."""
    t = title.lower()
    return any(kw in t for kw in TECH_TITLE_KEYWORDS)


def _is_senior_only(title: str) -> bool:
    """Return True if title indicates an explicitly senior/staff/director role."""
    t = title.lower()
    # If title has director, vp, staff, principal, lead, architect, etc -> senior
    if any(kw in t for kw in SENIOR_ONLY_TITLE_KEYWORDS):
        return True
    return False


def is_location_ok(location: str, remote: bool) -> bool:
    """Return True if the job is remote OR located in India."""
    if remote:
        return True
    loc = (location or "").lower()
    return any(kw in loc for kw in INDIA_KEYWORDS)


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
        location = item.get("location", "") or ""
        apply_url = item.get("apply_url") or item.get("url", "")
        if not apply_url:
            continue
        listings.append(_jd(
            title=title,
            company=company,
            location=location,
            remote=True,
            description=item.get("description", ""),
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
            description=item.get("description", ""),
            apply_url=apply_url,
            source="remotive",
        ))
    logger.info("fetch_remotive — %d listings", len(listings))
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
        from scripts.auto_apply_ats import _ATS_DOMAIN_MAP

        for comment in all_comments:
            raw_text = comment.get("comment_text", "")
            text = html.unescape(raw_text)
            if not ats_pattern.search(text):
                continue
            stripped = _strip_html(text)
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

            for raw_url in url_regex.findall(text):
                clean_url = raw_url.strip(').,;:>"\'')
                if any(domain in clean_url for domain in _ATS_DOMAIN_MAP.keys()):
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


# ── Main fetch_all with Source Balancing ─────────────────────────────────────

def fetch_all() -> list[dict]:
    """
    Fetch job listings from all sources, filter against processed URLs, and
    return a source-balanced, interleaved list.

    Round-Robin Interleaving ensures Greenhouse DOES NOT starve Ashby, Lever,
    RemoteOK, Remotive, or HackerNews!
    """
    sources = [
        ("ashby",      fetch_ashby),
        ("lever",      fetch_lever),
        ("greenhouse", fetch_greenhouse),
        ("remoteok",   fetch_remoteok),
        ("remotive",   fetch_remotive),
        ("hackernews", fetch_hackernews),
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

    # Filter against persistent store (Google Sheets)
    try:
        from scripts.log_and_notify import get_processed_urls
        processed_urls = get_processed_urls()
        if processed_urls:
            before_len = len(deduped)
            deduped = [jd for jd in deduped if jd.get("apply_url") not in processed_urls]
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

    # Round-Robin Interleave across sources:
    # [Ashby_0, Lever_0, RemoteOK_0, Remotive_0, Greenhouse_0, HN_0, Ashby_1, ...]
    source_order = ["ashby", "lever", "remoteok", "remotive", "greenhouse", "hackernews"]
    interleaved: list[dict] = []
    max_count = max((len(lst) for lst in grouped.values()), default=0)

    for i in range(max_count):
        for src in source_order:
            if i < len(grouped[src]):
                interleaved.append(grouped[src][i])

    logger.info(
        "fetch_all — %d total fetched → %d deduplicated & balanced across sources: %s",
        len(combined),
        len(interleaved),
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
            "greenhouse": fetch_greenhouse,
            "lever":      fetch_lever,
            "ashby":      fetch_ashby,
            "hackernews": fetch_hackernews,
            "remoteok":   fetch_remoteok,
            "remotive":   fetch_remotive,
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
