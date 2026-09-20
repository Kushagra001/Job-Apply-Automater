"""
test_cleanup_sheet.py
=====================
Tests for Google Sheet data sanitization & deduplication script.
"""

from unittest.mock import MagicMock
from scripts.cleanup_sheet import clean_google_sheet


def test_clean_google_sheet_logic(monkeypatch):
    mock_header = ["run_id", "timestamp", "company", "title", "source", "score", "variant_used", "status", "pdf_path", "apply_url", "notes"]
    mock_values = [
        mock_header,
        # Valid row 1
        ["id-1", "2026-09-01T10:00:00Z", "Acme", "Software Engineer", "greenhouse", "80", "fullstack", "apply_success", "pdf1", "https://job-boards.greenhouse.io/acme/jobs/123456", "Good match"],
        # Duplicate of row 1 but with lower priority status (should be dropped)
        ["id-2", "2026-09-01T09:00:00Z", "Acme", "Software Engineer", "greenhouse", "80", "fullstack", "skipped_low_score", "", "https://job-boards.greenhouse.io/acme/jobs/123456", "Duplicate"],
        # Invalid board URL (should be dropped)
        ["id-3", "2026-09-01T11:00:00Z", "Langfuse", "Software Engineer", "ashby", "85", "fullstack", "render_error", "", "https://jobs.ashbyhq.com/langfuse", "File name too long"],
        # Row with corrupted company text dump
        ["id-4", "2026-09-01T12:00:00Z", "DuckDuckGo - long text dump here...", "Dev", "hackernews", "70", "backend-python", "skipped_low_score", "", "https://jobs.ashbyhq.com/duck-duck-go/11223344-5566-7788-99aa-bbccddeeff00", "Notes with \u2011 hyphen"],
    ]

    mock_sheet = MagicMock()
    mock_sheet.get_all_values.return_value = mock_values
    monkeypatch.setattr("scripts.cleanup_sheet._get_sheet", lambda: mock_sheet)

    res = clean_google_sheet(dry_run=True)
    assert res["initial_rows"] == 5
    assert res["removed_invalid_urls"] == 1  # Langfuse board url dropped
    assert res["duplicates_removed"] == 1    # Duplicate dropped
    assert res["corrupted_fixed"] == 1       # DuckDuckGo text fixed
    assert res["final_rows"] == 3            # Header + 2 clean rows
