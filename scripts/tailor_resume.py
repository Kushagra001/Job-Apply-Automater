"""
tailor_resume.py — Resume Tailoring
=====================================
Uses Groq (Llama 3.3 70B) to re-rank bullets and refresh the summary of the
best-matched resume variant to mirror a specific JD — without fabricating
any new content.

Rules (enforced by prompt and post-validation):
  - NEVER add bullets that don't exist in the source variant.
  - NEVER add skills not present in source skills list.
  - Bullet count per role must remain unchanged.
  - Only re-rank bullets and lightly rephrase the summary.

Input:
  - variant_json: dict — one resume variant (from resumes/*.json)
  - jd: dict           — the target JD (from fetch_jds.py)

Output:
  - A deep copy of variant_json with re-ranked bullets and updated summary.
    The source file on disk is NEVER mutated.
"""

from __future__ import annotations

import copy
import json
import logging
import os
from pathlib import Path
from typing import Any
from groq import Groq, GroqError
from tenacity import retry, wait_exponential, stop_after_attempt, retry_if_exception_type

from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)


# ── Schema validation ─────────────────────────────────────────────────────────

def validate_tailored(original: dict, tailored: dict) -> None:
    """
    Assert that the tailored variant conforms to the source schema:
    - All required top-level keys present.
    - Per-role bullet counts unchanged.
    - No new skills added.
    Raises ValueError on violation.
    """
    required_keys = ["variant", "summary", "experience", "skills", "projects", "education", "certifications"]
    for key in required_keys:
        if key not in tailored:
            raise ValueError(f"Missing required key in tailored resume: {key}")

    # Validate experience bullets count
    orig_exp = original.get("experience", [])
    tail_exp = tailored.get("experience", [])
    if len(orig_exp) != len(tail_exp):
        raise ValueError("Number of experience roles changed during tailoring.")
    
    for o_role, t_role in zip(orig_exp, tail_exp):
        if len(o_role.get("bullets", [])) != len(t_role.get("bullets", [])):
            raise ValueError(f"Bullet count changed for role {o_role.get('company')}.")

    # Validate skills (no new skills)
    orig_skills = set(s.lower().strip() for s in original.get("skills", []))
    tail_skills = set(s.lower().strip() for s in tailored.get("skills", []))
    new_skills = tail_skills - orig_skills
    if new_skills:
        raise ValueError(f"LLM hallucinated new skills: {new_skills}")

    # Validate project bullets count
    orig_proj = original.get("projects", [])
    tail_proj = tailored.get("projects", [])
    if len(orig_proj) != len(tail_proj):
        raise ValueError("Number of projects changed during tailoring.")
    
    for o_proj, t_proj in zip(orig_proj, tail_proj):
        if len(o_proj.get("bullets", [])) != len(t_proj.get("bullets", [])):
            raise ValueError(f"Bullet count changed for project {o_proj.get('name')}.")



# ── Groq tailoring ────────────────────────────────────────────────────────────

@retry(
    wait=wait_exponential(multiplier=1, min=2, max=10),
    stop=stop_after_attempt(3),
    retry=retry_if_exception_type(GroqError)
)
def tailor_resume(variant_json: dict, jd: dict) -> dict:
    """
    Return a tailored deep copy of variant_json optimised for jd.
    Never mutates variant_json.
    Model: llama-3.3-70b-versatile
    """
    client = Groq()
    
    prompt = f"""
    You are an expert technical resume writer. You must tailor the provided resume to the provided job description.
    
    Rules (CRITICAL - DO NOT BREAK):
    1. DO NOT add any new bullet points. You can only re-order the existing bullets.
    2. DO NOT add any new skills that are not already in the original skills list.
    3. You MAY rewrite the 'summary' field to better align with the job description.
    4. You MAY slightly rephrase existing bullets for better impact, but do NOT invent new responsibilities, metrics, or technologies.
    5. The number of bullets per experience role and per project MUST remain EXACTLY the same.
    6. Return a valid JSON object matching the exact schema of the original resume.

    Job Description:
    {json.dumps(jd, indent=2)}
    
    Original Resume:
    {json.dumps(variant_json, indent=2)}
    """
    
    try:
        model_name = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
        response = client.chat.completions.create(
            model=model_name,
            messages=[
                {"role": "system", "content": "You output JSON only. Adhere strictly to the schema and anti-hallucination rules."},
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
            temperature=0,
        )
        tailored = json.loads(response.choices[0].message.content)
        validate_tailored(variant_json, tailored)
        return tailored
    except ValueError as ve:
        logger.error("Validation failed: %s. Returning original variant.", ve)
        return copy.deepcopy(variant_json)
    except Exception as e:
        logger.error("Groq API call failed: %s", e)
        return copy.deepcopy(variant_json)



if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    if len(sys.argv) < 3:
        print("Usage: python tailor_resume.py <variant.json> <jd.json>")
        sys.exit(1)
    variant = json.loads(Path(sys.argv[1]).read_text())
    jd = json.loads(Path(sys.argv[2]).read_text())
    result = tailor_resume(variant, jd)
    print(json.dumps(result, indent=2))
