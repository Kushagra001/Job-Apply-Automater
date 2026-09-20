"""
cleanup_sheet.py — Clean Up Corrupted & Redundant Data in Google Sheet
======================================================================
Cleans up:
1. Invalid board-only URLs (e.g. general Ashby/Greenhouse board indexes instead of specific postings).
2. Corrupted text fields (e.g. multi-paragraph comment text mistakenly stored in 'company').
3. Duplicate application rows (keeps the highest-priority status: success > dry_run > fail > skip,
   and most recent timestamp).
4. Unicode non-breaking hyphens / formatting anomalies in text fields.
"""

from __future__ import annotations

import collections
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.log_and_notify import _get_sheet, SHEET_COLUMNS
from scripts.auto_apply_ats import validate_ats_job_url

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

STATUS_PRIORITY = {
    "interview_scheduled": 12,
    "oa_received": 11,
    "apply_success_tailored": 10,
    "apply_success_base": 9,
    "apply_success": 8,
    "dry_run_success": 7,
    "application_received": 6,
    "rejection": 5,
    "apply_failed": 3,
    "skipped_low_score": 2,
    "skipped_unsupported_ats": 1,
    "render_error": 0,
}


def clean_google_sheet(dry_run: bool = False) -> dict:
    sheet = _get_sheet()
    all_values = sheet.get_all_values()

    if not all_values:
        logger.warning("Sheet is empty.")
        return {"status": "empty"}

    header = all_values[0]
    data_rows = all_values[1:]

    logger.info("Total rows before cleanup: %d (Data rows: %d)", len(all_values), len(data_rows))

    removed_invalid_urls = []
    cleaned_corrupted_fields = []
    seen_urls: dict[str, tuple[int, dict]] = {}

    for idx, r in enumerate(data_rows, start=2):
        row_dict = dict(zip(header, r))
        url = row_dict.get("apply_url", "").strip()
        company = row_dict.get("company", "").strip()
        title = row_dict.get("title", "").strip()
        status = row_dict.get("status", "").strip()
        notes = row_dict.get("notes", "").strip()

        # 1. Filter out invalid board-only URLs
        is_valid, reason = validate_ats_job_url(url)
        if not is_valid:
            removed_invalid_urls.append({"row": idx, "url": url, "reason": reason, "status": status})
            continue

        # 2. Fix corrupted fields (e.g. whole text dump in company)
        if len(company) > 60 or "\n" in company or "DuckDuckGo" in company:
            if "DuckDuckGo" in company:
                company = "DuckDuckGo"
                if len(title) > 60:
                    title = "Senior Web Security Engineer"
                cleaned_corrupted_fields.append({"row": idx, "field": "company", "fixed": "DuckDuckGo"})
            elif "Langfuse" in company:
                company = "Langfuse"
                title = "Software Engineer"
                cleaned_corrupted_fields.append({"row": idx, "field": "company", "fixed": "Langfuse"})

        # 3. Clean unicode non-breaking hyphens and typographical dashes
        clean_notes = notes.replace("\u2011", "-").replace("\u2013", "-").replace("\u2014", "-")
        clean_company = company.replace("\u2011", "-").replace("\u2013", "-").replace("\u2014", "-")
        clean_title = title.replace("\u2011", "-").replace("\u2013", "-").replace("\u2014", "-")

        row_dict["company"] = clean_company
        row_dict["title"] = clean_title
        row_dict["notes"] = clean_notes

        # 4. Deduplicate by URL
        if url in seen_urls:
            existing_idx, existing_row = seen_urls[url]
            existing_prio = STATUS_PRIORITY.get(existing_row.get("status", ""), 0)
            curr_prio = STATUS_PRIORITY.get(status, 0)

            if curr_prio > existing_prio:
                seen_urls[url] = (idx, row_dict)
            elif curr_prio == existing_prio:
                if row_dict.get("timestamp", "") > existing_row.get("timestamp", ""):
                    seen_urls[url] = (idx, row_dict)
        else:
            seen_urls[url] = (idx, row_dict)

    final_data = list(seen_urls.values())
    final_data.sort(key=lambda x: x[1].get("timestamp", ""))

    duplicates_removed_count = len(data_rows) - len(removed_invalid_urls) - len(final_data)

    logger.info("Summary of Cleanup Plan:")
    logger.info("  - Invalid board URLs removed: %d", len(removed_invalid_urls))
    logger.info("  - Corrupted text fields fixed: %d", len(cleaned_corrupted_fields))
    logger.info("  - Duplicate rows removed: %d", duplicates_removed_count)
    logger.info("  - Clean data rows remaining: %d (Total with header: %d)", len(final_data), len(final_data) + 1)

    if dry_run:
        logger.info("[DRY RUN] No changes written to Google Sheet.")
        return {
            "initial_rows": len(all_values),
            "final_rows": len(final_data) + 1,
            "removed_invalid_urls": len(removed_invalid_urls),
            "duplicates_removed": duplicates_removed_count,
            "corrupted_fixed": len(cleaned_corrupted_fields),
        }

    # Backup locally before writing
    backup_file = PROJECT_ROOT / "scratch" / f"sheet_backup_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
    backup_file.parent.mkdir(parents=True, exist_ok=True)
    with open(backup_file, "w", encoding="utf-8") as f:
        json.dump(all_values, f, ensure_ascii=False, indent=2)
    logger.info("Created local pre-cleanup backup at %s", backup_file)

    # Format rows for batch update
    new_matrix = [header]
    for _, rd in final_data:
        new_matrix.append([str(rd.get(col, "")) for col in header])

    # Clear and update sheet in batch
    sheet.clear()
    sheet.update(range_name="A1", values=new_matrix)
    logger.info("Successfully updated Google Sheet! Final row count: %d", len(new_matrix))

    return {
        "initial_rows": len(all_values),
        "final_rows": len(new_matrix),
        "removed_invalid_urls": len(removed_invalid_urls),
        "duplicates_removed": duplicates_removed_count,
        "corrupted_fixed": len(cleaned_corrupted_fields),
        "backup_path": str(backup_file),
    }


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Clean up corrupted and duplicate data from Google Sheet")
    parser.add_argument("--dry-run", action="store_true", help="Simulate without modifying sheet")
    args = parser.parse_args()

    clean_google_sheet(dry_run=args.dry_run)
