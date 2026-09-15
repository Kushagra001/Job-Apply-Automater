import os
import sys
import time
import json
import logging
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.fetch_jds import fetch_all
from scripts.score_and_pick import score_and_pick
from scripts.tailor_resume import tailor_resume
from scripts.render_pdf import render_pdf
from scripts.auto_apply_ats import apply
from scripts.log_and_notify import log_result, send_daily_digest

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("run_pipeline")

# Fix #1: absolute path — never relative to CWD
RESUMES_DIR = PROJECT_ROOT / "resumes"

# Fix #17: cap JDs processed per run to avoid runaway Groq spend
MAX_JOBS_PER_RUN = int(os.environ.get("MAX_JOBS_PER_RUN", "200"))

# Tiered score thresholds
SCORE_TAILOR_THRESHOLD = 55   # >= this: AI-tailor resume then apply
SCORE_VOLUME_FLOOR    = 20   # >= this: apply with best base variant (no tailoring)
# Score < SCORE_VOLUME_FLOOR → skip entirely


RESUMES_DIR = PROJECT_ROOT / "resumes"


def _keyword_pick_variant(jd: dict) -> str:
    """
    Fast, zero-LLM variant picker for the low-score volume path.
    Matches job title keywords to the most suitable base resume variant.
    """
    title = (jd.get("title", "") + " " + jd.get("description", "")[:200]).lower()
    if any(k in title for k in ["frontend", "front-end", "react", "vue", "angular", "next.js", "ui engineer", "css"]):
        return "frontend"
    if any(k in title for k in ["python", "django", "fastapi", "flask", "data engineer", "ml ", "machine learning"]):
        return "backend-python"
    if any(k in title for k in ["ai engineer", "llm", "genai", "mlops", "generative ai"]):
        return "ai-engineer"
    if any(k in title for k in ["node", "typescript", "express", "nestjs", "nest.js", "backend"]):
        return "backend-node"
    return "backend-node"  # Safe default

