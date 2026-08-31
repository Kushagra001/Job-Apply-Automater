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
from scripts.log_and_notify import log_result

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("run_pipeline")

# Fix #1: absolute path — never relative to CWD
RESUMES_DIR = PROJECT_ROOT / "resumes"

# Fix #17: cap JDs processed per run to avoid runaway Groq spend
MAX_JOBS_PER_RUN = int(os.environ.get("MAX_JOBS_PER_RUN", "50"))

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

    # Fix #17: cap per-run volume to control Groq token spend
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
            
        # 2. Score
        logger.info(f"Scoring JD...")
        time.sleep(3) # Small delay before scoring to help with rate limits
        try:
            score_result = score_and_pick(jd)
        except Exception as e:
            logger.error(f"Error scoring JD '{title}' at '{company}': {e}")
            continue
            
        score = score_result.get("score", 0)
        best_variant = score_result.get("best_variant", "N/A")
        reasoning = score_result.get("reasoning", "")
        
        if score < 65:
            logger.info(f"Skipping JD: Score {score} is below threshold 65.")
            log_result(
                jd=jd,
                score=score,
                variant_used=best_variant,
                status="skipped_low_score",
                pdf_path="",
                notes=reasoning[:200]
            )
            time.sleep(5)
            continue
            
        logger.info(f"JD passed with score {score} using variant '{best_variant}'.")
            
        # 3. Tailor
        logger.info(f"Tailoring resume variant '{best_variant}'...")
        try:
            # Fix #1: use absolute RESUMES_DIR, not a CWD-relative path
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
            success, apply_notes = apply(jd, pdf_path, dry_run=dry_run)
            status = "apply_success" if success else "apply_failed"
            if dry_run and success:
                status = "dry_run_success"
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
            
        # Rate limit protection between heavy LLM ops
        logger.info("Sleeping for 15 seconds to respect Groq rate limits...")
        time.sleep(15)

    logger.info("Pipeline run complete.")

if __name__ == "__main__":
    main()
