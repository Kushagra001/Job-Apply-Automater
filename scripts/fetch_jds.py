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
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import email.utils
import html
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
DEFAULT_MAX_JOB_AGE_DAYS = 14

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    )
}

# ── ATS Curated Company Lists ─────────────────────────────────────────────────

GREENHOUSE_COMPANIES = [
    # Borderless / EOR-friendly remote infra & global SaaS (remote-first priority)
    "gitlab", "canonical", "cloudflare", "postman", "vercel", "airtable",
    "elastic", "grafanalabs", "planetscale", "intercom", "descript",
    "typeform", "mixpanel", "amplitude", "dbt-labs", "hashicorp",
    "chainguard", "automattic", "remote", "springboard", "xohealth",
    "valtech", "yipitdata", "bloomreach", "smartsheet", "togetherai", "enveritas",
    "sourcegraph", "sentry", "duckduckgo", "docker", "netlify", "digitalocean",
    "buffer", "zapier", "wikimedia", "mozilla", "redis", "timescale",
    "clickhouse", "starburst", "launchdarkly", "pagerduty", "newrelic",
    "hasura", "apollographql", "anyscale", "weightsbiases", "deepgram",
    "assemblyai", "glean", "hootsuite", "ghost", "monzo", "retool",
    "snyk", "segment", "circleci", "cockroachlabs",
    # Verified India tech / engineering hubs on Greenhouse
    "rubrik", "databricks", "fivetran", "druva", "commerceiq", "zscaler",
    "coinbase", "inmobi", "groww", "twilio", "datadog", "mongodb",
    "figma", "dropbox", "atlassian", "github", "confluent", "snowflake",
    "hubspot", "shopify", "canva", "stripe", "square", "robinhood",
    "plaid", "checkr", "gusto", "zendesk", "okta",
    "mercury", "lattice", "brex", "browserstack", "thoughtworks",
    "clevertap", "harness", "razorpay", "cars24", "urbancompany",
]

ASHBY_COMPANIES = [
    # Verified active Ashby boards (AI / modern devtools / India-remote & global remote)
    "posthog", "supabase", "railway", "airbyte", "render",
    "livekit", "triggerdev", "elevenlabs", "perplexity",
    "secfix", "firstwork", "freehand", "phantom", "prefect",
    "aiprise", "buildai", "interviewkickstart", "opendoctor", "runpod",
    "linear", "ramp", "clerk", "notion", "sentry",
    "temporal", "neon", "weaviate", "pinecone", "langchain",
    "llamaindex", "cohere", "huggingface", "grafana-labs",
    "cockroach-labs", "sourcegraph", "1password", "tailscale", "fly", "turso",
    "openai", "anthropic", "mistral", "together-ai", "scale-ai",
]

LEVER_COMPANIES = [
    # Verified active Lever boards (India SaaS/product + global remote tech)
    "fly", "fampay", "hevodata", "cred", "meesho", "paytm", "binance", "spotify",
    "ripple", "consensys", "chainalysis", "opensea", "polygon",
    "near", "chainlink", "matterlabs", "smallcase", "juspay",
    "dream11", "deliveroo", "booking", "quora",
]

YC_COMPANIES = [
    # Fast-growing YC AI and devtool startups on Ashby / Greenhouse
    "resend", "cursor", "firecrawl", "browserbase", "greptile",
    "baseten", "decagon", "tavus", "posthog", "dagger",
    "warp", "cartesia", "mendable", "superhuman", "modal-labs",
    "retool", "monzo", "anysphere",
]

# ── Relevance keyword filter ───────────────────────────────────────────────────

# Non-tech roles and explicit false-positive roles to exclude immediately
EXCLUDED_NON_TECH_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"\b(?:business\s+development|sales\s+development|bdr|sdr|account\s+executive|account\s+manager)\b", re.IGNORECASE),
    re.compile(r"\b(?:sales\s+representative|sales\s+agent|sales\s+specialist|hvac)\b", re.IGNORECASE),
    re.compile(r"\b(?:linguistic|translator|translation|interpreter|linguist)\b", re.IGNORECASE),
    re.compile(r"\b(?:recruiter|talent\s+acquisition|human\s+resources|people\s+ops)\b", re.IGNORECASE),
    re.compile(r"\b(?:copywriter|content\s+writer|technical\s+writer|seo\s+specialist|marketing\s+manager)\b", re.IGNORECASE),
    re.compile(r"\b(?:product\s+manager|product\s+designer|ui/ux\s+designer|graphic\s+designer)\b", re.IGNORECASE),
    re.compile(r"\b(?:aml|investigator|compliance\s+officer|anti-money\s+laundering|fraud\s+analyst)\b", re.IGNORECASE),
    re.compile(r"\b(?:paralegal|legal\s+counsel|general\s+counsel|attorney)\b", re.IGNORECASE),
    re.compile(r"\b(?:salesforce|servicenow|peoplesoft|netsuite|oracle\s+fusion|sap\s+(?:abap|hana|basis|fico))\b", re.IGNORECASE),
)

TECH_TITLE_KEYWORDS: frozenset[str] = frozenset([
    # Generic engineering
    "software", "engineer", "developer", "dev", "programmer", "coder",
    # Frontend / Web
    "frontend", "front-end", "front end", "react", "next.js", "vue", "angular",
    "javascript", "typescript", "ui engineer", "web developer",
    "svelte", "remix", "nuxt", "gatsby",
    # Backend
    "backend", "back-end", "back end", "python", "node", "django", "fastapi",
    "golang", "rust", "spring", "rails", "ruby",
    "elixir", "scala", "kotlin", "laravel",
    # Full stack
    "fullstack", "full-stack", "full stack",
    # AI / ML / Data
    "ai", "ml", "llm", "machine learning", "deep learning", "data engineer",
    "data scientist", "nlp", "computer vision", "ai engineer", "generative ai",
    "genai", "prompt engineer", "rag", "fine-tuning", "mlops",
    "data analyst", "analytics engineer", "business intelligence",
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
    "sdet", "software tester", "test analyst", "test automation", "qa tester", "automation tester",
    # Database / Systems
    "database", "dba", "postgres", "mysql", "embedded", "firmware", "systems engineer",
    # Solutions / Support / Integrations / Tech Adjacent
    "solutions engineer", "solution engineer", "developer advocate", "developer support",
    "technical support", "tech support", "integrations", "integration engineer",
    "support engineer", "application support", "client support engineer", "technical operations",
])

# Precompile word-boundary patterns for high performance and strict token isolation
_TECH_PATTERNS: list[re.Pattern] = [
    re.compile(r"\b" + re.escape(kw) + r"\b", re.IGNORECASE) for kw in TECH_TITLE_KEYWORDS
]

