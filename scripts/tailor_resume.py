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
import re
from pathlib import Path
from groq import Groq, GroqError
from tenacity import retry, wait_exponential, stop_after_attempt, retry_if_exception_type

from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)


# ── Schema validation ─────────────────────────────────────────────────────────

class DailyRateLimitExceeded(GroqError):
    """Raised when Groq daily quota (TPD or RPD) is exhausted across all fallback models."""
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


def _sanitize_typography(obj):
    """Normalize typographical unicode dashes/NBSP to ASCII across nested resume data."""
    if isinstance(obj, str):
        return (
            obj.replace("\u2011", "-")
            .replace("\u2013", "-")
            .replace("\u2014", "-")
            .replace("\u2212", "-")
            .replace("\u00a0", " ")
        )
    if isinstance(obj, list):
        return [_sanitize_typography(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _sanitize_typography(v) for k, v in obj.items()}
    return obj


def _fallback_cover_letter(tailored: dict, jd: dict) -> str:
    """Synthesize a concise, grounded 3-sentence cover letter if LLM left 'cover_letter' blank."""
    title = (jd.get("title") or "Software Engineer").strip()
    company = (jd.get("company") or "your team").strip()
    projects = tailored.get("projects") or []
    proj_name = "BidCopy"
    proj_detail = "an AI-powered full-stack SaaS using Next.js, TypeScript, and LLM workflows"
    if projects and isinstance(projects[0], dict):
        proj_name = str(projects[0].get("name") or proj_name)
        bullets = projects[0].get("bullets") or []
        if bullets:
            proj_detail = str(bullets[0]).rstrip(".")

    skills_str = ", ".join((tailored.get("skills") or [])[:5]) or "Python, TypeScript, React, and REST APIs"
    is_remote_global = bool(jd.get("remote")) and "india" not in (jd.get("location") or "").lower()
    geo_note = (
        "Based in Jaipur, India, I work effectively in async-first remote teams with flexible IST time-zone overlap and immediate EOR/contractor readiness."
        if is_remote_global
        else "Based in Jaipur, India (MCA 9.03 CGPA), I am available immediately to contribute in fast-paced engineering sprints."
    )
    return (
        f"I am applying for the {title} role at {company}, bringing hands-on full-stack and automation experience across {skills_str}. "
        f"Most recently, I built {proj_name} ({proj_detail}), along with production client deliveries at Stackform and GYMYAK Pvt. Ltd. "
        f"{geo_note}"
    )


def validate_tailored(original: dict, tailored: dict) -> None:
    """
    Assert and repair the tailored variant so it conforms to the source schema:
    - Auto-merge any missing non-critical keys from original.
    - Repair bullet counts if LLM added or removed bullets.
    - Filter out hallucinated skills while preserving valid tailored content.
    """
    if not isinstance(tailored, dict):
        raise ValueError("Tailored resume is not a dictionary.")

    # 1. Ensure required keys are present and immutable identity sections stay intact
    required_keys = ["variant", "contact", "summary", "experience", "skills", "projects", "education", "certifications"]
    for key in required_keys:
        if key not in tailored or tailored[key] is None:
            tailored[key] = copy.deepcopy(original.get(key, [] if key in ("skills", "projects", "education", "certifications") else {}))
    for immutable_key in ("variant", "contact", "education", "certifications"):
        if immutable_key in original:
            tailored[immutable_key] = copy.deepcopy(original[immutable_key])

    # 2. Validate experience roles and bullet counts
    orig_exp = original.get("experience", [])
    tail_exp = tailored.get("experience", [])
    if not isinstance(tail_exp, list) or len(orig_exp) != len(tail_exp):
        tailored["experience"] = copy.deepcopy(orig_exp)
    else:
        for idx, (o_role, t_role) in enumerate(zip(orig_exp, tail_exp)):
            if not isinstance(t_role, dict):
                tail_exp[idx] = copy.deepcopy(o_role)
                continue
            for immutable_field in ("company", "role", "dates"):
                if immutable_field in o_role:
                    t_role[immutable_field] = o_role[immutable_field]
            orig_bullets = o_role.get("bullets", [])
            tail_bullets = t_role.get("bullets", [])
            if not isinstance(tail_bullets, list) or len(orig_bullets) != len(tail_bullets) or not all(isinstance(b, str) for b in tail_bullets):
                t_role["bullets"] = copy.deepcopy(orig_bullets)

    # 3. Clean up hallucinated skills without discarding the tailored resume
    orig_skills = [s.strip() for s in original.get("skills", []) if isinstance(s, str)]
    orig_skills_set = set(s.lower() for s in orig_skills)
    tail_skills = tailored.get("skills", [])
    if isinstance(tail_skills, list):
        cleaned_skills = [s for s in tail_skills if isinstance(s, str) and s.lower().strip() in orig_skills_set]
        tailored["skills"] = cleaned_skills if cleaned_skills else copy.deepcopy(orig_skills)
    else:
        tailored["skills"] = copy.deepcopy(orig_skills)

    # 4. Validate project bullets
    orig_proj = original.get("projects", [])
    tail_proj = tailored.get("projects", [])
    if not isinstance(tail_proj, list) or len(orig_proj) != len(tail_proj):
        tailored["projects"] = copy.deepcopy(orig_proj)
    else:
        for idx, (o_proj, t_proj) in enumerate(zip(orig_proj, tail_proj)):
            if not isinstance(t_proj, dict):
                tail_proj[idx] = copy.deepcopy(o_proj)
                continue
            for immutable_field in ("name", "stack"):
                if immutable_field in o_proj:
                    t_proj[immutable_field] = o_proj[immutable_field]
            orig_bullets = o_proj.get("bullets", [])
            tail_bullets = t_proj.get("bullets", [])
            if not isinstance(tail_bullets, list) or len(orig_bullets) != len(tail_bullets) or not all(isinstance(b, str) for b in tail_bullets):
                t_proj["bullets"] = copy.deepcopy(orig_bullets)


def _prioritize_matching_skills(skills: list[str], jd: dict) -> list[str]:
    """
    Deterministically move real skills explicitly mentioned in the JD to the front
    of the skills list (preserving relative order within matched/unmatched groups).
    Never invents or removes skills.
    """
    if not skills or not isinstance(jd, dict):
        return list(skills or [])
    haystack = f"{jd.get('title', '')} {jd.get('description', '')[:3000]}".lower()
    if not haystack.strip():
        return list(skills)

    matched: list[str] = []
    rest: list[str] = []
    for raw_skill in skills:
        if not isinstance(raw_skill, str):
            continue
        core = re.sub(r"\s*\([^)]*\)", "", raw_skill).strip().lower()
        variants_to_check = [core]
        if core.endswith(".js"):
            variants_to_check.append(core[:-3])
        if core == "postgresql":
            variants_to_check.append("postgres")
        elif core == "rest apis":
            variants_to_check.extend(["rest api", "restful"])
        elif core == "ci/cd" or core == "ci/cd pipelines":
            variants_to_check.append("ci/cd")

        is_hit = False
        for tok in variants_to_check:
            tok = tok.strip()
            if len(tok) < 2:
                continue
            pat = re.compile(r"(?<!\w)" + re.escape(tok) + r"(?!\w)", re.IGNORECASE)
            if pat.search(haystack):
                is_hit = True
                break
        if is_hit:
            matched.append(raw_skill)
        else:
            rest.append(raw_skill)
    return matched + rest


# ── Groq tailoring ────────────────────────────────────────────────────────────

# Active Groq JSON-capable models verified against /v1/models (2026-11-12)
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
def tailor_resume(variant_json: dict, jd: dict) -> dict:
    """
    Return a tailored deep copy of variant_json optimised for jd.
    Never mutates variant_json.
    """
    # Fix #12: lazy-init — don't read GROQ_API_KEY at import time
    client = Groq()

    is_remote_outside_india = bool(jd.get("remote")) and "india" not in (jd.get("location") or "").lower()
    remote_cl_note = (
        "Mention remote collaboration readiness from India (strong async communication, flexible IST overlap, and international contractor/EOR readiness with zero visa barrier)."
        if is_remote_outside_india else ""
    )

    prompt = f"""
    You are an expert technical resume writer. You must tailor the provided resume to the provided job description.
    
    Rules (CRITICAL - DO NOT BREAK):
    1. DO NOT add any new bullet points. Re-order existing bullets so the most relevant achievements appear first per role/project.
    2. DO NOT add any new skills not already in original_skills. Re-order existing skills so skills requested by the JD appear first.
    3. Rewrite the 'summary' field in 2-3 punchy sentences aligned to this exact role without fabricating experience or inflating numbers.
    4. You MAY lightly rephrase existing bullets to mirror the JD's terminology truthfully, keeping every metric factual and the bullet count per experience role and per project IDENTICAL.
    5. Generate a sharp, grounded 3-4 sentence "cover_letter" mentioning one specific project from this resume (e.g., BidCopy, Stackform, GYMYAK, Medical-Chatbot, or Automated Job Pipeline) relevant to the company/role — NO generic filler ("I am excited to apply..."). {remote_cl_note}
    6. Return a single valid JSON object containing all schema fields of Original Resume PLUS a required non-empty top-level string field "cover_letter".

    Job Description:
    {json.dumps(jd, indent=2)}
    
    Original Resume:
    {json.dumps(variant_json, indent=2)}
    """

    last_error = None
    all_daily_limited = True

    forced_model = os.environ.get("GROQ_MODEL")
    models_to_try = [forced_model] if forced_model else FALLBACK_MODELS

    for model_name in models_to_try:
        try:
            create_kwargs = {
                "model": model_name,
                "messages": [
                    {"role": "system", "content": "You output JSON only. Adhere strictly to the schema and anti-hallucination rules."},
                    {"role": "user", "content": prompt},
                ],
                "response_format": {"type": "json_object"},
                "temperature": 0,
            }
            if "qwen" in model_name.lower():
                create_kwargs["max_tokens"] = 950
            response = client.chat.completions.create(**create_kwargs)
            tailored = json.loads(_clean_llm_json(response.choices[0].message.content))
            if isinstance(tailored, list):
                tailored = tailored[0] if tailored else {}
            validate_tailored(variant_json, tailored)
            tailored = _sanitize_typography(tailored)
            tailored["skills"] = _prioritize_matching_skills(tailored.get("skills", []), jd)
            if not str(tailored.get("cover_letter") or "").strip():
                tailored["cover_letter"] = _fallback_cover_letter(tailored, jd)
            return tailored
        except GroqError as e:
            logger.warning("Groq API error (tailoring) for model %s: %s", model_name, e)
            last_error = e
            if not _is_daily_limit_error(e):
                all_daily_limited = False
            if forced_model:
                raise e  # User explicitly forced a model — don't silently fall back
            logger.info("Attempting next fallback model...")
        except ValueError as ve:
            logger.error("Validation failed: %s. Returning original variant.", ve)
            return copy.deepcopy(variant_json)
        except Exception as e:
            logger.error("Unexpected error tailoring resume: %s", e)
            return copy.deepcopy(variant_json)

    if last_error:
        if all_daily_limited:
            raise DailyRateLimitExceeded(f"Daily quota reached on all Groq models during tailoring: {last_error}")
        raise last_error
    return copy.deepcopy(variant_json)




if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    if len(sys.argv) < 3:
        print("Usage: python tailor_resume.py <variant.json> <jd.json>")
        sys.exit(1)
    variant = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    jd = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
    result = tailor_resume(variant, jd)
    print(json.dumps(result, indent=2))
