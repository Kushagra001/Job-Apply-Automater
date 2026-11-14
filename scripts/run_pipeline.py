import argparse
import os
import sys
import time
import json
import logging
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.fetch_jds import fetch_all, _requires_senior_experience, has_geo_knockout_in_text
from scripts.enrich_jd import enrich_jd
from scripts.doctor import run_doctor
from scripts.score_and_pick import score_and_pick
from scripts.tailor_resume import tailor_resume, _prioritize_matching_skills, DailyRateLimitExceeded
from scripts.render_pdf import render_pdf
from scripts.auto_apply_ats import apply
from scripts.outreach import create_outreach_draft, should_draft_outreach
from scripts.log_and_notify import (
    log_result,
    send_daily_digest,
    normalize_company_name,
    get_company_apply_history,
    check_company_cooldown,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("run_pipeline")

import requests

# Fix #1: absolute path — never relative to CWD
RESUMES_DIR = PROJECT_ROOT / "resumes"

# Fix #17: cap JDs processed per run to avoid runaway Groq spend / 429 quota exhaustion on free tier
MAX_JOBS_PER_RUN = int(os.environ.get("MAX_JOBS_PER_RUN", "25"))

# Strict quality-first thresholds aligned with AGENTS.md (score >= 65 mandatory gate)
SCORE_TAILOR_THRESHOLD = int(os.environ.get("SCORE_TAILOR_THRESHOLD", "65"))   # >= 65: tailor + apply
SCORE_VOLUME_FLOOR = int(os.environ.get("SCORE_VOLUME_FLOOR", "65"))           # < 65: skip entirely

COOLDOWN_DAYS_PER_COMPANY = int(os.environ.get("COOLDOWN_DAYS_PER_COMPANY", "14"))
MAX_APPLICATIONS_PER_COMPANY_30D = int(os.environ.get("MAX_APPLICATIONS_PER_COMPANY_30D", "2"))


def _is_viable_apply_url(apply_url: str, source: str) -> tuple[bool, str]:
    """
    Pre-validate apply URLs before spending LLM tokens on scoring and tailoring.
    Ensures that direct links and aggregator postings lead to supported ATS platforms
    (Greenhouse, Lever, Ashby, or Himalayas).
    """
    if not apply_url:
        return False, "Empty apply_url"

    _SUPPORTED_DOMAINS = (
        "greenhouse.io", "lever.co", "ashbyhq.com",
        "gh_jid=", "himalayas.app",
    )

    # Direct ATS links are immediately viable
    apply_url_lower = apply_url.lower()
    if any(domain in apply_url_lower for domain in _SUPPORTED_DOMAINS):
        return True, ""

    # For aggregators, inspect redirect target to ensure it lands on a supported ATS
    is_aggregator = source in ("remoteok", "remotive", "jobicy", "arbeitnow")
    if is_aggregator:
        try:
            resp = requests.get(
                apply_url,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
                timeout=5,
                allow_redirects=True,
            )
            final_url = resp.url.lower()
            if any(domain in final_url for domain in _SUPPORTED_DOMAINS):
                return True, ""
            # Parse explicit outbound <a href=...> targets instead of raw HTML text
            import re
            hrefs = re.findall(r"""href\s*=\s*["']([^"']+)["']""", resp.text, flags=re.IGNORECASE)
            if any(
                any(domain in h.lower() for domain in ("greenhouse.io", "lever.co", "ashbyhq.com", "gh_jid="))
                for h in hrefs
            ):
                return True, ""
            return False, f"Aggregator does not redirect to supported ATS: {resp.url[:80]}"
        except Exception as e:
            logger.debug("Failed pre-resolving aggregator URL %s: %s", apply_url, e)
            return False, f"Aggregator URL resolution failed: {e}"

    return False, f"Unsupported ATS domain in URL: {apply_url[:80]}"


def parse_args():
    parser = argparse.ArgumentParser(description="Automated Job Application Pipeline")
    parser.add_argument("--doctor", action="store_true", help="Run pre-flight health diagnostics and exit")
    parser.add_argument("--persistent-profile", action="store_true", help="Use persistent browser user-data directory for cookies/sessions")
    parser.add_argument("--dry-run", action="store_true", help="Run in dry-run mode (do not submit applications)")
    parser.add_argument("--remote-only", action="store_true", help="Filter exclusively for remote=True roles")
    parser.add_argument("--max-jobs", type=int, default=None, help="Maximum number of JDs to process this run")
    return parser.parse_args()


def _keyword_pick_variant(jd: dict) -> str:
    """
    Fast, zero-LLM variant picker for the low-score volume path.
    Matches job title keywords to the most suitable base resume variant.
    """
    title = (jd.get("title", "") + " " + jd.get("description", "")[:200]).lower()
    if any(k in title for k in ["qa", "sdet", "test", "quality assurance", "automation engineer", "tester"]):
        return "sdet-qa"
    if any(k in title for k in ["solutions", "solution", "support engineer", "technical support", "integration", "client support", "customer engineer"]):
        return "solutions-support"
    if any(k in title for k in ["frontend", "front-end", "react", "vue", "angular", "next.js", "ui engineer", "css"]):
        return "frontend"
    if any(k in title for k in ["ai engineer", "llm", "genai", "mlops", "generative ai"]):
        return "ai-engineer"
    if any(k in title for k in ["python", "django", "fastapi", "flask", "data engineer", "ml ", "machine learning"]):
        return "backend-python"
    if any(k in title for k in ["node", "typescript", "express", "nestjs", "nest.js", "backend"]):
        return "backend-node"
    return "backend-node"  # Safe default


def classify_apply_status(success: bool, apply_notes: str, dry_run: bool, success_status: str) -> str:
    """Map (success, apply_notes) from auto_apply_ats.apply() into canonical pipeline status values."""
    if success:
        return "dry_run_success" if dry_run else success_status

    notes_lower = (apply_notes or "").strip().lower()
    if "requires_captcha:" in notes_lower:
        return "requires_captcha"
    if "job_closed:" in notes_lower:
        return "job_closed"
    if "skipped_geo_knockout:" in notes_lower:
        return "skipped_geo_knockout"

    unsupported_markers = (
        "unsupported ats domain:",
        "redirect did not land on a supported ats:",
        "no nested flow for ats",
        "is a company board index",
        "is a company index",
        "lacks a specific job identifier",
    )
    if any(marker in notes_lower for marker in unsupported_markers):
        return "skipped_unsupported_ats"

    return "apply_failed"


def main():
    args = parse_args()
    if args.doctor:
        healthy = run_doctor()
        sys.exit(0 if healthy else 1)

    logger.info("Starting automated job application pipeline...")

    dry_run = args.dry_run or os.environ.get("DRY_RUN", "false").lower() == "true"
    if dry_run:
        os.environ["DRY_RUN"] = "true"
    if args.remote_only:
        os.environ["REMOTE_ONLY"] = "true"
    use_persistent = args.persistent_profile or os.environ.get("PLAYWRIGHT_PERSISTENT_PROFILE", "").lower() in ("true", "1", "yes")
    max_jobs = args.max_jobs if args.max_jobs is not None else MAX_JOBS_PER_RUN
    
    # 1. Fetch JDs
    logger.info("Fetching JDs from all sources...")
    try:
        jds = fetch_all()
    except Exception as e:
        logger.error(f"Failed to fetch JDs: {e}", exc_info=True)
        sys.exit(1)

    if not jds:
        logger.info("No JDs found. Exiting.")
        return

    # Deduplicate across sources based on (company, title)
    unique_jds = []
    seen_roles = set()
    for jd in jds:
        # Normalize to lowercase and strip whitespace for matching
        company = jd.get("company", "").lower().strip()
        title = jd.get("title", "").lower().strip()
        key = f"{company}::{title}"
        if key not in seen_roles:
            seen_roles.add(key)
            unique_jds.append(jd)

    logger.info(f"Deduplicated JDs: {len(jds)} raw -> {len(unique_jds)} unique.")
    jds = unique_jds

    max_scan = max(max_jobs * 4, max_jobs) if max_jobs > 0 else 0
    logger.info(f"Fetched {len(jds)} unique JDs. Processing up to {max_jobs} scoring/apply candidates (scanning up to {max_scan})...")

    # Process each JD
    zero_llm_mode = False
    consecutive_rate_limits = 0
    processed_count = 0
    company_history = get_company_apply_history(days=30)
    run_company_applied: dict[str, int] = {}

    for i, jd in enumerate(jds):
        if processed_count >= max_jobs or i >= max_scan:
            logger.info(f"Reached run budget (processed={processed_count}/{max_jobs}, scanned={i}/{max_scan}). Stopping.")
            break

        title = jd.get("title", "Unknown Title")
        company = jd.get("company", "Unknown Company")
        logger.info(f"--- Processing [{i+1}/{len(jds)}]: {title} at {company} ---")

        # Gate 0: per-company cooldown & 30-day cap (prevents burst spam to same company)
        on_cooldown, cooldown_reason = check_company_cooldown(
            company,
            history=company_history,
            run_counts=run_company_applied,
            cooldown_days=COOLDOWN_DAYS_PER_COMPANY,
            max_per_30d=MAX_APPLICATIONS_PER_COMPANY_30D,
        )
        if on_cooldown:
            logger.info(f"Company cooldown gate: skipping '{title}' at '{company}' ({cooldown_reason})")
            log_result(
                jd=jd,
                score=0,
                variant_used="N/A",
                status="skipped_company_cooldown",
                pdf_path="",
                notes=cooldown_reason,
            )
            continue

        # Gate 1: pre-validate apply URL (including pre-checking aggregator destination)
        apply_url = jd.get("apply_url", "")
        source = jd.get("source", "")
        viable, reason = _is_viable_apply_url(apply_url, source)
        if not viable:
            logger.warning(f"Unsupported ATS or apply URL for '{title}' at '{company}': {reason}")
            log_result(
                jd=jd,
                score=0,
                variant_used="N/A",
                status="skipped_unsupported_ats",
                pdf_path="",
                notes=f"Skipped before scoring: {reason}"
            )
            continue

        # Gate 2: JD Description Enrichment + Seniority & Geo-Knockout filter BEFORE scoring
        desc_text = jd.get("description", "").strip()
        if len(desc_text) < 250:
            logger.info(f"Description sparse (<250 chars); attempting enrichment for '{title}'...")
            jd = enrich_jd(jd)
            desc_text = jd.get("description", "").strip()

        if _requires_senior_experience(desc_text):
            logger.info(f"JD requires senior experience (>=3 yrs). Skipping '{title}' at '{company}'.")
            log_result(
                jd=jd,
                score=0,
                variant_used="N/A",
                status="skipped_senior_role",
                pdf_path="",
                notes="Filtered out before scoring (requires >= 3 years experience).",
            )
            continue

        if has_geo_knockout_in_text(title, desc_text):
            logger.info(f"JD imposes US/UK/CA/EU work-auth/clearance wall. Skipping '{title}' at '{company}'.")
            log_result(
                jd=jd,
                score=0,
                variant_used="N/A",
                status="skipped_geo_knockout",
                pdf_path="",
                notes="Filtered out before scoring (foreign work-authorization or residence requirement).",
            )
            continue

        processed_count += 1

        # 2. Score with Groq (or fast Zero-LLM keyword heuristic if circuit breaker active)
        logger.info(f"Scoring JD...")
        try:
            score_result = score_and_pick(jd, zero_llm_mode=zero_llm_mode)
        except Exception as e:
            logger.error(f"Error scoring JD '{title}' at '{company}': {e}")
            continue

        # Circuit Breaker check: if Groq hit rate limits, activate Zero-LLM mode for remaining run
        if score_result.get("rate_limit_tripped"):
            consecutive_rate_limits += 1
            if not zero_llm_mode:
                zero_llm_mode = True
                logger.warning("Groq daily rate limit tripped! Circuit breaker ACTIVATED: switching remaining run to Zero-LLM Fallback Mode.")

        score = score_result.get("score", 0)
        best_variant = score_result.get("best_variant", "N/A")
        reasoning = score_result.get("reasoning", "")

        # ── Hard gate: strict score threshold (>= SCORE_VOLUME_FLOOR = 65) ───
        if score < SCORE_VOLUME_FLOOR:
            logger.info(f"Skipping JD: Score {score} below strict threshold {SCORE_VOLUME_FLOOR}.")
            log_result(
                jd=jd,
                score=score,
                variant_used=best_variant,
                status="skipped_low_score",
                pdf_path="",
                notes=reasoning[:200],
            )
            continue

        # ── Zero-LLM path OR Volume tier (if SCORE_VOLUME_FLOOR < SCORE_TAILOR_THRESHOLD) ────
        if zero_llm_mode or score < SCORE_TAILOR_THRESHOLD:
            path_desc = "Zero-LLM fallback path (circuit breaker active)" if zero_llm_mode else "Base-variant path"
            logger.info(f"{path_desc}: Score {score} — applying with prioritized base resume (no LLM tailoring).")
            base_variant = best_variant if (RESUMES_DIR / f"{best_variant}.json").exists() else _keyword_pick_variant(jd)
            try:
                variant_path = RESUMES_DIR / f"{base_variant}.json"
                variant_json = json.loads(variant_path.read_text(encoding="utf-8"))
                variant_json["skills"] = _prioritize_matching_skills(variant_json.get("skills", []), jd)
            except Exception as e:
                logger.error(f"Could not load base variant '{base_variant}': {e}")
                continue

            try:
                pdf_path = render_pdf(variant_json, company)
            except Exception as e:
                logger.error(f"Error rendering base PDF: {e}")
                log_result(jd, score, base_variant, "render_error", "", str(e)[:200])
                continue

            try:
                success, apply_notes = apply(
                    jd,
                    pdf_path,
                    dry_run=dry_run,
                    use_persistent_profile=use_persistent,
                    zero_llm_mode=zero_llm_mode,
                )
                status = classify_apply_status(success, apply_notes, dry_run, "apply_success_base")
            except Exception as e:
                status = "apply_error"
                apply_notes = str(e)
                success = False

            if status in ("apply_success_base", "dry_run_success"):
                norm_c = normalize_company_name(company)
                run_company_applied[norm_c] = run_company_applied.get(norm_c, 0) + 1
                if should_draft_outreach(jd, score):
                    try:
                        create_outreach_draft(
                            jd=jd,
                            variant_json=variant_json,
                            score=score,
                            pdf_path=pdf_path,
                            dry_run=dry_run,
                            zero_llm_mode=True,
                        )
                    except Exception as oe:
                        logger.debug("Non-fatal outreach draft failure for %s: %s", company, oe)

            combined_notes = reasoning[:150]
            if apply_notes:
                combined_notes = (combined_notes + " | apply_err: " + apply_notes)[:400]
            log_result(jd=jd, score=score, variant_used=base_variant,
                       status=status, pdf_path=pdf_path, notes=combined_notes)
            continue

        # ── High-quality path (score >= 65 and not zero_llm_mode): AI-tailor + apply ──────────
        logger.info(f"High-quality path: Score {score} using variant '{best_variant}' — tailoring resume.")

        # 3. Tailor
        logger.info(f"Tailoring resume variant '{best_variant}'...")
        try:
            variant_path = RESUMES_DIR / f"{best_variant}.json"
            variant_json = json.loads(variant_path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.error(f"Could not load base variant '{best_variant}': {e}")
            continue

        tailored = None
        is_tailored = False
        try:
            tailored = tailor_resume(variant_json, jd)
            is_tailored = True
        except DailyRateLimitExceeded as e:
            logger.warning(f"Tailoring hit daily Groq quota for '{title}' at '{company}': {e}. Falling back to base resume & activating Zero-LLM mode.")
            tailored = dict(variant_json)
            tailored["skills"] = _prioritize_matching_skills(tailored.get("skills", []), jd)
            consecutive_rate_limits += 1
            zero_llm_mode = True
        except Exception as e:
            logger.warning(f"Tailoring hit error for '{title}' at '{company}': {e}. Falling back to base resume.")
            tailored = dict(variant_json)
            tailored["skills"] = _prioritize_matching_skills(tailored.get("skills", []), jd)
            consecutive_rate_limits += 1
            if consecutive_rate_limits >= 2:
                zero_llm_mode = True
                logger.warning("Repeated LLM rate limits. Circuit breaker ACTIVATED: switching remaining run to Zero-LLM Fallback Mode.")

        # 4. Render PDF
        logger.info("Rendering tailored PDF...")
        try:
            pdf_path = render_pdf(tailored, company)
            logger.info(f"PDF generated: {pdf_path}")
        except Exception as e:
            logger.error(f"Error rendering PDF for '{title}' at '{company}': {e}")
            log_result(jd, score, best_variant, "render_error", "", str(e)[:200])
            continue

        # 5. Apply
        logger.info("Applying via ATS...")
        apply_notes = ""
        success_status = "apply_success_tailored" if is_tailored else "apply_success_base"
        try:
            cl = tailored.get("cover_letter", "") if isinstance(tailored, dict) else ""
            success, apply_notes = apply(
                jd,
                pdf_path,
                dry_run=dry_run,
                cover_letter=cl,
                use_persistent_profile=use_persistent,
                zero_llm_mode=zero_llm_mode,
            )
            status = classify_apply_status(success, apply_notes, dry_run, success_status)
            if apply_notes and not success:
                logger.warning(f"Apply notes for '{title}' at '{company}': {apply_notes}")
            elif apply_notes:
                logger.info(f"Apply notes for '{title}' at '{company}': {apply_notes}")
            logger.info(f"Application status: {status}")
        except Exception as e:
            logger.error(f"Error applying to '{title}' at '{company}': {e}")
            status = "apply_error"
            apply_notes = str(e)
            success = False

        norm_c = normalize_company_name(company)
        run_company_applied[norm_c] = run_company_applied.get(norm_c, 0) + 1

        if status in ("apply_success_tailored", "apply_success_base", "dry_run_success"):
            if should_draft_outreach(jd, score):
                try:
                    create_outreach_draft(
                        jd=jd,
                        variant_json=tailored,
                        score=score,
                        pdf_path=pdf_path,
                        dry_run=dry_run,
                        zero_llm_mode=zero_llm_mode,
                    )
                except Exception as oe:
                    logger.debug("Non-fatal outreach draft failure for %s: %s", company, oe)

        # 6. Log
        logger.info("Logging result to Google Sheets...")
        try:
            combined_notes = reasoning[:200]
            if apply_notes:
                combined_notes = (combined_notes + " | apply_err: " + apply_notes)[:400]
            log_result(
                jd=jd,
                score=score,
                variant_used=best_variant,
                status=status,
                pdf_path=pdf_path,
                notes=combined_notes,
            )
        except Exception as e:
            logger.error(f"Error logging result: {e}")

        time.sleep(3)  # Brief spacing between applications

    logger.info("Pipeline run complete.")
    
    # 7. Send daily digest
    try:
        send_daily_digest()
    except Exception as e:
        logger.error(f"Failed to send daily digest: {e}")

if __name__ == "__main__":
    main()