# Word-boundary patterns for seniority levels (e.g. SDE 2, SWE II, Level 3 vs SDE 1, SWE I)
SENIOR_LEVEL_PATTERN = re.compile(
    r"\b(?:sde|swe|engineer|developer|qa|scientist|analyst|specialist|consultant)\s*[-_ ]*(?:ii|iii|iv|v|2|3|4|5)\b|\b(?:level|ic|l)\s*[-_ ]*[2-6]\b|\b(?:level|ic)\s*[-_ ]*(?:ii|iii|iv|v)\b",
    re.IGNORECASE
)

JUNIOR_LEVEL_PATTERN = re.compile(
    r"\b(?:sde|swe|engineer|developer|qa|scientist|analyst)\s*[-_ ]*(?:i|1)\b|\b(?:level|ic|l)\s*[-_ ]*1\b|\b(?:level|ic)\s*[-_ ]*i\b",
    re.IGNORECASE
)

# Senior-only keywords: excluded to prioritize freshers (0-2 yrs)
SENIOR_ONLY_TITLE_KEYWORDS: frozenset[str] = frozenset([
    # Executive & leadership
    "staff", "principal", "distinguished", "fellow",
    "vp", "vice president", "director", "head of", "chief",
    "c-level", "cto", "cso", "ciso", "cio",
    "manager", "senior manager", "engineering manager", "em",
    "lead", "team lead", "tech lead", "lead engineer", "lead developer",
    "architect",
    # Senior / Mid-Senior explicit keywords
    "senior", "sr", "mid-senior", "mid senior", "mid-level", "mid level", "intermediate",
    # Non-technical managers
    "client relationships manager", "business development manager", "general manager",
    "operations manager", "branch manager",
])

# Explicit entry/fresher signals: always kept even if overlapping with senior tier unless super-senior
JUNIOR_TITLE_KEYWORDS: frozenset[str] = frozenset([
    "junior", "jr", "entry", "entry-level", "entry level",
    "associate", "associate engineer", "associate developer", "associate software",
    "intern", "internship", "graduate", "grad", "new grad", "early career",
    "0-2", "0 to 2", "fresher", "fresh graduate", "campus", "trainee",
])

# High-level management / architect titles that should never be overridden by junior tokens
SUPER_SENIOR: tuple[str, ...] = (
    "director", "vp", "vice president", "head of", "chief", "cto", "cio", "cso", "ciso",
    "architect", "staff", "principal", "distinguished", "fellow",
    "engineering manager", "senior manager",
)


def _compile_token_pattern(kw: str) -> re.Pattern:
    return re.compile(r"(?<!\w)" + re.escape(kw.strip().lower()) + r"(?!\w)", re.IGNORECASE)


_SUPER_SENIOR_PATTERNS: list[re.Pattern] = [_compile_token_pattern(kw) for kw in SUPER_SENIOR]
_SENIOR_ONLY_PATTERNS: list[re.Pattern] = [_compile_token_pattern(kw) for kw in SENIOR_ONLY_TITLE_KEYWORDS]
_JUNIOR_TITLE_PATTERNS: list[re.Pattern] = [_compile_token_pattern(kw) for kw in JUNIOR_TITLE_KEYWORDS]

# India target locations (matched with strict word boundaries to avoid Indiana / preposition false matches)
INDIA_KEYWORDS: frozenset[str] = frozenset([
    "india", "bangalore", "bengaluru", "mumbai", "pune", "hyderabad", "chennai",
    "delhi", "new delhi", "gurugram", "gurgaon", "noida", "kolkata", "ahmedabad",
    "jaipur", "rajasthan", "kochi", "cochin", "trivandrum", "thiruvananthapuram",
    "indore", "chandigarh", "remote india", "india remote",
])
_INDIA_PATTERNS: list[re.Pattern] = [_compile_token_pattern(kw) for kw in INDIA_KEYWORDS]

# Explicit foreign geo-fence phrases
EXCLUDED_GEO_KEYWORDS: tuple[str, ...] = (
    "us only", "u.s. only", "usa only", "united states only", "us-only",
    "remote - us", "remote (us)", "remote, us", "remote: us", "us remote",
    "remote - usa", "remote (usa)", "remote, usa", "remote: usa", "usa remote",
    "remote - united states", "remote (united states)", "remote, united states",
    "remote - canada", "remote (canada)", "remote, canada",
    "remote - uk", "remote (uk)", "remote, uk",
    "remote - europe", "remote (europe)", "remote, europe",
    "uk only", "canada only", "europe only", "germany only",
    "us timezone", "americas timezone", "european timezone",
    "any state", "within the us", "within the united states",
    "must reside in", "must be located in", "us citizenship",
    "authorized to work in the us", "authorized to work in the united states",
    "authorized to work in canada", "authorized to work in the uk",
    "right to work in the uk", "right to work in canada",
    "eligible to work in the us", "public sector", "security clearance",
    "u.s. citizen", "us citizen", "green card",
)

