"""
fetch_jds.py — Job Discovery
=============================
Fetches job listings from all configured sources, normalizes each listing to the
canonical JD schema defined in Agents.md, and returns a deduplicated list.

Sources
-------
API (no browser required):
  - RemoteOK   — https://remoteok.com/api
  - Remotive    — https://remotive.com/api/remote-jobs
  - Himalayas   — https://himalayas.app/jobs/api
  - Arbeitnow   — https://arbeitnow.com/api/job-board-api

Output schema per listing:
  {
    "title":      str,
    "company":    str,
    "location":   str,
    "remote":     bool,
    "description": str,
    "apply_url":  str,
    "source":     "remoteok | remotive | himalayas | arbeitnow",
    "fetched_at": ISO8601 str
  }

Deduplication key: normalized (company.lower().strip(), title.lower().strip())
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

REMOTEOK_URL = "https://remoteok.com/api"
REMOTIVE_URL = "https://remotive.com/api/remote-jobs"
HIMALAYAS_URL = "https://himalayas.app/jobs/api"
ARBEITNOW_URL = "https://arbeitnow.com/api/job-board-api"
REMOTEOK_LIMIT = 100          # max listings per fetch
REMOTIVE_LIMIT = 100
HIMALAYAS_LIMIT = 100
ARBEITNOW_LIMIT = 100
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    )
}


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
    """Remove duplicates by normalized (company, title)."""
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    for jd in listings:
        key = (
            jd.get("company", "").lower().strip(),
            jd.get("title", "").lower().strip(),
        )
        if key not in seen:
            seen.add(key)
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


# ── API sources ───────────────────────────────────────────────────────────────

@retry(
    wait=wait_exponential(multiplier=1, min=2, max=10),
    stop=stop_after_attempt(3),
    retry=retry_if_exception_type(requests.RequestException)
)
def fetch_remoteok() -> list[dict]:
    """Fetch listings from RemoteOK JSON API. No browser required.
    Selector version: N/A (JSON API).
    API field map:
      position → title, company, location, description (HTML),
      apply_url (= url), date
    First element of the array is a legal/metadata object — skipped via 'slug' check.
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
        # First element is a metadata/legal object — it has no 'slug'
        if "slug" not in item:
            continue
        title = item.get("position", "").strip()
        company = item.get("company", "").strip()
        if not title or not company:
            continue
        location = item.get("location", "") or ""
        apply_url = item.get("apply_url") or item.get("url", "")
        listings.append(
            _jd(
                title=title,
                company=company,
                location=location,
                remote=_is_remote(location),
                description=item.get("description", ""),
                apply_url=apply_url,
                source="remoteok",
            )
        )
    logger.debug("fetch_remoteok — raw=%d parsed=%d", len(raw), len(listings))
    return listings


@retry(
    wait=wait_exponential(multiplier=1, min=2, max=10),
    stop=stop_after_attempt(3),
    retry=retry_if_exception_type(requests.RequestException)
)
def fetch_remotive() -> list[dict]:
    """Fetch listings from Remotive JSON API. No browser required.
    Selector version: N/A (JSON API).
    API field map:
      title, company_name, candidate_required_location, description (HTML),
      url (= apply_url), publication_date
    """
    resp = requests.get(
        REMOTIVE_URL,
        params={"limit": REMOTIVE_LIMIT},
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
        location = item.get("candidate_required_location", "") or ""
        apply_url = item.get("url", "")
        listings.append(
            _jd(
                title=title,
                company=company,
                location=location,
                remote=_is_remote(location),
                description=item.get("description", ""),
                apply_url=apply_url,
                source="remotive",
            )
        )
    logger.debug("fetch_remotive — raw=%d parsed=%d", len(raw), len(listings))
    return listings


@retry(
    wait=wait_exponential(multiplier=1, min=2, max=10),
    stop=stop_after_attempt(3),
    retry=retry_if_exception_type(requests.RequestException)
)
def fetch_himalayas() -> list[dict]:
    """Fetch listings from Himalayas JSON API. No browser required."""
    resp = requests.get(
        HIMALAYAS_URL,
        params={"limit": HIMALAYAS_LIMIT},
        headers=HEADERS,
        timeout=20,
    )
    resp.raise_for_status()
    payload: dict = resp.json()
    raw: list[dict] = payload.get("jobs", [])

    listings: list[dict] = []
    for item in raw:
        title = item.get("title", "").strip()
        company = item.get("companyName", "").strip()
        if not title or not company:
            continue
        location = item.get("locationRestrictions", [])
        location_str = ", ".join(location) if isinstance(location, list) else str(location)
        apply_url = item.get("applicationLink", "")
        listings.append(
            _jd(
                title=title,
                company=company,
                location=location_str,
                remote=True,  # Himalayas is remote-first
                description=item.get("description", ""),
                apply_url=apply_url,
                source="himalayas",
            )
        )
    logger.debug("fetch_himalayas — raw=%d parsed=%d", len(raw), len(listings))
    return listings


@retry(
    wait=wait_exponential(multiplier=1, min=2, max=10),
    stop=stop_after_attempt(3),
    retry=retry_if_exception_type(requests.RequestException)
)
def fetch_arbeitnow() -> list[dict]:
    """Fetch listings from Arbeitnow JSON API. No browser required."""
    resp = requests.get(
        ARBEITNOW_URL,
        headers=HEADERS,
        timeout=20,
    )
    resp.raise_for_status()
    payload: dict = resp.json()
    raw: list[dict] = payload.get("data", [])

    # The API returns all items on the first page, so we limit client-side
    raw = raw[:ARBEITNOW_LIMIT]

    listings: list[dict] = []
    for item in raw:
        title = item.get("title", "").strip()
        company = item.get("company_name", "").strip()
        if not title or not company:
            continue
        location = item.get("location", "") or ""
        apply_url = item.get("url", "")
        listings.append(
            _jd(
                title=title,
                company=company,
                location=location,
                remote=item.get("remote", False) or _is_remote(location),
                description=item.get("description", ""),
                apply_url=apply_url,
                source="arbeitnow",
            )
        )
    logger.debug("fetch_arbeitnow — raw=%d parsed=%d", len(raw), len(listings))
    return listings


# ── Main entry ────────────────────────────────────────────────────────────────

def fetch_all() -> list[dict]:
    """
    Run all fetch functions, collect results, and return a deduplicated list.
    A failure in one source is logged and skipped — it does not abort others.
    """
    sources = [
        ("remoteok", fetch_remoteok),
        ("remotive", fetch_remotive),
        ("himalayas", fetch_himalayas),
        ("arbeitnow", fetch_arbeitnow),
    ]

    all_listings: list[dict] = []
    for name, fn in sources:
        try:
            listings = fn()
            logger.info("fetch:%s — got %d listings", name, len(listings))
            all_listings.extend(listings)
        except Exception as exc:
            # Import here to avoid circular import before log_and_notify exists
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
    except ImportError:
        pass
        
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
    # Optional: pass source name as arg to run a single fetcher
    # e.g. python fetch_jds.py remoteok
    if len(sys.argv) == 2:
        source = sys.argv[1].lower()
        fn_map = {
            "remoteok": fetch_remoteok,
            "remotive": fetch_remotive,
            "himalayas": fetch_himalayas,
            "arbeitnow": fetch_arbeitnow,
        }
        if source not in fn_map:
            print(f"Unknown source '{source}'. Choose from: {', '.join(fn_map)}")
            sys.exit(1)
        results = fn_map[source]()
    else:
        results = fetch_all()

    print(json.dumps(results, indent=2))
