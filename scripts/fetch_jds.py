"""
fetch_jds.py — Job Discovery
=============================
Fetches job listings from all configured sources, normalizes each listing to the
canonical JD schema defined in Agents.md, and returns a deduplicated list.

Sources
-------
Native ATS APIs (direct apply_url — no redirect required):
  - Greenhouse  — boards-api.greenhouse.io/v1/boards/{company}/jobs
  - Lever       — api.lever.co/v0/postings/{company}?mode=json

Aggregator APIs (broad coverage, filtered by keyword):
  - RemoteOK    — https://remoteok.com/api
  - Remotive    — https://remotive.com/api/remote-jobs?category=software-dev

Job Relevance Filter
---------------------
All listings pass through _is_tech_job(title) before entering the pipeline.
Only titles containing at least one keyword from TECH_TITLE_KEYWORDS are kept.
This prevents Handyman, Gardener, Courier, etc. from clogging the pipeline.

Output schema per listing:
  {
    "title":      str,
    "company":    str,
    "location":   str,
    "remote":     bool,
    "description": str,
    "apply_url":  str,
    "source":     str,
    "fetched_at": ISO8601 str
  }

Deduplication key: apply_url (primary), then (company, title) (fallback)
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

import requests
from dotenv import load_dotenv
from tenacity import retry, wait_exponential, stop_after_attempt, retry_if_exception_type

load_dotenv()
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

REMOTEOK_URL  = "https://remoteok.com/api"
REMOTIVE_URL  = "https://remotive.com/api/remote-jobs"
REMOTEOK_LIMIT  = 150
REMOTIVE_LIMIT  = 150

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    )
}

# ── ATS native sources — curated company lists ────────────────────────────────
# These boards return direct greenhouse.io / lever.co apply URLs.
# Add or remove companies freely — one bad slug just logs a warning and continues.

GREENHOUSE_COMPANIES = [
    # Verified working GH boards (audited 2026-08-31)
    "vercel",        # 90 jobs
    "planetscale",   # 10 jobs
    "postman",       # 64 jobs
    "airtable",      # 16 jobs
    "intercom",      # 124 jobs
    "descript",      # 8 jobs
    "mercury",       # 54 jobs
    "lattice",       # 10 jobs
    "brex",          # 294 jobs
    "typeform",      # 12 jobs
    # Additional known-good boards
    "stripe",        "square",       "robinhood",
    "coinbase",      "plaid",        "checkr",
    "gusto",         "zendesk",      "twilio",
    "okta",          "datadog",      "elastic",
    "segment",       "mixpanel",     "amplitude",
    "figma",         "dropbox",      "atlassian",
    "github",        "gitlab",       "hashicorp",
    "cloudflare",    "mongodb",      "confluent",
    "snowflake",     "databricks",   "dbt-labs",
    "hubspot",       "shopify",      "canva",
]

LEVER_COMPANIES = [
    # Verified active boards (audited 2026-08-31)
    "webflow",       "bubble",       "temporal",
    "scale-ai",      "labelbox",     "weights-and-biases",
    "deel",          "remote",       "oyster",
    "hotjar",        "maze",
    # Additional known-good lever boards
    "netflix",       "spotify",      "twitter",
    "lyft",          "pinterest",    "reddit",
    "asana",         "notion",       "duolingo",
    "grammarly",     "canva",        "coursera",
    "carta",         "benchling",    "toast",
    "opentable",     "instacart",    "doordash",
]

# ── Relevance keyword filter ───────────────────────────────────────────────────
# A job title must contain at least one of these tokens (case-insensitive) to
# be allowed into the pipeline. This blocks Handyman, Gardener, Courier, etc.

TECH_TITLE_KEYWORDS: frozenset[str] = frozenset([
    # Generic engineering
    "software", "engineer", "developer", "dev", "programmer", "coder",
    # Frontend
    "frontend", "front-end", "front end", "react", "next", "vue", "angular",
    "javascript", "typescript", "ui/ux", "ui engineer", "web developer",
    # Backend
    "backend", "back-end", "back end", "python", "node", "django", "fastapi",
    "golang", "go ", "rust", "java ", "spring", "rails", "ruby",
    # Full stack
    "fullstack", "full-stack", "full stack",
    # AI / ML / Data
    "ai ", "ml ", "llm", "machine learning", "deep learning", "data engineer",
    "data scientist", "nlp", "computer vision", "ai engineer",
    # Platform / DevOps / Cloud
    "devops", "platform", "infrastructure", "sre", "cloud", "kubernetes",
    "docker", "devsecops", "reliability",
    # Mobile
    "ios", "android", "mobile", "flutter", "react native",
    # Product / Design (close enough to be relevant)
    "product manager", "product designer", "ux designer", "ux researcher",
])


def _is_tech_job(title: str) -> bool:
    """Return True if the job title contains at least one tech keyword."""
    t = title.lower()
    return any(kw in t for kw in TECH_TITLE_KEYWORDS)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _strip_html(text: str) -> str:
    """Remove HTML tags and collapse whitespace."""
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _is_remote(location: str) -> bool:
    """Heuristic: treat 'remote', blank, or 'worldwide' as remote=True."""
    loc = (location or "").lower().strip()
    return loc in ("", "remote", "worldwide", "anywhere") or "remote" in loc


def _dedup(listings: list[dict]) -> list[dict]:
    """Remove duplicates by apply_url (primary) then normalized (company, title).

    apply_url is a stronger key than (company, title) because many boards
    post the same role with slightly different titles.
    """
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


# ── Native ATS sources ────────────────────────────────────────────────────────

def fetch_greenhouse(companies: list[str] | None = None) -> list[dict]:
    """
    Fetch open roles from each company's Greenhouse job board.
    Returns direct greenhouse.io apply URLs — no redirect needed.
    A failing company board is skipped silently.
    """
    companies = companies or GREENHOUSE_COMPANIES
    listings: list[dict] = []

    for slug in companies:
        url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
        try:
            resp = requests.get(url, headers=HEADERS, timeout=15)
            if resp.status_code == 404:
                logger.debug("greenhouse: board not found for slug '%s'", slug)
                continue
            resp.raise_for_status()
            data = resp.json()
            jobs = data.get("jobs", [])
            for job in jobs:
                title = job.get("title", "").strip()
                if not title or not _is_tech_job(title):
                    continue
                # Greenhouse location block
                loc_data = job.get("location", {})
                location = loc_data.get("name", "") if isinstance(loc_data, dict) else str(loc_data)
                apply_url = job.get("absolute_url", "")
                description = _strip_html(job.get("content", ""))
                listings.append(_jd(
                    title=title,
                    company=slug.replace("-", " ").title(),
                    location=location,
                    remote=_is_remote(location),
                    description=description,
                    apply_url=apply_url,
                    source="greenhouse",
                ))
            logger.debug("greenhouse:%s — %d jobs", slug, len(jobs))
        except Exception as e:
            logger.debug("greenhouse:%s — error: %s", slug, e)

    logger.info("fetch_greenhouse — %d total listings", len(listings))
    return listings


def fetch_lever(companies: list[str] | None = None) -> list[dict]:
    """
    Fetch open roles from each company's Lever job board.
    Returns direct lever.co apply URLs — no redirect needed.
    A failing company board is skipped silently.
    """
    companies = companies or LEVER_COMPANIES
    listings: list[dict] = []

    for slug in companies:
        url = f"https://api.lever.co/v0/postings/{slug}?mode=json&limit=50"
        try:
            resp = requests.get(url, headers=HEADERS, timeout=15)
            if resp.status_code == 404:
                logger.debug("lever: board not found for slug '%s'", slug)
                continue
            resp.raise_for_status()
            jobs = resp.json()
            if not isinstance(jobs, list):
                jobs = jobs.get("data", [])
            for job in jobs:
                title = job.get("text", "").strip()
                if not title or not _is_tech_job(title):
                    continue
                categories = job.get("categories", {})
                location = categories.get("location", "") or categories.get("commitment", "")
                apply_url = job.get("applyUrl", "") or job.get("hostedUrl", "")
                description = _strip_html(
                    job.get("descriptionBody", "") or job.get("description", "")
                )
                listings.append(_jd(
                    title=title,
                    company=slug.replace("-", " ").title(),
                    location=location,
                    remote=_is_remote(location),
                    description=description,
                    apply_url=apply_url,
                    source="lever",
                ))
            logger.debug("lever:%s — %d jobs", slug, len(jobs))
        except Exception as e:
            logger.debug("lever:%s — error: %s", slug, e)

    logger.info("fetch_lever — %d total listings", len(listings))
    return listings


# ── Aggregator sources (keyword-filtered) ─────────────────────────────────────

def fetch_hackernews() -> list[dict]:
    """
    Fetch comments from the latest 'Ask HN: Who is hiring?' thread and extract
    direct links to greenhouse.io, lever.co, and ashbyhq.com.
    """
    listings: list[dict] = []
    try:
        # Get latest thread posted by the official whoishiring account
        url = "https://hn.algolia.com/api/v1/search_by_date"
        params = {"tags": "story,author_whoishiring", "query": "Ask HN: Who is hiring?", "hitsPerPage": 1}
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        hits = resp.json().get("hits", [])
        if not hits:
            logger.warning("hackernews: no 'Who is hiring' thread found")
            return []
        story_id = hits[0]["objectID"]

        # Fetch all top-level comments in this thread
        c_url = "https://hn.algolia.com/api/v1/search"
        c_params = {"tags": f"comment,story_{story_id}", "hitsPerPage": 1000}
        c_resp = requests.get(c_url, params=c_params, timeout=15)
        c_resp.raise_for_status()
        all_comments = c_resp.json().get("hits", [])

        # Keep only comments that reference a supported ATS
        ats_pattern = re.compile(r"greenhouse\.io|lever\.co|ashbyhq\.com", re.IGNORECASE)
        url_regex = re.compile(r"https?://[^\s<\"]+")

        for comment in all_comments:
            text = comment.get("comment_text", "")
            if not ats_pattern.search(text):
                continue

            stripped = _strip_html(text)
            # HN format: "Company | Role | Location | Remote" on line 1
            first_line = stripped.split("\n")[0].strip()
            parts = [p.strip() for p in first_line.split("|")]
            company = parts[0] if parts else "HN Startup"

            # Extract role title from second pipe segment when it's a tech role
            if len(parts) >= 2 and _is_tech_job(parts[1]):
                title = parts[1]
            elif _is_tech_job(first_line):
                title = first_line[:100]
            else:
                title = "Software Engineer"

            location = parts[2] if len(parts) >= 3 else "Remote"
            is_remote = "remote" in first_line.lower()

            # Extract the first matching ATS URL from the comment
            from scripts.auto_apply_ats import _ATS_DOMAIN_MAP
            for raw_url in url_regex.findall(text):
                clean_url = raw_url.rstrip(").,;'\">")
                if any(domain in clean_url for domain in _ATS_DOMAIN_MAP.keys()):
                    listings.append(_jd(
                        title=title,
                        company=company,
                        location=location,
                        remote=is_remote,
                        description=stripped,
                        apply_url=clean_url,
                        source="hackernews",
                    ))
                    break  # One job per comment

    except Exception as e:
        logger.error("fetch_hackernews — error: %s", e)

    logger.info("fetch_hackernews — %d total listings", len(listings))
    return listings




@retry(
    wait=wait_exponential(multiplier=1, min=2, max=10),
    stop=stop_after_attempt(3),
    retry=retry_if_exception_type(requests.RequestException)
)
def fetch_remoteok() -> list[dict]:
    """Fetch listings from RemoteOK JSON API. Keyword-filtered to tech jobs only.
    RemoteOK sometimes provides direct ATS apply_urls — we keep whatever they give us.
    """
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
        if "slug" not in item:
            continue
        title = item.get("position", "").strip()
        company = item.get("company", "").strip()
        if not title or not company:
            continue
        # Keyword filter — skip Handyman, Gardener, Courier etc.
        if not _is_tech_job(title):
            continue
        location = item.get("location", "") or ""
        apply_url = item.get("apply_url") or item.get("url", "")
        listings.append(_jd(
            title=title,
            company=company,
            location=location,
            remote=_is_remote(location),
            description=item.get("description", ""),
            apply_url=apply_url,
            source="remoteok",
        ))
    logger.debug("fetch_remoteok — raw=%d parsed=%d", len(raw), len(listings))
    return listings


@retry(
    wait=wait_exponential(multiplier=1, min=2, max=10),
    stop=stop_after_attempt(3),
    retry=retry_if_exception_type(requests.RequestException)
)
def fetch_remotive() -> list[dict]:
    """Fetch software-dev listings from Remotive JSON API. Category-filtered at API level,
    then keyword-filtered locally for safety.
    """
    resp = requests.get(
        REMOTIVE_URL,
        # Restrict to software dev category at the API level — reduces irrelevant results
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
        if not title or not company:
            continue
        if not _is_tech_job(title):
            continue
        location = item.get("candidate_required_location", "") or ""
        apply_url = item.get("url", "")
        listings.append(_jd(
            title=title,
            company=company,
            location=location,
            remote=_is_remote(location),
            description=item.get("description", ""),
            apply_url=apply_url,
            source="remotive",
        ))
    logger.debug("fetch_remotive — raw=%d parsed=%d", len(raw), len(listings))
    return listings


# ── Main entry ────────────────────────────────────────────────────────────────

def fetch_all() -> list[dict]:
    """
    Run all fetch functions, collect results, and return a deduplicated list.
    A failure in one source is logged and skipped — it does not abort others.

    Source priority:
      1. Greenhouse / Lever — native ATS URLs, curated companies, tech-only
      2. Hacker News — direct ATS URLs from "Who is Hiring" thread
      3. RemoteOK / Remotive — broad aggregators, keyword-filtered
    """
    sources = [
        ("greenhouse", fetch_greenhouse),
        ("lever", fetch_lever),
        ("hackernews", fetch_hackernews),
        ("remoteok", fetch_remoteok),
        ("remotive", fetch_remotive),
    ]

    all_listings: list[dict] = []
    for name, fn in sources:
        try:
            listings = fn()
            logger.info("fetch:%s — got %d listings", name, len(listings))
            all_listings.extend(listings)
        except Exception as exc:
            try:
                from scripts.log_and_notify import log_failure
                log_failure(name, exc)
            except Exception:
                logger.error("fetch:%s — error: %s", name, exc, exc_info=True)

    deduped = _dedup(all_listings)

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

    logger.info(
        "fetch_all — %d total → %d final after dedup and persistent filter",
        len(all_listings),
        len(deduped),
    )
    return deduped


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
            "lever": fetch_lever,
            "hackernews": fetch_hackernews,
            "remoteok": fetch_remoteok,
            "remotive": fetch_remotive,
        }
        if source not in fn_map:
            print(f"Unknown source '{source}'. Choose from: {', '.join(fn_map)}")
            sys.exit(1)
        results = fn_map[source]()
    else:
        results = fetch_all()

    print(json.dumps(results[:5], indent=2))
    print(f"\n... {len(results)} total listings")
