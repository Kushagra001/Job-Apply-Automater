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
import time  # Fix #11: top-level import, not inside function
from pathlib import Path
from typing import Any, TYPE_CHECKING
from groq import Groq, GroqError, RateLimitError
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
            data = json.loads(file.read_text())
            variant_name = data.get("variant", file.stem)
            variants[variant_name] = data
        except Exception as e:
            logger.error("Failed to load %s: %s", file, e)
    return variants


# ── Groq scoring ──────────────────────────────────────────────────────────────

# Fix #2: these are real, publicly available Groq model IDs (as of 2026)
# Fix #12: client is NOT instantiated at module level — lazy-init inside function
# Models confirmed available on this Groq account (queried 2026-08-30 via /v1/models)
# Priority: compound first (no rate limits observed), gpt-oss-120b last (32-min rate limit waits)
FALLBACK_MODELS = [
    "groq/compound",          # fast, no rate limits observed, good quality → PRIMARY
    "groq/compound-mini",     # fastest, slightly lower quality → SECONDARY
    "openai/gpt-oss-20b",     # rate limited but better quality → TERTIARY
    "openai/gpt-oss-120b",    # best quality but severe rate limits → LAST RESORT
]

@retry(
    wait=wait_exponential(multiplier=1, min=2, max=10),
    stop=stop_after_attempt(3),
    retry=retry_if_exception_type(GroqError)
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
        "Score primarily based on the job title, company, and typical requirements for that role. "
        "Be generous — if the title clearly matches the candidate's stack, score 70+."
        if len(jd_description) < 100 else ""
    )

    variants_summary = {}
    for vname, vdata in variants.items():
        variants_summary[vname] = {
            "skills": vdata.get("skills", []),
            "summary": vdata.get("summary", ""),
            "experience_titles": [exp.get("title", "") for exp in vdata.get("experience", [])]
        }

    prompt = f"""
    You are an expert technical recruiter matching a job description against a candidate's resume variants.
    Candidate Background:
    - Master of Computer Applications (MCA, CGPA 9.03/10) & BCA (CGPA 8.20/10).
    - Early career engineer with 0–2 years of total experience.
    - Strong technical foundation across full-stack, frontend, backend (Python/Node), AI/ML engineering, QA/SDET automation, and Solutions/Technical Support engineering.
    {description_note}

    Job Listing:
    Title: {jd.get('title', '')}
    Company: {jd.get('company', '')}
    Location: {jd.get('location', '')}
    Remote: {jd.get('remote', False)}
    Description: {jd_description[:3000] if jd_description else '(not provided — use title/company to infer requirements)'}

    Candidate Resume Variants:
    {json.dumps(variants_summary, indent=2)}

    Scoring Guidelines:
    1. Experience Level Gate:
       - If the JD strictly requires 3+ years of experience or is a mid/senior/staff/lead role, assign a score BELOW 40 (status skipped).
       - If the JD is early career, junior, associate, fresher, graduate, or 0–2 years experience, evaluate favorably. If core tech skills align, score 70+.
    2. Variant Selection:
       - Pick the variant that provides the highest alignment with the JD's requirements.
       - Available variants include software development, QA/SDET, and Solutions/Support roles.
    3. Return JSON with exactly these fields:
       - "best_variant": string, must be one of {list(variants.keys())}.
       - "score": integer 0-100 (match quality of the chosen best variant).
       - "missing_skills": list of strings for critical skills in the JD that are absent from this best variant.
       - "reasoning": 1-2 sentence explanation of why this variant is the best match.
    """

    client = Groq()
    last_error = None

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
            )
            result = json.loads(response.choices[0].message.content)
            if isinstance(result, list):
                result = result[0] if result else {}

            best_v = result.get("best_variant", "")
            if best_v not in variants and variants:
                # Default to first variant if model returned an unlisted name
                best_v = list(variants.keys())[0]

            score = int(result.get("score", 0))
            # Remote preference bonus: +5 points for remote jobs if base score is already competitive (>= 50)
            if jd.get("remote") and score >= 50:
                score = min(100, score + 5)

            return {
                "best_variant": best_v,
                "score": score,
                "missing_skills": result.get("missing_skills", []),
                "reasoning": result.get("reasoning", "")
            }
        except GroqError as e:
            logger.warning("Groq API error for model %s: %s", model_name, e)
            last_error = e
            if forced_model:
                raise e
            logger.info("Attempting next fallback model...")
        except Exception as e:
            logger.error("Unexpected error scoring JD: %s", e)
            default_v = list(variants.keys())[0] if variants else "backend-node"
            return {"best_variant": default_v, "score": 0, "missing_skills": [], "reasoning": f"Error: {e}"}

    if last_error:
        raise last_error

    default_v = list(variants.keys())[0] if variants else "backend-node"
    return {"best_variant": default_v, "score": 0, "missing_skills": [], "reasoning": "Failed after trying fallback models."}


# ── Backward-compatible single-variant scorer ─────────────────────────────────

@retry(
    wait=wait_exponential(multiplier=1, min=2, max=10),
    stop=stop_after_attempt(3),
    retry=retry_if_exception_type(GroqError)
)
def score_jd_against_variant(jd: dict, variant: dict) -> dict[str, Any]:
    """Call Groq to score a single (JD, variant) pair (kept for compatibility)."""
    return score_jd_against_all_variants(jd, {variant.get("variant", "variant"): variant})


# ── Main entry ────────────────────────────────────────────────────────────────

def score_and_pick(jd: dict) -> dict:
    """
    Score JD against all four variants in a single consolidated Groq call.
    If best score < SCORE_THRESHOLD, result includes status="skipped".
    """
    variants = load_all_variants()
    if not variants:
        logger.error("No variants found in resumes/")
        return {"jd_id": f"{jd.get('company', '')}_{jd.get('title', '')}", "best_variant": "", "score": 0, "status": "error"}

    logger.info("Scoring JD against all variants via consolidated Groq call...")
    res = score_jd_against_all_variants(jd, variants)

    best_variant_name = res.get("best_variant", list(variants.keys())[0])
    score = res.get("score", 0)

    final_result = {
        "jd_id": f"{jd.get('company', '')}_{jd.get('title', '')}".replace(" ", "_").lower(),
        "best_variant": best_variant_name,
        "score": score,
        "missing_skills": res.get("missing_skills", []),
        "reasoning": res.get("reasoning", "")
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
    jd = json.loads(Path(sys.argv[1]).read_text())
    result = score_and_pick(jd)
    print(json.dumps(result, indent=2))