# Foreign country / region / hub names (checked with word boundaries against normalized text)
EXCLUDED_COUNTRY_RESTRICTED: frozenset[str] = frozenset([
    "us", "usa", "united states", "united states of america",
    "canada", "uk", "united kingdom", "great britain", "england", "scotland", "wales",
    "germany", "deutschland", "france", "australia", "netherlands",
    "brazil", "poland", "spain", "singapore", "mexico", "czechia", "czech republic",
    "hungary", "ukraine", "malaysia", "argentina", "europe", "eu", "emea", "latam",
    "amer", "americas", "north america", "south america", "central america",
    "mena", "middle east", "africa", "caribbean", "southeast asia", "east asia",
    "scandinavia", "nordics", "benelux", "dach", "anz", "oceania",
    "ireland", "italy", "portugal", "sweden", "norway", "denmark", "finland",
    "switzerland", "austria", "belgium", "japan", "south korea", "new zealand",
    "philippines", "thailand", "indonesia", "vietnam", "taiwan", "hong kong", "china",
    "bosnia", "bosnia and herzegovina", "croatia", "serbia", "romania", "bulgaria",
    "greece", "turkey", "israel", "uae", "united arab emirates", "saudi arabia", "saudi", "ksa",
    "riyadh", "jeddah", "abu dhabi", "qatar", "doha", "bahrain", "kuwait", "oman",
    "south africa", "nigeria", "egypt", "kenya", "morocco", "tunisia",
    "colombia", "chile", "peru", "costa rica", "panama", "ecuador", "uruguay", "venezuela",
    "guatemala", "puerto rico", "estonia", "latvia", "lithuania", "slovakia", "slovenia",
    "luxembourg", "iceland", "cyprus", "malta", "moldova", "albania", "north macedonia",
    "montenegro", "armenia", "kazakhstan", "pakistan", "bangladesh", "sri lanka", "nepal",
    "san francisco", "new york", "los angeles", "seattle", "chicago", "boston",
    "austin", "denver", "miami", "atlanta", "dallas", "houston", "san diego", "san jose",
    "toronto", "vancouver", "montreal", "ottawa", "waterloo", "calgary", "ontario", "alberta", "quebec",
    "london", "manchester", "edinburgh", "bristol", "cambridge", "berlin", "munich", "frankfurt",
    "amsterdam", "paris", "dublin", "madrid", "barcelona", "lisbon", "warsaw", "krakow", "wroclaw",
    "prague", "budapest", "bucharest", "stockholm", "oslo", "copenhagen", "helsinki",
    "zurich", "vienna", "brussels", "milan", "rome", "tel aviv", "dubai",
    "tokyo", "seoul", "manila", "bangkok", "jakarta", "sydney", "melbourne", "auckland",
    "sao paulo", "buenos aires", "bogota", "mexico city", "european union", "schengen",
])
_EXCLUDED_COUNTRY_PATTERNS: list[re.Pattern] = [
    _compile_token_pattern(kw) for kw in EXCLUDED_COUNTRY_RESTRICTED
]
# Sub-countries/cities within APAC that lock out India when attached to "APAC" (e.g. "APAC; Singapore")
_APAC_SUBCOUNTRY_LOCKS: tuple[re.Pattern, ...] = tuple(
    _compile_token_pattern(kw) for kw in (
        "singapore", "australia", "new zealand", "japan", "tokyo", "south korea", "seoul",
        "taiwan", "hong kong", "china", "philippines", "manila", "malaysia", "indonesia",
        "jakarta", "vietnam", "thailand", "bangkok", "sydney", "melbourne", "auckland",
        "pakistan", "bangladesh", "sri lanka", "nepal", "anz",
    )
)
_EXCLUDE_INDIA_RE = re.compile(r"\b(?:exclud(?:e|es|ing)|except|not)\s+(?:in\s+)?india\b", re.IGNORECASE)
_APAC_TOKEN_RE = re.compile(r"\b(?:apac|asia[\s-]+pacific)\b", re.IGNORECASE)
# Case-sensitive uppercase 2-letter US state suffixes (e.g. "Fort Wayne, IN", "Austin, TX")
_US_STATE_COMMA_RE = re.compile(
    r",\s*(?:AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|DC)\b"
)
# Case-sensitive foreign timezone badges (excludes IST)
_FOREIGN_TZ_RE = re.compile(
    r"(?<!\w)(?:PST|EST|CST|MST|PDT|EDT|CDT|MDT|CET|CEST|EET|BST|AEST|UTC\s*-\s*(?:[1-9]|1[0-2]))(?!\w)"
)

# Regex patterns detecting explicit requirement for >= 3 years experience in JD text
SENIOR_EXP_PATTERNS = [
    re.compile(r"(?:minimum|at least|requires?|have|with)\s*([3-9]|\d{2})\s*\+?\s*(?:years?|yrs?)\s*(?:of)?\s*(?:experience|exp)", re.IGNORECASE),
    re.compile(r"([3-9]|\d{2})\s*\+?\s*(?:years?|yrs?)\s*(?:of)?\s*(?:experience|exp)\s*(?:required|minimum)", re.IGNORECASE),
    re.compile(r"experience\s*(?:required|level)?\s*:\s*([3-9]|\d{2})\s*\+?\s*(?:years?|yrs?)", re.IGNORECASE),
]


def _is_tech_job(title: str) -> bool:
    """Return True if the job title matches tech engineering keywords with word-boundary accuracy and is not an excluded role."""
    if not title:
        return False
    # Check non-tech exclusions first
    for pat in EXCLUDED_NON_TECH_PATTERNS:
        if pat.search(title):
            return False
    # Check word-boundary tech patterns
    return any(p.search(title) for p in _TECH_PATTERNS)


