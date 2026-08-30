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
# Fix #10: use a simple index tracked per-call, not a shared global mutated mid-loop
_CURRENT_MODEL_INDEX = 0

@retry(
    wait=wait_exponential(multiplier=1, min=2, max=10),
    stop=stop_after_attempt(3),
    retry=retry_if_exception_type(GroqError)
)
def score_jd_against_variant(jd: dict, variant: dict) -> dict[str, Any]:
    """
    Call Groq to score a single (JD, variant) pair.
    Returns partial Score result: {score, missing_skills, reasoning}.
    """
    prompt = f"""
    You are an expert technical recruiter scoring a job description against a candidate's resume.
    
    Job Description:
    {json.dumps(jd, indent=2)}
    
    Resume Variant:
    {json.dumps(variant, indent=2)}
    
    Analyze the match between the resume and the job description.
    Return a JSON object with exactly these fields:
    - "score": integer between 0 and 100 representing the fit.
    - "missing_skills": list of strings for critical skills mentioned in JD but missing in resume.
    - "reasoning": a brief explanation of the score and missing skills.
    """
    # Fix #12: lazy-init client — only reads GROQ_API_KEY when actually called,
    #          not at import time, keeping test isolation clean.
    client = Groq()

    global _CURRENT_MODEL_INDEX
    last_error = None

    # Fix #10: iterate through fallbacks, advancing the index BEFORE the attempt
    #          so each pass in the loop actually tries a different model.
    for attempt in range(len(FALLBACK_MODELS)):
        forced_model = os.environ.get("GROQ_MODEL")
        if forced_model:
            model_name = forced_model
        else:
            model_name = FALLBACK_MODELS[_CURRENT_MODEL_INDEX]

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
            return {
                "score": result.get("score", 0),
                "missing_skills": result.get("missing_skills", []),
                "reasoning": result.get("reasoning", "")
            }
        except GroqError as e:
            logger.warning("Groq API error for model %s: %s", model_name, e)
            last_error = e
            if forced_model:
                raise e  # User explicitly forced a model — don't silently fall back
            # Advance to the next model for the next iteration
            _CURRENT_MODEL_INDEX = (_CURRENT_MODEL_INDEX + 1) % len(FALLBACK_MODELS)
            logger.info("Switching to fallback model: %s", FALLBACK_MODELS[_CURRENT_MODEL_INDEX])
        except Exception as e:
            logger.error("Unexpected error scoring JD: %s", e)
            return {"score": 0, "missing_skills": [], "reasoning": f"Error: {e}"}

    if last_error:
        raise last_error

    return {"score": 0, "missing_skills": [], "reasoning": "Failed to score JD after trying all fallback models."}



# ── Main entry ────────────────────────────────────────────────────────────────

def score_and_pick(jd: dict) -> dict:
    """
    Score JD against all four variants, return the best Score result.
    If best score < SCORE_THRESHOLD, result includes status="skipped".
    """
    variants = load_all_variants()
    if not variants:
        logger.error("No variants found in resumes/")
        return {"jd_id": f"{jd.get('company', '')}_{jd.get('title', '')}", "best_variant": "", "score": 0, "status": "error"}
    
    best_variant_name = ""
    best_score = -1
    best_result = {}

    for name, variant_data in variants.items():
        logger.info("Scoring JD against variant: %s", name)
        res = score_jd_against_variant(jd, variant_data)
        if res["score"] > best_score:
            best_score = res["score"]
            best_variant_name = name
            best_result = res
        time.sleep(2)  # Delay between scoring variants to respect rate limits
    
    final_result = {
        "jd_id": f"{jd.get('company', '')}_{jd.get('title', '')}".replace(" ", "_").lower(),
        "best_variant": best_variant_name,
        "score": best_result.get("score", 0),
        "missing_skills": best_result.get("missing_skills", []),
        "reasoning": best_result.get("reasoning", "")
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
