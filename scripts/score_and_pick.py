"""
score_and_pick.py — JD Scoring & Variant Selection
====================================================
Scores a JD against all four resume variants via Groq (Llama 3.3 70B) and
picks the best match. Hard gates at score >= 65 before the pipeline advances.

Input:  a single JD dict (from fetch_jds.py)
Output: a Score result dict:
  {
    "jd_id":        str,         # "{company}_{title}" slug
    "best_variant": "frontend | fullstack | backend-node | ai-engineer",
    "score":        int (0–100),
    "missing_skills": [str, ...],
    "reasoning":    str
  }

If score < 65 the function returns the dict with status="skipped" — the caller
(pipeline orchestrator) must NOT advance to tailoring in that case.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time  # Fix #11: top-level import, not inside function
from pathlib import Path
from typing import Any
from groq import Groq, GroqError
from tenacity import retry, wait_exponential, stop_after_attempt, retry_if_exception_type

from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

RESUMES_DIR = Path(__file__).parent.parent / "resumes"
SCORE_THRESHOLD = 65


# ── Resume loading ────────────────────────────────────────────────────────────

def load_all_variants() -> dict[str, dict]:
    """Load all resume variant JSON files from resumes/. Returns {variant: dict}."""
    variants = {}
    for file in RESUMES_DIR.glob("*.json"):
        try:
            data = json.loads(file.read_text(encoding="utf-8"))
            variant_name = data.get("variant", file.stem)
            variants[variant_name] = data
        except Exception as e:
            logger.error("Failed to load %s: %s", file, e)
    return variants


# ── Groq scoring ──────────────────────────────────────────────────────────────

# Fix #2: these are real, publicly available Groq model IDs (as of 2026)
# Fix #12: client is NOT instantiated at module level — lazy-init inside function
class DailyRateLimitExceeded(GroqError):
    """Raised when Groq daily quota (TPD or RPD) is exhausted across all models."""
    pass


def _is_daily_limit_error(err: Exception) -> bool:
    """Return True if the error indicates daily token or request limits are exhausted."""
    msg = str(err).lower()
    return "rate_limit_exceeded" in msg and any(k in msg for k in [
        "tokens per day", "tpd", "requests per day", "rpd",
        "limit 250", "limit 100000", "limit 200000"
    ])


def _is_non_retryable_groq_error(err: Exception) -> bool:
    """Return True for permanent client/model errors (404 model_not_found, 401/403 auth, 400 bad request)."""
    status = getattr(err, "status_code", None)
    if status in (400, 401, 403, 404):
        return True
    msg = str(err).lower()
    return any(tok in msg for tok in (
        "model_not_found",
        "not_found_error",
        "does not exist",
        "decommissioned",
        "error code: 404",
        "invalid_api_key",
    ))


def _should_retry_groq(retry_state) -> bool:
    """Tenacity retry predicate: only retry transient Groq errors, NEVER 404/model_not_found or daily quota limits."""
    exc = retry_state.outcome.exception()
    if not exc:
        return False
    if isinstance(exc, DailyRateLimitExceeded) or _is_daily_limit_error(exc) or _is_non_retryable_groq_error(exc):
        return False
    return isinstance(exc, GroqError)


def _clean_llm_json(raw: str | None) -> str:
    """Strip reasoning <think>...</think> blocks and markdown fences before json.loads."""
    text = (raw or "").strip()
    if not text:
        return "{}"
    text = re.sub(r"<think>[\s\S]*?</think>", "", text, flags=re.IGNORECASE).strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    return text.strip() or "{}"


# ── Zero-LLM Keyword Scorer ───────────────────────────────────────────────────

def keyword_score_jd(jd: dict, variants: dict[str, dict] | None = None) -> dict[str, Any]:
    """
    Fast, zero-LLM heuristic scorer that evaluates a JD against candidate resume variants.
    Used for the volume path or when Groq API daily rate limits are hit.
    """
    variants = variants or load_all_variants()
    title = (jd.get("title") or "").lower()
    desc = (jd.get("description") or "")[:2000].lower()
    combined_text = f"{title} {desc}"

    # Variant keyword profiles matching Kushagra's background
    variant_keywords = {
        "ai-engineer": ["ai", "llm", "genai", "generative ai", "rag", "langchain", "prompt", "mlops", "machine learning", "nlp", "vector", "embedding"],
        "sdet-qa": ["qa", "sdet", "playwright", "pytest", "automation", "selenium", "tester", "quality assurance", "test automation", "e2e"],
        "solutions-support": ["solutions engineer", "solution engineer", "support engineer", "technical support", "integration", "customer engineer", "client support", "troubleshooting"],
        "frontend": ["frontend", "front-end", "react", "next.js", "nextjs", "vue", "angular", "typescript", "javascript", "tailwind", "ui engineer", "css"],
        "backend-python": ["python", "django", "fastapi", "flask", "sqlalchemy", "celery", "postgres", "sql", "backend", "back-end"],
        "backend-node": ["node", "nodejs", "typescript", "express", "nestjs", "javascript", "backend", "back-end", "rest api", "postgresql", "mongodb"],
    }

    # Count keyword occurrences for each variant using isolated word boundaries
    scores_per_variant = {}
    for vname, kws in variant_keywords.items():
        title_matches = 0
        desc_matches = 0
        for kw in kws:
            pat = re.compile(r"(?<!\w)" + re.escape(kw) + r"(?!\w)", re.IGNORECASE)
            if pat.search(title):
                title_matches += 1
            if pat.search(desc):
                desc_matches += 1
        scores_per_variant[vname] = (title_matches * 5) + desc_matches

    # Pick the variant with the highest keyword affinity
    available_variants = list(variants.keys()) if variants else list(variant_keywords.keys())
    ranked = sorted(
        [(v, scores_per_variant.get(v, 0)) for v in available_variants if v in variant_keywords],
        key=lambda x: x[1],
        reverse=True
    )
    best_variant = ranked[0][0] if ranked and ranked[0][1] > 0 else (available_variants[0] if available_variants else "backend-node")
    top_score_points = ranked[0][1] if ranked else 0

    # Calculate overall match score (0-100)
    score = 45  # Baseline for matching tech filter
    if top_score_points > 0:
        score += min(35, top_score_points * 4)

    # Junior/fresher bonus
    if any(k in combined_text for k in ["junior", "jr", "entry", "associate", "graduate", "fresher", "intern", "0-2"]):
        score += 10

    # Remote preference / non-remote focus penalty
    remote_focus = os.environ.get("REMOTE_FOCUS", "true").lower() in ("true", "1", "yes")
    if jd.get("remote"):
        score += 5
    elif remote_focus and top_score_points > 0:
        score = max(20, score - 10)

    # Penalize explicit senior-only, >=3 yr experience, or foreign work-auth knockouts in Zero-LLM mode
    try:
        from scripts.fetch_jds import _is_senior_only, _requires_senior_experience, has_geo_knockout_in_text
        raw_title = jd.get("title") or ""
        raw_desc = jd.get("description") or ""
        if _is_senior_only(raw_title) or _requires_senior_experience(raw_desc) or has_geo_knockout_in_text(raw_title, raw_desc):
            score = min(score, 35)
    except Exception:
        pass

    score = min(90, max(20, score))

    return {
        "best_variant": best_variant,
        "score": score,
        "missing_skills": [],
        "reasoning": f"Zero-LLM keyword heuristic: matched variant '{best_variant}' (relevance score {score}).",
        "zero_llm": True
    }


# ── Groq scoring ──────────────────────────────────────────────────────────────

# Fix #2: these are real, publicly available Groq model IDs (as of 2026)
# Fix #12: client is NOT instantiated at module level — lazy-init inside function
# Active Groq JSON-capable models verified against /v1/models (2026-11-12)
# Retired/404 models removed: groq/compound, groq/compound-mini, llama-3.3-70b-versatile
FALLBACK_MODELS = [
    "openai/gpt-oss-20b",          # fast, structured JSON reliable -> PRIMARY
    "openai/gpt-oss-120b",         # deeper reasoning & higher OTPM limit -> SECONDARY
    "qwen/qwen3.8-27b",            # fast fallback (OTPM capped at 1000) -> TERTIARY
    "openai/gpt-oss-safeguard-20b",# last resort
]

@retry(
    wait=wait_exponential(multiplier=1, min=2, max=10),
    stop=stop_after_attempt(3),
    retry=_should_retry_groq
)
def score_jd_against_all_variants(jd: dict, variants: dict[str, dict]) -> dict[str, Any]:
    """
    Call Groq ONCE to evaluate the JD against all resume variants simultaneously.
    Returns: {best_variant, score, missing_skills, reasoning}.
    Reduces LLM calls and latency by 75%.
    """
    jd_description = jd.get('description', '').strip()
    description_note = (
        "NOTE: The job description body is empty or very short. "
        "Score conservatively based on visible title, location eligibility, and seniority — "
        "do NOT assume unstated details are a strong match; cap sparse-JD scores below 60 unless the title explicitly specifies an early-career/junior role in the candidate's core stack."
        if len(jd_description) < 100 else ""
    )

    variants_summary = {}
    for vname, vdata in variants.items():
        variants_summary[vname] = {
            "skills": vdata.get("skills", []),
            "summary": vdata.get("summary", ""),
            "experience_titles": [exp.get("role") or exp.get("title", "") for exp in vdata.get("experience", [])]
        }

    prompt = f"""
    You are a strict technical recruiter matching a job description against a candidate's resume variants.
    Candidate Background:
    - Master of Computer Applications (MCA, CGPA 9.03/10) & BCA (CGPA 8.20/10).
    - Based in Jaipur, India; Primary Goal is landing a **remote-first engineering role** (India Remote or truly global/worldwide EOR remote with zero visa barrier). Strongly favor remote=True roles over onsite roles.
    - Early-career engineer with 0-2 years of total experience across full-stack, frontend (React/Next.js/TypeScript), backend (Python/Django/Node.js/PostgreSQL/MongoDB), AI/LLM automation pipelines, QA/SDET (Playwright/pytest), and Solutions/Developer Support.
    {description_note}

    Job Listing:
    Title: {jd.get('title', '')}
    Company: {jd.get('company', '')}
    Location: {jd.get('location', '')}
    Remote: {jd.get('remote', False)}
    Description: {jd_description[:3000] if jd_description else '(not provided — score conservatively)'}

    Candidate Resume Variants:
    {json.dumps(variants_summary, indent=2)}

    Scoring Guidelines (Apply strictly so only real callback-viable jobs score >= 65):
    1. Mandatory Knockout Gates (MUST assign score <= 35):
       - Seniority mismatch: explicitly requires 3+ years experience or senior/staff/principal/lead/architect/manager level.
       - Work-authorization / location wall: requires US/UK/Canada/EU legal work authorization, citizenship, permanent residence, security clearance, or physical relocation outside India.
       - Core stack mismatch: enterprise ERP/CRM or unrelated stacks (Salesforce, ServiceNow, SAP, PeopleSoft, Workday, Oracle Fusion, embedded C/RTOS, hardware).
    2. Calibration:
       - 80-100: Early-career (0-2 yrs), India or India-accessible remote, strong overlap with candidate's real stack & projects.
       - 65-79: Solid fit with most required technologies and compatible location/seniority.
       - < 65: Weak alignment, heavy seniority/location mismatch, or missing core stack requirements.
    3. Variant Selection:
       - Pick the single variant with the highest true skill & project alignment.
    4. Return JSON with exactly these fields:
       - "best_variant": string, must be one of {list(variants.keys())}.
       - "score": integer 0-100 (match quality of the chosen best variant).
       - "missing_skills": list of strings for critical skills in the JD that are absent from this best variant.
       - "reasoning": 1-2 sentence explanation of why this variant is the best match or why it failed a gate.
    """

    client = Groq()
    last_error = None
    all_daily_limited = True

    forced_model = os.environ.get("GROQ_MODEL")
    models_to_try = [forced_model] if forced_model else FALLBACK_MODELS

    for model_name in models_to_try:
        try:
            response = client.chat.completions.create(
                model=model_name,
                messages=[
                    {"role": "system", "content": "You output JSON only."},
                    {"role": "user", "content": prompt},
                ],
                response_format={"type": "json_object"},
                temperature=0,
                max_tokens=600,
            )
            result = json.loads(_clean_llm_json(response.choices[0].message.content))
            if isinstance(result, list):
                result = result[0] if result else {}

            best_v = result.get("best_variant", "")
            if best_v not in variants and variants:
                # Default to first variant if model returned an unlisted name
                best_v = list(variants.keys())[0]

            score = int(result.get("score", 0))
            remote_focus = os.environ.get("REMOTE_FOCUS", "true").lower() in ("true", "1", "yes")
            # Remote preference calibration: boost competitive remote jobs; demote onsite jobs under REMOTE_FOCUS
            if jd.get("remote") and score >= 50:
                score = min(100, score + 5)
            elif not jd.get("remote") and remote_focus and score > 0:
                score = max(0, score - 10)

            return {
                "best_variant": best_v,
                "score": score,
                "missing_skills": result.get("missing_skills", []),
                "reasoning": result.get("reasoning", "")
            }
        except GroqError as e:
            logger.warning("Groq API error for model %s: %s", model_name, e)
            last_error = e
            if not _is_daily_limit_error(e):
                all_daily_limited = False
            if forced_model:
                raise e
            logger.info("Attempting next fallback model...")
        except Exception as e:
            logger.error("Unexpected error scoring JD: %s", e)
            default_v = list(variants.keys())[0] if variants else "backend-node"
            return {"best_variant": default_v, "score": 0, "missing_skills": [], "reasoning": f"Error: {e}"}

    if last_error:
        if all_daily_limited:
            raise DailyRateLimitExceeded(f"Daily quota reached on all Groq models: {last_error}")
        raise last_error

    default_v = list(variants.keys())[0] if variants else "backend-node"
    return {"best_variant": default_v, "score": 0, "missing_skills": [], "reasoning": "Failed after trying fallback models."}


# ── Backward-compatible single-variant scorer ─────────────────────────────────

@retry(
    wait=wait_exponential(multiplier=1, min=2, max=10),
    stop=stop_after_attempt(3),
    retry=_should_retry_groq
)
def score_jd_against_variant(jd: dict, variant: dict) -> dict[str, Any]:
    """Call Groq to score a single (JD, variant) pair (kept for compatibility)."""
    return score_jd_against_all_variants(jd, {variant.get("variant", "variant"): variant})


# ── Main entry ────────────────────────────────────────────────────────────────

def score_and_pick(jd: dict, zero_llm_mode: bool = False) -> dict:
    """
    Score JD against variants via consolidated Groq call, or keyword heuristic if zero_llm_mode is active.
    If best score < SCORE_THRESHOLD, result includes status="skipped".
    """
    variants = load_all_variants()
    if not variants:
        logger.error("No variants found in resumes/")
        return {"jd_id": f"{jd.get('company', '')}_{jd.get('title', '')}", "best_variant": "", "score": 0, "status": "error"}

    res = None
    rate_limit_tripped = False

    if zero_llm_mode:
        logger.info("Scoring JD via fast Zero-LLM keyword heuristic (circuit breaker active)...")
        res = keyword_score_jd(jd, variants)
    else:
        logger.info("Scoring JD against all variants via consolidated Groq call...")
        try:
            res = score_jd_against_all_variants(jd, variants)
        except (DailyRateLimitExceeded, GroqError) as e:
            logger.warning("Groq scoring unavailable (%s). Falling back to Zero-LLM keyword heuristic.", e)
            res = keyword_score_jd(jd, variants)
            rate_limit_tripped = True

    best_variant_name = res.get("best_variant", list(variants.keys())[0])
    score = res.get("score", 0)

    final_result = {
        "jd_id": f"{jd.get('company', '')}_{jd.get('title', '')}".replace(" ", "_").lower(),
        "best_variant": best_variant_name,
        "score": score,
        "missing_skills": res.get("missing_skills", []),
        "reasoning": res.get("reasoning", ""),
        "rate_limit_tripped": rate_limit_tripped,
        "zero_llm": res.get("zero_llm", False),
    }

    if final_result["score"] < SCORE_THRESHOLD:
        final_result["status"] = "skipped"
        logger.info("JD skipped: highest score %d is below threshold %d", final_result["score"], SCORE_THRESHOLD)
    else:
        logger.info("JD passed with score %d using variant %s", final_result["score"], best_variant_name)

    return final_result


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    if len(sys.argv) < 2:
        print("Usage: python score_and_pick.py <path/to/fixture_jd.json>")
        sys.exit(1)
    jd = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    result = score_and_pick(jd)
    print(json.dumps(result, indent=2))
