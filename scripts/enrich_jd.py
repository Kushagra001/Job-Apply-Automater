"""
enrich_jd.py — Zero-Config Job Description Enrichment Engine
============================================================
Inspired by Agent-Reach's universal reading and platform routing philosophy:
When listings arrive with thin descriptions (< 250 characters, e.g. from
SimplifyJobs, HackerNews, or high-level aggregators), this engine retrieves the
full job description (requirements, tech stacks, responsibilities) before
Groq scoring and resume tailoring.

Resolvers:
1. Greenhouse API : boards-api.greenhouse.io/v1/boards/{company}/jobs/{id}
2. Lever API      : api.lever.co/v0/postings/{company}/{id}?mode=json
3. Ashby API      : api.ashbyhq.com/posting-api/job-board/{company}
4. Jina Reader    : https://r.jina.ai/{url} (Universal zero-token markdown proxy)
"""

from __future__ import annotations

import html
import json
import logging
import re
import urllib.parse
import urllib.request
from typing import Any
import requests

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    )
}

MAX_DESCRIPTION_CHARS = 4000


def _strip_html(raw_html: str) -> str:
    """Strip HTML tags, unescape entities, and collapse excess whitespace."""
    if not raw_html:
        return ""
    text = re.sub(r"<script[^>]*>[\s\S]*?</script>", " ", raw_html, flags=re.IGNORECASE)
    text = re.sub(r"<style[^>]*>[\s\S]*?</style>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+([.,!?;:])", r"\1", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def resolve_greenhouse(apply_url: str) -> str:
    """Retrieve full description from Greenhouse single-job API."""
    try:
        parsed = urllib.parse.urlparse(apply_url)
        path_parts = [p for p in parsed.path.split("/") if p]
        
        # Check query string for gh_jid=
        job_id = None
        qs = urllib.parse.parse_qs(parsed.query)
        if "gh_jid" in qs:
            job_id = qs["gh_jid"][0]
        elif "jobs" in path_parts:
            idx = path_parts.index("jobs")
            if idx + 1 < len(path_parts):
                job_id = path_parts[idx + 1]

        # Determine company slug
        company_slug = None
        if path_parts and path_parts[0] not in ("jobs", "embed"):
            company_slug = path_parts[0]

        if not job_id:
            return ""

        # If company not in path, try host or fallback
        if not company_slug:
            host_parts = parsed.netloc.split(".")
            if len(host_parts) >= 2 and host_parts[0] not in ("boards", "job-boards", "www"):
                company_slug = host_parts[0]

        if not company_slug:
            return ""

        api_url = f"https://boards-api.greenhouse.io/v1/boards/{company_slug}/jobs/{job_id}"
        resp = requests.get(api_url, headers=HEADERS, timeout=8)
        if resp.status_code == 200:
            data = resp.json()
            content = data.get("content", "")
            if content:
                return _strip_html(content)
    except Exception as e:
        logger.debug("resolve_greenhouse failed for %s: %s", apply_url, e)
    return ""


def resolve_lever(apply_url: str) -> str:
    """Retrieve full description from Lever postings API."""
    try:
        parsed = urllib.parse.urlparse(apply_url)
        path_parts = [p for p in parsed.path.split("/") if p]
        if len(path_parts) >= 2:
            company_slug = path_parts[0]
            job_id = path_parts[1]
            api_url = f"https://api.lever.co/v0/postings/{company_slug}/{job_id}?mode=json"
            resp = requests.get(api_url, headers=HEADERS, timeout=8)
            if resp.status_code == 200:
                data = resp.json()
                desc = data.get("descriptionPlain", "") or _strip_html(data.get("description", ""))
                additional = data.get("additionalPlain", "") or _strip_html(data.get("additional", ""))
                combined = f"{desc}\n\n{additional}".strip()
                if combined:
                    return combined
    except Exception as e:
        logger.debug("resolve_lever failed for %s: %s", apply_url, e)
    return ""


def resolve_ashby(apply_url: str) -> str:
    """Retrieve full description from Ashby job-board API."""
    try:
        parsed = urllib.parse.urlparse(apply_url)
        path_parts = [p for p in parsed.path.split("/") if p]
        if len(path_parts) >= 2:
            company_slug = path_parts[0]
            job_id = path_parts[1]
            api_url = f"https://api.ashbyhq.com/posting-api/job-board/{company_slug}"
            resp = requests.get(api_url, headers=HEADERS, timeout=8)
            if resp.status_code == 200:
                jobs = resp.json().get("jobs", [])
                for j in jobs:
                    if j.get("id") == job_id or job_id in (j.get("jobUrl") or "") or job_id in (j.get("applyUrl") or ""):
                        raw = j.get("descriptionPlain") or j.get("descriptionHtml") or ""
                        if raw:
                            return _strip_html(raw)
    except Exception as e:
        logger.debug("resolve_ashby failed for %s: %s", apply_url, e)
    return ""


def resolve_jina_reader(apply_url: str) -> str:
    """
    Universal fallback reader via Jina Reader (https://r.jina.ai/{url}).
    Extracts structured markdown without requiring headless browsers or API keys.
    """
    try:
        jina_url = f"https://r.jina.ai/{apply_url}"
        req = urllib.request.Request(
            jina_url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                "Accept": "text/plain",
                "X-Return-Format": "markdown",
            }
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read(64 * 1024).decode("utf-8", errors="replace")

        # Check for challenge or 404 warnings
        body_lower = body.lower()
        if any(marker in body_lower for marker in ("warning: target url returned error", "### page not found", "job not found", "attention required! | cloudflare")):
            return ""

        clean_text = re.sub(r"\n{3,}", "\n\n", body).strip()
        if len(clean_text) > 100:
            return clean_text
    except Exception as e:
        logger.debug("resolve_jina_reader failed for %s: %s", apply_url, e)
    return ""


def enrich_jd(jd: dict[str, Any]) -> dict[str, Any]:
    """
    Enrich a JD with full requirements and description text if its existing description is sparse.
    Returns the same JD dictionary with enriched 'description'.
    """
    desc = jd.get("description", "").strip()
    apply_url = jd.get("apply_url", "").strip()

    # If description is already sufficiently detailed, do not overwrite
    if len(desc) >= 250 or not apply_url:
        return jd

    enriched = ""
    host = urllib.parse.urlparse(apply_url).netloc.lower()

    # 1. Native ATS direct resolvers (fastest, structured, 0 false-positives)
    if "greenhouse.io" in host or "gh_jid=" in apply_url:
        enriched = resolve_greenhouse(apply_url)
    elif "lever.co" in host:
        enriched = resolve_lever(apply_url)
    elif "ashbyhq.com" in host:
        enriched = resolve_ashby(apply_url)

    # 2. Universal Jina Reader fallback (if native ATS resolver returned nothing)
    if not enriched:
        enriched = resolve_jina_reader(apply_url)

    # Update description if enrichment retrieved substantial text
    if enriched and len(enriched) > len(desc):
        # Cap length to prevent unbounded context growth
        if len(enriched) > MAX_DESCRIPTION_CHARS:
            enriched = enriched[:MAX_DESCRIPTION_CHARS] + "..."
        jd["description"] = enriched
        logger.info(
            "Enriched JD for '%s' at '%s' (expanded from %d to %d chars)",
            jd.get("title", ""),
            jd.get("company", ""),
            len(desc),
            len(enriched),
        )

    return jd
