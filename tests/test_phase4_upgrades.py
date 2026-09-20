"""
test_phase4_upgrades.py
========================
Tests for Phase 4: Upgrade 7 (Application Status Tracker)
- Email classification rules (Interview, Assessment/OA, Rejection, Received)
- Safe handling when credentials are unset
- Google Sheet row status update logic
"""

import pytest
from unittest.mock import MagicMock
from scripts.track_applications import (
    classify_email,
    track_gmail_updates,
    update_sheet_status,
)


def test_classify_email_interview():
    # Test phone screen / chat invite
    status, conf = classify_email(
        "Invitation to Interview with Acme AI",
        "Hi Kushagra, we reviewed your application and would like to schedule a time to speak."
    )
    assert status == "interview_scheduled"
    assert conf >= 0.90

    # Test calendly link in body
    status, conf = classify_email(
        "Next steps: Technical chat",
        "Please pick a time on my calendar: https://calendly.com/recruiter/30min"
    )
    assert status == "interview_scheduled"


def test_classify_email_oa():
    # HackerRank test
    status, conf = classify_email(
        "Next step: Acme Engineering Coding Challenge",
        "Please complete the online assessment within 5 days via HackerRank: https://hackerrank.com/test/123"
    )
    assert status == "oa_received"
    assert conf >= 0.90

    # CodeSignal
    status, conf = classify_email(
        "Technical Assessment Request",
        "You have been invited to complete a CodeSignal assessment."
    )
    assert status == "oa_received"


def test_classify_email_rejection():
    status, conf = classify_email(
        "Update on your application at BigCo",
        "Thank you for your interest. Unfortunately, at this time we have decided to pursue other candidates."
    )
    assert status == "rejection"
    assert conf >= 0.80

    status, conf = classify_email(
        "Your application for Frontend Engineer",
        "We regret to inform you that you were not selected for this role."
    )
    assert status == "rejection"


def test_classify_email_application_received():
    status, conf = classify_email(
        "Thank you for applying to Linear",
        "We received your application for Full Stack Engineer. We will review it shortly."
    )
    assert status == "application_received"
    assert conf >= 0.80


def test_classify_email_unrelated():
    status, conf = classify_email(
        "Your weekly newsletter",
        "Here are the top stories of the week in tech."
    )
    assert status is None
    assert conf == 0.0


def test_track_gmail_updates_missing_creds(monkeypatch):
    monkeypatch.delenv("GMAIL_USER", raising=False)
    monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)
    results = track_gmail_updates()
    assert results == []


def test_update_sheet_status_mock(monkeypatch):
    mock_sheet = MagicMock()
    mock_sheet.get_all_records.return_value = [
        {"company": "Acme Corp", "status": "apply_success_tailored", "notes": ""},
        {"company": "Beta Inc", "status": "apply_success_base", "notes": ""},
    ]

    monkeypatch.setattr("scripts.log_and_notify._get_sheet", lambda: mock_sheet)

    # Update Acme Corp
    res = update_sheet_status("Acme Corp", "interview_scheduled", notes="Interview on Monday")
    assert res is True
    # Row 2 (header is row 1, Acme is record 0 -> row 2)
    mock_sheet.update_cell.assert_any_call(2, 8, "interview_scheduled")
    mock_sheet.update_cell.assert_any_call(2, 11, "Interview on Monday")

    # Non-existent company
    res_unknown = update_sheet_status("Unknown Corp", "rejection")
    assert res_unknown is False