def main():
    logger.info("Starting automated job application pipeline...")
    
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

    if len(jds) > MAX_JOBS_PER_RUN:
        logger.info(f"Capping run to {MAX_JOBS_PER_RUN} JDs (fetched {len(jds)}). Set MAX_JOBS_PER_RUN env var to change.")
        jds = jds[:MAX_JOBS_PER_RUN]

    logger.info(f"Fetched {len(jds)} unique JDs. Processing...")

    
    # Process each JD
    for i, jd in enumerate(jds):
        title = jd.get("title", "Unknown Title")
        company = jd.get("company", "Unknown Company")
        logger.info(f"--- Processing [{i+1}/{len(jds)}]: {title} at {company} ---")
        
        # Gate: only advance jobs we can actually apply to
        apply_url = jd.get("apply_url", "")
        source = jd.get("source", "")
        _SUPPORTED_DOMAINS = (
            "greenhouse.io", "lever.co", "ashbyhq.com",
            "gh_jid=", # For custom greenhouse domains like stripe.com
        )
        # RemoteOK and Remotive have their own apply flows, always pass them through
        is_aggregator = source in ("remoteok", "remotive")
        if not is_aggregator and not any(domain in apply_url for domain in _SUPPORTED_DOMAINS):
            logger.warning(f"Unsupported ATS or apply URL for '{title}' at '{company}': {apply_url}")
            log_result(
                jd=jd,
                score=0,
                variant_used="N/A",
                status="skipped_unsupported_ats",
                pdf_path="",
                notes="Skipped before scoring due to unsupported ATS."
            )
            continue
            
        # 2. Score with Groq (one fast call to get a number)
        logger.info(f"Scoring JD...")
        time.sleep(2)
        try:
            score_result = score_and_pick(jd)
        except Exception as e:
            logger.error(f"Error scoring JD '{title}' at '{company}': {e}")
            continue

        score = score_result.get("score", 0)
        best_variant = score_result.get("best_variant", "N/A")
        reasoning = score_result.get("reasoning", "")

        # ── Hard floor: completely irrelevant ────────────────────────────
        if score < SCORE_VOLUME_FLOOR:
            logger.info(f"Skipping JD: Score {score} below hard floor {SCORE_VOLUME_FLOOR}.")
            log_result(
                jd=jd,
                score=score,
                variant_used=best_variant,
                status="skipped_low_score",
                pdf_path="",
                notes=reasoning[:200],
            )
            time.sleep(2)
            continue

        # ── Volume path (20 ≤ score < 55): base resume, no tailoring ────
        if score < SCORE_TAILOR_THRESHOLD:
            logger.info(f"Volume path: Score {score} — applying with base resume (no tailoring).")
            base_variant = _keyword_pick_variant(jd)
            try:
                variant_path = RESUMES_DIR / f"{base_variant}.json"
                variant_json = json.loads(variant_path.read_text(encoding="utf-8"))
            except Exception as e:
                logger.error(f"Could not load base variant '{base_variant}': {e}")
                continue

            try:
                pdf_path = render_pdf(variant_json, company)
            except Exception as e:
                logger.error(f"Error rendering base PDF: {e}")
                log_result(jd, score, base_variant, "render_error", "", str(e)[:200])
                continue

            dry_run = os.environ.get("DRY_RUN", "false").lower() == "true"
            try:
                success, apply_notes = apply(jd, pdf_path, dry_run=dry_run)
                if dry_run and success:
                    status = "dry_run_success"
                else:
                    status = "apply_success_base" if success else "apply_failed"
            except Exception as e:
                status = "apply_error"
                apply_notes = str(e)
                success = False

            combined_notes = reasoning[:150]
            if apply_notes:
                combined_notes = (combined_notes + " | apply_err: " + apply_notes)[:400]
            log_result(jd=jd, score=score, variant_used=base_variant,
                       status=status, pdf_path=pdf_path, notes=combined_notes)
            time.sleep(2)   # Fast path — no Groq, minimal wait
            continue

        # ── High-quality path (score >= 55): AI-tailor + apply ──────────
        logger.info(f"High-quality path: Score {score} using variant '{best_variant}' — tailoring resume.")

        # 3. Tailor
        logger.info(f"Tailoring resume variant '{best_variant}'...")
        try:
            variant_path = RESUMES_DIR / f"{best_variant}.json"
            variant_json = json.loads(variant_path.read_text(encoding="utf-8"))
            tailored = tailor_resume(variant_json, jd)
        except Exception as e:
            logger.error(f"Error tailoring resume for '{title}' at '{company}': {e}")
            log_result(jd, score, best_variant, "tailor_error", "", str(e)[:200])
            time.sleep(5)
            continue

        # 4. Render PDF
        logger.info("Rendering tailored PDF...")
        try:
            pdf_path = render_pdf(tailored, company)
            logger.info(f"PDF generated: {pdf_path}")
        except Exception as e:
            logger.error(f"Error rendering PDF for '{title}' at '{company}': {e}")
            log_result(jd, score, best_variant, "render_error", "", str(e)[:200])
            time.sleep(5)
            continue

        # 5. Apply
        logger.info("Applying via ATS...")
        apply_notes = ""
        try:
            dry_run = os.environ.get("DRY_RUN", "false").lower() == "true"
            cl = tailored.get("cover_letter", "")
            success, apply_notes = apply(jd, pdf_path, dry_run=dry_run, cover_letter=cl)
            if dry_run and success:
                status = "dry_run_success"
            else:
                status = "apply_success_tailored" if success else "apply_failed"
            if apply_notes:
                logger.warning(f"Apply notes for '{title}' at '{company}': {apply_notes}")
            logger.info(f"Application status: {status}")
        except Exception as e:
            logger.error(f"Error applying to '{title}' at '{company}': {e}")
            status = "apply_error"
            apply_notes = str(e)
            success = False

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

        logger.info("Sleeping 10 seconds (high-quality path rate limiting)...")
        time.sleep(10)

    logger.info("Pipeline run complete.")
    
    # 7. Send daily digest
    try:
        send_daily_digest()
    except Exception as e:
        logger.error(f"Failed to send daily digest: {e}")

if __name__ == "__main__":
    main()
