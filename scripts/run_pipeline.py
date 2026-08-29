import os
import sys
import time
import json
import logging
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.fetch_jds import fetch_all
from scripts.score_and_pick import score_and_pick
from scripts.tailor_resume import tailor_resume
from scripts.render_pdf import render_pdf
from scripts.auto_apply_ats import apply
from scripts.log_and_notify import log_result

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("run_pipeline")

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
        
    logger.info(f"Fetched {len(jds)} unique JDs. Processing...")
    
    # Process each JD
    for i, jd in enumerate(jds):
        title = jd.get("title", "Unknown Title")
        company = jd.get("company", "Unknown Company")
        logger.info(f"--- Processing [{i+1}/{len(jds)}]: {title} at {company} ---")
        
        # 1.5 Check ATS compatibility early to save LLM tokens
        apply_url = jd.get("apply_url", "")
        if "greenhouse.io" not in apply_url and "lever.co" not in apply_url and "workday" not in apply_url.lower() and "ashby" not in apply_url.lower():
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
            variant_path = Path("resumes") / f"{best_variant}.json"
            variant_json = json.loads(variant_path.read_text())
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
        try:
            dry_run = os.environ.get("DRY_RUN", "false").lower() == "true"
            success = apply(jd, pdf_path, dry_run=dry_run)
            status = "apply_success" if success else "apply_failed"
            if dry_run and success:
                status = "dry_run_success"
            logger.info(f"Application status: {status}")
        except Exception as e:
            logger.error(f"Error applying to '{title}' at '{company}': {e}")
            status = "apply_error"
            success = False
            
        # 6. Log
        logger.info("Logging result to Google Sheets...")
        try:
            log_result(
                jd=jd,
                score=score,
                variant_used=best_variant,
                status=status,
                pdf_path=pdf_path,
                notes=reasoning[:200]
            )
        except Exception as e:
            logger.error(f"Error logging result: {e}")
            
        # Rate limit protection between heavy LLM ops
        logger.info("Sleeping for 15 seconds to respect Groq rate limits...")
        time.sleep(15)

    logger.info("Pipeline run complete.")

if __name__ == "__main__":
    main()