def _is_senior_only(title: str) -> bool:
    """Return True if title indicates an explicitly senior/staff/lead role, unless valid junior signals are present."""
    t = title or ""
    # Executive and architecture roles are always senior, even if 'associate' appears
    if any(p.search(t) for p in _SUPER_SENIOR_PATTERNS):
        return True
    # Level numerals: SDE 2, SWE II, Level 3, etc. are senior
    if SENIOR_LEVEL_PATTERN.search(t):
        return True
    # Explicit entry/fresher signals (word-boundary safe: avoids 'upgrade'->'grad', 'internal'->'intern')
    if any(p.search(t) for p in _JUNIOR_TITLE_PATTERNS):
        return False
    has_senior_signal = any(p.search(t) for p in _SENIOR_ONLY_PATTERNS)
    if JUNIOR_LEVEL_PATTERN.search(t) and not has_senior_signal:
        return False
    # Standard senior signals (e.g. 'Senior Engineer', 'Tech Lead', 'Sr. Dev', 'Senior Engineer I')
    if has_senior_signal:
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
    - Remote WITHOUT restrictive foreign geo-fencing (allows broad Global / Worldwide / APAC
      remote regions unless locked to another specific APAC country like Singapore/Australia).
    """
    raw_loc = (location or "").strip()
    loc = raw_loc.lower()

    if _EXCLUDE_INDIA_RE.search(raw_loc):
        return False

    # Explicit India location is always OK (word-boundary safe; excludes Indiana / Indianapolis / IN)
    if any(p.search(loc) for p in _INDIA_PATTERNS):
        return True

    # If not remote and not in India, cannot work onsite
    if not remote:
        return False

    # Normalize punctuation like "U.S." -> "us", "U.K." -> "uk"
    loc_nodots = loc.replace(".", "")

    # Check explicit foreign restriction phrases, US state suffixes, and foreign timezones
    if any(kw in loc or kw in loc_nodots for kw in EXCLUDED_GEO_KEYWORDS):
        return False

    if _US_STATE_COMMA_RE.search(raw_loc) or _FOREIGN_TZ_RE.search(raw_loc):
        return False

    # Segment-aware evaluation for multi-region remote strings (e.g., "APAC, EMEA, LATAM, Canada" or "Remote - NA, APAC, EMEA")
    has_apac_subcountry_lock = any(p.search(loc) or p.search(loc_nodots) for p in _APAC_SUBCOUNTRY_LOCKS)
    segments = [seg.strip() for seg in re.split(r"[,;/|•]+", raw_loc) if seg.strip()]
    for seg in segments:
        seg_low = seg.lower()
        seg_nodots = seg_low.replace(".", "")
        if _APAC_TOKEN_RE.search(seg_low) and not has_apac_subcountry_lock:
            return True
        if any(kw in seg_low for kw in ("worldwide", "anywhere", "global", "wfa", "all countries")):
            if not any(p.search(seg_low) or p.search(seg_nodots) for p in _EXCLUDED_COUNTRY_PATTERNS):
                return True

    if any(p.search(loc) or p.search(loc_nodots) for p in _EXCLUDED_COUNTRY_PATTERNS):
        return False

    # If remote with explicit global signals or empty location
    if loc in ("", "remote", "worldwide", "anywhere", "global", "work from anywhere", "wfa"):
        return True

    if any(kw in loc for kw in ("worldwide", "anywhere", "global", "wfa", "all countries")):
        return True

    return True


_GEO_KNOCKOUT_TEXT_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(
        r"\b(?:must\s+be\s+(?:legally\s+)?authorized\s+to\s+work\s+in\s+(?:the\s+)?(?:u\.?s\.?a?|united\s+states|uk|united\s+kingdom|canada|eu|australia)|"
        r"legally\s+authorized\s+to\s+work\s+in\s+(?:the\s+)?(?:u\.?s\.?a?|united\s+states|uk|united\s+kingdom|canada)|"
        r"eligible\s+to\s+work\s+in\s+(?:the\s+)?(?:u\.?s\.?a?|united\s+states|uk|united\s+kingdom|canada)|"
        r"right\s+to\s+work\s+in\s+(?:the\s+)?(?:uk|united\s+kingdom|canada|eu)|"
        r"(?:u\.?s\.?\s+citizenship|u\.?s\.?\s+persons?\s+only|citizenship\s+or\s+permanent\s+residen|itar|ear\s+controlled|security\s+clearance\s+required))\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:must\s+(?:be\s+)?(?:reside|located|based)\s+in\s+(?:the\s+)?(?:u\.?s\.?a?|united\s+states|uk|united\s+kingdom|canada|eu|europe|north\s+america)|"
        r"only\s+(?:accepting|considering)\s+candidates\s+(?:who\s+reside\s+|located\s+|based\s+)?in\s+(?:the\s+)?(?:u\.?s\.?a?|united\s+states|uk|canada|europe))\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:remote\s+(?:within|across)\s+the\s+(?:u\.?s\.?a?|united\s+states|uk|united\s+kingdom|canada|eu)|"
        r"public\s+sector\s+clearance|active\s+(?:top\s+)?secret\s+clearance)\b",
        re.IGNORECASE,
    ),
)

_GENERIC_CONTACT_PREFIXES = ("no-reply", "noreply", "donotreply", "do-not-reply", "privacy", "legal", "support", "abuse", "dmca")
_GENERIC_CONTACT_DOMAINS = ("example.com", "greenhouse.io", "lever.co", "ashbyhq.com", "sentry.io", "w3.org")
_STATIC_ASSET_TLDS = frozenset({
    "png", "jpg", "jpeg", "gif", "svg", "webp", "ico", "bmp", "avif",
    "woff", "woff2", "ttf", "eot", "otf", "css", "js", "mjs", "map", "json", "pdf", "mp4",
})


def has_geo_knockout_in_text(title: str, description: str = "") -> bool:
    """Return True when title/JD explicitly imposes a US/UK/CA/EU authorization, residence, or clearance wall."""
    combined = f"{title or ''}\n{description or ''}"
    low = combined.lower()
    if any(kw in low for kw in ("public sector", "security clearance", "u.s. citizen only", "us citizen only", "active top secret", "itar", "ear controlled")):
        return True

    title_is_india = any(p.search((title or "").lower()) for p in _INDIA_PATTERNS)
    # Split into sentence/line clauses (keeping "U.S."/"U.K." abbreviations intact)
    clauses: list[str] = []
    pos = 0
    for sep in re.finditer(r"(?<!\b[Uu]\.[Ss])(?<!\b[Uu]\.[Kk])(?<!\b[Uu])(?:[.!?]\s+|\n+)", combined):
        clauses.append(combined[pos:sep.start()])
        pos = sep.end()
    clauses.append(combined[pos:])

    for clause in clauses:
        if not clause.strip():
            continue
        if any(pat.search(clause) for pat in _GEO_KNOCKOUT_TEXT_PATTERNS):
            if title_is_india:
                continue
            if any(ip.search(clause.lower()) for ip in _INDIA_PATTERNS):
                continue
            return True
    return False


def _extract_contact_emails(text: str) -> list[str]:
    """Extract clean, non-automated public hiring/founder email addresses from JD/comment text."""
    if not text:
        return []
    found = re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text)
    out: list[str] = []
    seen: set[str] = set()
    for em in found:
        em_clean = em.strip(".,;:)>\"'").lower()
        local, _, dom = em_clean.partition("@")
        if not local or not dom:
            continue
        tld = dom.rsplit(".", 1)[-1].lower()
        if tld in _STATIC_ASSET_TLDS:
            continue
        if any(local.startswith(pfx) for pfx in _GENERIC_CONTACT_PREFIXES):
            continue
        if any(dom == gd or dom.endswith("." + gd) for gd in _GENERIC_CONTACT_DOMAINS):
            continue
        if em_clean not in seen:
            seen.add(em_clean)
            out.append(em_clean)
    return out


def _parse_posted_at(raw_val: Any) -> str:
    """Normalize ISO8601, RFC2822, or UNIX epoch (s/ms) timestamps to UTC ISO8601; returns '' on failure."""
    if raw_val is None or raw_val == "":
        return ""
    try:
        if isinstance(raw_val, (int, float)):
            ts = float(raw_val)
            if ts <= 0:
                return ""
            if ts > 1e12:
                ts /= 1000.0
            return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
        s = str(raw_val).strip()
        if not s:
            return ""
        if re.fullmatch(r"\d+(?:\.\d+)?", s):
            ts = float(s)
            if ts > 1e12:
                ts /= 1000.0
            return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
        # Handle ISO strings including trailing Z or bare dates YYYY-MM-DD
        iso_candidate = s.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(iso_candidate)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).isoformat()
        except ValueError:
            pass
        # Handle RFC 2822 date strings (e.g. RSS/Jobicy pubDate)
        dt_rfc = email.utils.parsedate_to_datetime(s)
        if dt_rfc is not None:
            if dt_rfc.tzinfo is None:
                dt_rfc = dt_rfc.replace(tzinfo=timezone.utc)
            return dt_rfc.astimezone(timezone.utc).isoformat()
    except Exception:
        return ""
    return ""


def _get_job_age_days(posted_at: str) -> float | None:
    """Return age in days for an ISO8601 posted_at timestamp, or None if unavailable."""
    if not posted_at:
        return None
    try:
        dt = datetime.fromisoformat(posted_at.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds() / 86400.0
        return max(0.0, age)
    except Exception:
        return None


def _is_too_old(posted_at: str, max_age_days: int | None = None) -> bool:
    """Return True if posted_at is known and older than max_age_days."""
    if max_age_days is None:
        try:
            max_age_days = int(os.environ.get("MAX_JOB_AGE_DAYS", str(DEFAULT_MAX_JOB_AGE_DAYS)))
        except ValueError:
            max_age_days = DEFAULT_MAX_JOB_AGE_DAYS
    if max_age_days <= 0:
        return False
    age = _get_job_age_days(posted_at)
    return age is not None and age > max_age_days


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _strip_html(text: str) -> str:
    """Remove HTML tags and collapse whitespace."""
    text = html.unescape(text or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _extract_clean_lines(raw_html_text: str) -> list[str]:
    """Preserve HTML paragraph/line breaks and return clean non-empty text lines."""
    with_breaks = re.sub(
        r"(?i)<(?:br\s*/?|/?p(?:\s[^>]*)?|/?div(?:\s[^>]*)?|/?li(?:\s[^>]*)?)>",
        "\n",
        html.unescape(raw_html_text or ""),
    )
    no_tags = re.sub(r"<[^>]+>", " ", with_breaks)
    lines = []
    for line in no_tags.splitlines():
        clean = re.sub(r"\s+", " ", line).strip()
        if clean:
            lines.append(clean)
    return lines


def _extract_first_line(raw_html_text: str) -> str:
    """Return the first non-empty header line from raw HTML comment text."""
    lines = _extract_clean_lines(raw_html_text)
    return lines[0] if lines else ""


@retry(
    wait=wait_exponential(multiplier=1, min=2, max=10),
    stop=stop_after_attempt(3),
    retry=retry_if_exception_type(requests.RequestException),
    reraise=True,
)
def _get_with_retry(
    url: str,
    *,
    params: dict | None = None,
    headers: dict | None = None,
    timeout: int = 20,
    raise_on_http_error: bool = False,
) -> requests.Response:
    """Perform HTTP GET with working tenacity retry on transient RequestExceptions and 429/5xx."""
    resp = requests.get(url, params=params, headers=headers, timeout=timeout)
    if raise_on_http_error or resp.status_code in (429, 500, 502, 503, 504):
        resp.raise_for_status()
    return resp


def _report_source_failure(source: str, exc: Exception) -> None:
    try:
        from scripts.log_and_notify import log_failure
        log_failure(source, exc)
    except Exception:
        logger.error("fetch:%s — error: %s", source, exc)


def _is_remote(location: str, title: str = "") -> bool:
    """Heuristic: treat 'remote', blank, 'worldwide', 'anywhere', 'apac', or remote titles as remote=True."""
    loc = (location or "").lower().strip()
    if (
        loc in ("", "remote", "worldwide", "anywhere", "global", "distributed", "wfa", "work from anywhere")
        or any(tok in loc for tok in ("remote", "worldwide", "anywhere", "work from anywhere", "distributed"))
        or bool(_APAC_TOKEN_RE.search(loc))
    ):
        return True
    if title and re.search(r"\bremote\b", title, re.IGNORECASE):
        return True
    return False


def _is_direct_ats_url(apply_url: str) -> bool:
    """Return True for direct hosted ATS application URLs (no Cloudflare aggregator hop)."""
    u = (apply_url or "").lower()
    return any(tok in u for tok in ("greenhouse.io", "lever.co", "ashbyhq.com", "gh_jid="))


_SPAM_MARKETPLACE_COMPANIES = frozenset({
    "micro1", "telus digital", "telus international", "imerit", "imerit technology",
    "outlier", "outlier ai", "dataannotation", "alignerr", "remotasks", "scale ai", "g2i", "g2i inc.",
})
_SPAM_TITLE_PATTERNS = re.compile(
    r"\b(?:talent\s+pool|ai\s+(?:trainer|community|tutor|content\s+reviewer)|annotation|video\s+annotation)\b",
    re.IGNORECASE,
)


def _is_excluded_spam_listing(company: str, title: str) -> bool:
    """Reject known AI-training marketplaces & talent-pool aggregators."""
    c_low = (company or "").strip().lower()
    if c_low in _SPAM_MARKETPLACE_COMPANIES or any(c_low.startswith(s + " ") for s in ("micro1", "telus", "outlier", "imerit", "g2i")):
        return True
    return bool(_SPAM_TITLE_PATTERNS.search(title or ""))


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
    posted_at: str = "",
    contact_emails: list[str] | None = None,
) -> dict[str, Any]:
    """Build a validated JD dict matching the canonical schema."""
    jd_obj: dict[str, Any] = {
        "title": title.strip(),
        "company": company.strip(),
        "location": location.strip(),
        "remote": remote,
        "description": _strip_html(description),
        "apply_url": apply_url.strip(),
        "source": source,
        "posted_at": posted_at,
        "fetched_at": _now_iso(),
    }
    if contact_emails:
        jd_obj["contact_emails"] = contact_emails
    return jd_obj


def _recency_tier(jd: dict[str, Any]) -> tuple[int, int]:
    """Return (freshness_bucket, is_not_india) for coarse tier sorting while preserving round-robin diversity."""
    age = _get_job_age_days(jd.get("posted_at", ""))
    if age is None:
        bucket = 3
    elif age <= 2.0:
        bucket = 0
    elif age <= 7.0:
        bucket = 1
    else:
        bucket = 2
    loc_low = (jd.get("location") or "").lower()
    is_india = any(p.search(loc_low) for p in _INDIA_PATTERNS)
    return (bucket, 0 if is_india else 1)


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
            posted_at = _parse_posted_at(job.get("first_published") or job.get("updated_at"))
            if _is_too_old(posted_at):
                continue
            loc_data = job.get("location", {})
            location = loc_data.get("name", "") if isinstance(loc_data, dict) else str(loc_data)
            remote = _is_remote(location, title)
            if not is_location_ok(location, remote):
                continue
            apply_url = job.get("absolute_url", "")
            if not apply_url:
                continue
            # Normalize custom corporate Greenhouse URLs (e.g. rubrik.com, stripe.com, okta.com)
            # to direct Greenhouse embed URLs so Playwright bypasses corporate WAFs & wrappers.
            gh_host = (urllib.parse.urlparse(apply_url).netloc or "").lower()
            job_id = job.get("id")
            if job_id and not any(
                d in gh_host for d in ("boards.greenhouse.io", "job-boards.greenhouse.io", "job-boards.eu.greenhouse.io")
            ):
                apply_url = f"https://job-boards.greenhouse.io/embed/job_app?for={slug}&token={job_id}"
            description = _strip_html(job.get("content", ""))
            if _requires_senior_experience(description) or has_geo_knockout_in_text(title, description):
                continue
            results.append(_jd(
                title=title,
                company=company_name,
                location=location,
                remote=remote,
                description=description,
                apply_url=apply_url,
                source="greenhouse",
                posted_at=posted_at,
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
            posted_at = _parse_posted_at(job.get("publishedAt") or job.get("updatedAt"))
            if _is_too_old(posted_at):
                continue
            location = job.get("location", "") or ""
            is_remote_flag = bool(job.get("isRemote", False))
            workplace_type = (job.get("workplaceType") or "").lower()
            remote = is_remote_flag or "remote" in workplace_type or _is_remote(location, title)
            if not is_location_ok(location, remote):
                sec_locs = [
                    s.get("location", "")
                    for s in (job.get("secondaryLocations") or [])
                    if isinstance(s, dict) and s.get("location")
                ]
                matched_sec = next(
                    (
                        sl for sl in sec_locs
                        if (_is_remote(sl) or any(p.search(sl.lower()) for p in _INDIA_PATTERNS))
                        and is_location_ok(sl, _is_remote(sl) or is_remote_flag or "remote" in workplace_type)
                    ),
                    "",
                )
                if not matched_sec:
                    continue
                location = matched_sec
                remote = _is_remote(location) or is_remote_flag or "remote" in workplace_type
            apply_url = job.get("applyUrl", "") or job.get("jobUrl", "")
            if not apply_url:
                continue
            description = _strip_html(
                job.get("descriptionHtml", "") or job.get("descriptionPlain", "")
            )
            if _requires_senior_experience(description) or has_geo_knockout_in_text(title, description):
                continue
            results.append(_jd(
                title=title,
                company=company_name,
                location=location,
                remote=remote,
                description=description,
                apply_url=apply_url,
                source="ashby",
                posted_at=posted_at,
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
            posted_at = _parse_posted_at(job.get("createdAt"))
            if _is_too_old(posted_at):
                continue
            categories = job.get("categories", {}) or {}
            location = categories.get("location", "") or categories.get("commitment", "")
            workplace_type = (job.get("workplaceType") or "").lower()
            remote = (workplace_type == "remote") or _is_remote(location, title)
            if not is_location_ok(location, remote):
                all_locs = [str(x).strip() for x in (categories.get("allLocations") or []) if x]
                matched_alt = next(
                    (
                        al for al in all_locs
                        if (_is_remote(al) or any(p.search(al.lower()) for p in _INDIA_PATTERNS))
                        and is_location_ok(al, (workplace_type == "remote") or _is_remote(al))
                    ),
                    "",
                )
                if not matched_alt:
                    continue
                location = matched_alt
                remote = (workplace_type == "remote") or _is_remote(location, title)
            apply_url = job.get("applyUrl", "") or job.get("hostedUrl", "")
            if not apply_url:
                continue
            description = _strip_html(
                job.get("descriptionBody", "") or job.get("description", "")
            )
            if _requires_senior_experience(description) or has_geo_knockout_in_text(title, description):
                continue
            results.append(_jd(
                title=title,
                company=company_name,
                location=location,
                remote=remote,
                description=description,
                apply_url=apply_url,
                source="lever",
                posted_at=posted_at,
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


# ── Aggregators (RemoteOK, Remotive, Jobicy, Arbeitnow, HackerNews) ───────────

def fetch_remoteok() -> list[dict]:
    listings: list[dict] = []
    try:
        resp = _get_with_retry(
            REMOTEOK_URL,
            params={"limit": REMOTEOK_LIMIT},
            headers=HEADERS,
            timeout=20,
            raise_on_http_error=True,
        )
        raw: list[dict] = resp.json()

        for item in raw:
            if not isinstance(item, dict) or "slug" not in item:
                continue
            title = item.get("position", "").strip()
            company = item.get("company", "").strip()
            if not title or not company or not _is_tech_job(title) or _is_senior_only(title) or _is_excluded_spam_listing(company, title):
                continue
            posted_at = _parse_posted_at(item.get("date") or item.get("epoch"))
            if _is_too_old(posted_at):
                continue
            description = item.get("description", "")
            if _requires_senior_experience(description) or has_geo_knockout_in_text(title, description):
                continue
            location = item.get("location", "") or ""
            if not is_location_ok(location, True):
                continue
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
                posted_at=posted_at,
            ))
    except Exception as e:
        _report_source_failure("remoteok", e)
        return []
    logger.info("fetch_remoteok — %d listings", len(listings))
    return listings


def fetch_remotive() -> list[dict]:
    listings: list[dict] = []
    try:
        resp = _get_with_retry(
            REMOTIVE_URL,
            params={"limit": REMOTIVE_LIMIT, "category": "software-dev"},
            headers=HEADERS,
            timeout=20,
            raise_on_http_error=True,
        )
        payload: dict = resp.json()
        raw: list[dict] = payload.get("jobs", [])

        for item in raw:
            title = item.get("title", "").strip()
            company = item.get("company_name", "").strip()
            if not title or not company or not _is_tech_job(title) or _is_senior_only(title) or _is_excluded_spam_listing(company, title):
                continue
            posted_at = _parse_posted_at(item.get("publication_date"))
            if _is_too_old(posted_at):
                continue
            description = item.get("description", "")
            if _requires_senior_experience(description) or has_geo_knockout_in_text(title, description):
                continue
            location = item.get("candidate_required_location", "") or ""
            remote = _is_remote(location) or not location.strip()
            if not is_location_ok(location, remote):
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
                posted_at=posted_at,
            ))
    except Exception as e:
        _report_source_failure("remotive", e)
        return []
    logger.info("fetch_remotive — %d listings", len(listings))
    return listings


def fetch_jobicy() -> list[dict]:
    """Fetch 100% remote job listings from Jobicy API."""
    listings: list[dict] = []
    try:
        resp = _get_with_retry(
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
            if not title or not company or not _is_tech_job(title) or _is_senior_only(title) or _is_excluded_spam_listing(company, title):
                continue
            posted_at = _parse_posted_at(item.get("pubDate"))
            if _is_too_old(posted_at):
                continue
            location = item.get("jobGeo", "") or ""
            remote = True
            if not is_location_ok(location, remote):
                continue
            description = _strip_html(item.get("jobDescription", ""))
            if _requires_senior_experience(description) or has_geo_knockout_in_text(title, description):
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
                posted_at=posted_at,
            ))
    except Exception as e:
        _report_source_failure("jobicy", e)
        return []

    logger.info("fetch_jobicy — %d listings", len(listings))
    return listings


_EU_REMOTE_PHRASES = (
    "werken op afstand", "telearbeit", "télétravail", "teletravail",
    "deutschlandweit", "homeoffice", "nur deutschland", "only in germany",
    "only in france", "only in netherlands", "only in switzerland",
)


def fetch_arbeitnow() -> list[dict]:
    """Fetch tech job listings from Arbeitnow API."""
    listings: list[dict] = []
    try:
        resp = _get_with_retry(ARBEITNOW_URL, headers=HEADERS, timeout=20)
        if resp.status_code != 200:
            logger.warning("fetch_arbeitnow — HTTP %s", resp.status_code)
            return []
        data = resp.json()
        jobs = data.get("data", [])
        for item in jobs:
            title = (item.get("title") or "").strip()
            company = (item.get("company_name") or "").strip()
            if not title or not company or not _is_tech_job(title) or _is_senior_only(title) or _is_excluded_spam_listing(company, title):
                continue
            posted_at = _parse_posted_at(item.get("created_at"))
            if _is_too_old(posted_at):
                continue
            apply_url = (item.get("url") or "").strip()
            if not apply_url:
                continue
            location = (item.get("location") or "").strip()
            remote = bool(item.get("remote", False)) or _is_remote(location)
            host = (urllib.parse.urlparse(apply_url).netloc or "").lower()
            loc_india = any(p.search(location.lower()) for p in _INDIA_PATTERNS)
            # Reject European regional ccTLD listings (.fr, .nl, .ch, .de, .co.uk, etc.) unless India
            if host and not (host == "arbeitnow.com" or host.endswith(".arbeitnow.com")) and not loc_india:
                continue
            # Also evaluate location + title together so empty location doesn't mask EU/foreign titles
            if not is_location_ok(location, remote):
                continue
            if not loc_india and not is_location_ok(title, True):
                continue
            description = _strip_html(item.get("description", ""))
            desc_low = f"{location} {title} {description[:600]}".lower()
            if any(phrase in desc_low for phrase in _EU_REMOTE_PHRASES) and not loc_india:
                continue
            if _requires_senior_experience(description) or has_geo_knockout_in_text(title, description):
                continue
            listings.append(_jd(
                title=title,
                company=company,
                location=location,
                remote=remote,
                description=description,
                apply_url=apply_url,
                source="arbeitnow",
                posted_at=posted_at,
            ))
    except Exception as e:
        _report_source_failure("arbeitnow", e)
        return []

    logger.info("fetch_arbeitnow — %d listings", len(listings))
    return listings


def fetch_hackernews() -> list[dict]:
    listings: list[dict] = []
    try:
        url = "https://hn.algolia.com/api/v1/search_by_date"
        params = {"tags": "story,author_whoishiring", "query": "Ask HN: Who is hiring?", "hitsPerPage": 1}
        resp = _get_with_retry(url, params=params, timeout=20, raise_on_http_error=True)
        hits = resp.json().get("hits", [])
        if not hits:
            logger.warning("hackernews: no 'Who is hiring' thread found")
            return []
        story_id = hits[0]["objectID"]

        c_url = "https://hn.algolia.com/api/v1/search"
        c_params = {"tags": f"comment,story_{story_id}", "hitsPerPage": 500}
        c_resp = _get_with_retry(c_url, params=c_params, timeout=25, raise_on_http_error=True)
        all_comments = c_resp.json().get("hits", [])

        ats_pattern = re.compile(r"greenhouse\.io|lever\.co|ashbyhq\.com|gh_jid=", re.IGNORECASE)
        url_regex = re.compile(r"https?://\S+")

        from scripts.auto_apply_ats import validate_ats_job_url

        for comment in all_comments:
            raw_text = comment.get("comment_text", "")
            text = html.unescape(raw_text)
            if not ats_pattern.search(text):
                continue

            posted_at = _parse_posted_at(comment.get("created_at") or comment.get("created_at_i"))
            lines = _extract_clean_lines(raw_text)
            if not lines:
                continue
            first_line = lines[0]

            stripped = _strip_html(raw_text)
            if _requires_senior_experience(stripped):
                continue

            parts = [p.strip() for p in first_line.split("|")]
            if len(parts) == 1 and len(first_line) > 100:
                # Discussion comment mentioning an ATS link without standard HN header format
                continue

            company = re.sub(r"\s*\(https?://[^)]+\)", "", parts[0]).strip() if parts else "HN Startup"
            if not company or len(company) > 80:
                company = company[:77].rstrip() + "..." if len(company) > 80 else "HN Startup"

            top_header = " ".join(lines[:2]) if "|" in first_line else first_line
            is_remote_hn = "remote" in top_header.lower()
            first_line_no_urls = re.sub(r"https?://\S+", "", first_line).strip()
            # Validate full header line (catches `Company | Remote (US) | Full-time` ordering)
            if not is_location_ok(first_line_no_urls, is_remote_hn):
                continue

            location_part = ""
            for p in parts[1:]:
                if any(k in p.lower() for k in ("remote", "onsite", "on-site", "hybrid", "india", "worldwide", "anywhere")):
                    location_part = p
                    break
            location = location_part or (parts[2] if len(parts) >= 3 else ("Remote" if is_remote_hn else ""))

            header_role = ""
            for p in parts[1:]:
                if "http" in p.lower():
                    continue
                if len(p) <= 60 and _is_tech_job(p):
                    header_role = p
                    break
            base_title = header_role or "Software Engineer"

            if _is_senior_only(base_title) or has_geo_knockout_in_text(base_title, stripped):
                continue

            contact_emails = _extract_contact_emails(stripped)

            for raw_url in url_regex.findall(text):
                clean_url = raw_url.strip(').,;:>"\'\\')
                clean_lower = clean_url.lower()
                if any(domain in clean_lower for domain in ("greenhouse.io", "lever.co", "ashbyhq.com", "gh_jid=")):
                    valid, _ = validate_ats_job_url(clean_url)
                    if not valid:
                        continue

                    # Check line containing this specific role link for per-role seniority/location overrides
                    role_line = next((ln for ln in lines if clean_url in ln or clean_url[:35] in ln), "")
                    role_line_no_url = re.sub(r"https?://\S+", "", role_line).lstrip(">•*-: ").strip()
                    if role_line_no_url and role_line != first_line:
                        role_remote = is_remote_hn or ("remote" in role_line_no_url.lower())
                        if not is_location_ok(role_line_no_url, role_remote):
                            continue
                        role_parts = [rp.strip(" :-") for rp in role_line_no_url.split("|") if rp.strip(" :-")]
                        role_title = next((rp for rp in role_parts if len(rp) <= 60 and _is_tech_job(rp)), "")
                        final_title = role_title or base_title
                        if _is_senior_only(final_title):
                            continue
                    else:
                        final_title = base_title

                    listings.append(_jd(
                        title=final_title,
                        company=company,
                        location=location,
                        remote=is_remote_hn,
                        description=stripped,
                        apply_url=clean_url,
                        source="hackernews",
                        posted_at=posted_at,
                        contact_emails=contact_emails,
                    ))
                    break
    except Exception as e:
        _report_source_failure("hackernews", e)
        return []

    logger.info("fetch_hackernews — %d listings", len(listings))
    return listings


# ── Himalayas Remote Fetcher ──────────────────────────────────────────────────

def fetch_himalayas(limit: int = 40) -> list[dict]:
    """
    Fetch remote tech jobs from Himalayas API + targeted India/Worldwide search endpoints.
    Filters for tech relevance, early-career experience caps, and India/global/APAC eligibility.
    """
    listings: list[dict] = []
    if limit <= 20:
        queries = [(HIMALAYAS_URL, {"offset": 0})]
    else:
        search_url = f"{HIMALAYAS_URL}/search"
        queries = [
            (HIMALAYAS_URL, {"offset": 0}),
            (HIMALAYAS_URL, {"offset": 20}),
            (search_url, {"q": "developer", "country": "IN"}),
            (search_url, {"q": "full stack", "worldwide": "true"}),
            (search_url, {"q": "backend", "worldwide": "true"}),
        ]

    for endpoint_url, params in queries:
        try:
            resp = _get_with_retry(endpoint_url, params=params, headers=HEADERS, timeout=20)
            if resp.status_code != 200:
                logger.warning("fetch_himalayas — HTTP %s at %s", resp.status_code, params)
                continue
            data = resp.json()
            jobs = data.get("jobs", [])
            for item in jobs:
                title = (item.get("title") or "").strip()
                company = (item.get("companyName") or "").strip()
                if not title or not company or not _is_tech_job(title) or _is_senior_only(title):
                    continue
                posted_at = _parse_posted_at(item.get("pubDate") or item.get("createdAt"))
                if _is_too_old(posted_at):
                    continue
                loc_restrictions = item.get("locationRestrictions") or []
                location = ", ".join(loc_restrictions) if loc_restrictions else "Worldwide"
                remote = True
                if loc_restrictions:
                    loc_low = location.lower()
                    loc_nodots = loc_low.replace(".", "")
                    has_apac_sub_lock = any(p.search(loc_low) or p.search(loc_nodots) for p in _APAC_SUBCOUNTRY_LOCKS)
                    allows_india = any(p.search(loc_low) for p in _INDIA_PATTERNS)
                    allows_global = (
                        any(w in loc_low for w in ("worldwide", "anywhere", "global", "all countries"))
                        or (bool(_APAC_TOKEN_RE.search(loc_low)) and not has_apac_sub_lock)
                    )
                    if not (allows_india or allows_global):
                        continue
                if not is_location_ok(location, remote):
                    continue
                description = _strip_html(item.get("description", "") or item.get("excerpt", ""))
                if _requires_senior_experience(description) or has_geo_knockout_in_text(title, description):
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
                    posted_at=posted_at,
                ))
        except Exception as e:
            _report_source_failure("himalayas", e)
    listings = _dedup(listings)
    logger.info("fetch_himalayas — %d listings", len(listings))
    return listings


# ── SimplifyJobs Early-Career Feed ────────────────────────────────────────────

def fetch_simplify_jobs() -> list[dict]:
    """
    Fetch active new-grad / entry-level positions from SimplifyJobs public feed.
    Pre-validates that apply URLs point to legitimate ATS postings (Ashby, Greenhouse, Lever).
    """
    from scripts.auto_apply_ats import validate_ats_job_url, detect_ats, UnsupportedATSError
    listings: list[dict] = []
    try:
        resp = _get_with_retry(SIMPLIFY_URL, headers=HEADERS, timeout=25)
        if resp.status_code != 200:
            logger.warning("fetch_simplify_jobs — HTTP %s", resp.status_code)
            return []
        items = resp.json()
        for item in items:
            if not item.get("active") or not item.get("is_visible", True):
                continue
            title = (item.get("title") or "").strip()
            company = (item.get("company_name") or "").strip()
            if not title or not company or not _is_tech_job(title) or _is_senior_only(title) or _is_excluded_spam_listing(company, title):
                continue
            posted_at = _parse_posted_at(item.get("date_updated") or item.get("date_posted"))
            if _is_too_old(posted_at):
                continue
            locations = item.get("locations") or []
            loc_str = ", ".join(locations) if locations else "Remote"
            remote = _is_remote(loc_str) or any("remote" in l.lower() for l in locations)
            if not is_location_ok(loc_str, remote):
                continue
            apply_url = (item.get("url") or "").strip()
            try:
                ats_kind = detect_ats(apply_url)
                if ats_kind not in ("greenhouse", "lever", "ashby"):
                    continue
            except UnsupportedATSError:
                continue
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
                posted_at=posted_at,
            ))
    except Exception as e:
        _report_source_failure("simplify", e)
        return []
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
    Fetch job listings from all sources, filter against processed URLs, stale listings,
    and geo/work-auth knockouts, and return a freshness- & source-balanced queue.

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

    # Filter out stale postings (> MAX_JOB_AGE_DAYS; monthly Ask HN threads up to 31d) and explicit geo knockouts
    before_fresh = len(deduped)
    hn_max_age = int(os.environ.get("MAX_HN_JOB_AGE_DAYS", "31"))
    deduped = [
        jd for jd in deduped
        if not _is_too_old(
            jd.get("posted_at", ""),
            max_age_days=hn_max_age if jd.get("source") == "hackernews" else None,
        )
        and not has_geo_knockout_in_text(jd.get("title", ""), jd.get("description", ""))
    ]
    if before_fresh != len(deduped):
        logger.info("fetch_all — filtered out %d stale or geo-knockout listings", before_fresh - len(deduped))

    # Optional strict remote-only filter (enabled via REMOTE_ONLY=true or --remote-only)
    if os.environ.get("REMOTE_ONLY", "false").lower() in ("true", "1", "yes"):
        before_remote = len(deduped)
        deduped = [jd for jd in deduped if jd.get("remote")]
        if before_remote != len(deduped):
            logger.info("fetch_all — REMOTE_ONLY enabled: filtered out %d non-remote listings", before_remote - len(deduped))

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

    # Sort each source group: remote jobs first, then newest / India priority
    for src_list in grouped.values():
        src_list.sort(key=lambda j: (0 if j.get("remote") else 1, _recency_tier(j)))

    # Round-Robin Interleave across sources (Direct ATS first)
    source_order = [
        "greenhouse", "lever", "ashby", "yc_startups", "jobicy", "arbeitnow", "remoteok", "remotive",
        "hackernews", "himalayas", "simplify",
    ]
    interleaved: list[dict] = []
    max_count = max((len(lst) for lst in grouped.values()), default=0)

    for i in range(max_count):
        for src in source_order:
            if i < len(grouped[src]):
                interleaved.append(grouped[src][i])

    # Direct ATS Priority + Remote-First + Recency & India-First Ordering
    remote_jobs = [j for j in interleaved if j.get("remote")]
    onsite_jobs = [j for j in interleaved if not j.get("remote")]
    remote_jobs.sort(key=lambda j: (0 if _is_direct_ats_url(j.get("apply_url", "")) else 1, _recency_tier(j)))
    onsite_jobs.sort(key=lambda j: (0 if _is_direct_ats_url(j.get("apply_url", "")) else 1, _recency_tier(j)))
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
