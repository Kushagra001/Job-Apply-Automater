import pytest
from unittest.mock import MagicMock
from scripts.auto_apply_ats import (
    _human_type,
    _is_job_closed,
    _detect_captcha,
    detect_ats,
    _ATS_DOMAIN_MAP,
)

def test_human_type_ignores_checkbox_and_non_text_inputs():
    """Verify that _human_type gracefully skips checkboxes, radios, and buttons rather than crashing."""
    mock_checkbox = MagicMock()
    mock_checkbox.get_attribute.return_value = "checkbox"
    
    # Should safely return without calling clear or press_sequentially
    _human_type(mock_checkbox, "https://linkedin.com/in/test")
    mock_checkbox.clear.assert_not_called()
    mock_checkbox.fill.assert_not_called()

    mock_radio = MagicMock()
    mock_radio.get_attribute.return_value = "radio"
    _human_type(mock_radio, "text")
    mock_radio.clear.assert_not_called()


def test_is_job_closed_detection():
    """Verify that _is_job_closed detects expired, 404, or closed postings."""
    # 1. 404 title
    page_404 = MagicMock()
    page_404.title.return_value = "Jobs - 404 Page Not Found"
    closed, reason = _is_job_closed(page_404)
    assert closed is True
    assert "404" in reason

    # 2. Closed body text
    page_closed = MagicMock()
    page_closed.title.return_value = "Software Engineer @ PostHog"
    body_mock = MagicMock()
    body_mock.count.return_value = 1
    body_mock.inner_text.return_value = "Thank you for visiting. The page you requested was not found or is no longer accepting applications."
    page_closed.locator.return_value = body_mock
    
    closed, reason = _is_job_closed(page_closed)
    assert closed is True
    assert "page you requested was not found" in reason or "no longer accepting applications" in reason

    # 3. Active open job
    page_active = MagicMock()
    page_active.title.return_value = "Senior Software Engineer @ Sentry"
    active_body = MagicMock()
    active_body.count.return_value = 1
    active_body.inner_text.return_value = "About the role: We are looking for an engineer to join our core team. Please submit your application below."
    page_active.locator.return_value = active_body

    closed, _ = _is_job_closed(page_active)
    assert closed is False


def test_early_captcha_detection_by_title():
    """Verify that Cloudflare Turnstile 'Just a moment' challenge is detected instantly via title."""
    page_cf = MagicMock()
    page_cf.title.return_value = "Just a moment..."
    assert _detect_captcha(page_cf) is True

    page_normal = MagicMock()
    page_normal.title.return_value = "Careers @ Supabase"
    loc_mock = MagicMock()
    loc_mock.count.return_value = 0
    page_normal.locator.return_value = loc_mock
    assert _detect_captcha(page_normal) is False


def test_detect_ats_domain_expansion():
    """Verify that arbeitnow.co.uk and all supported ATS domains resolve correctly."""
    assert detect_ats("https://www.arbeitnow.co.uk/jobs/123") == "arbeitnow"
    assert detect_ats("https://www.arbeitnow.com/jobs/456") == "arbeitnow"
    assert detect_ats("https://jobs.ashbyhq.com/company/123") == "ashby"
    assert detect_ats("https://jobs.lever.co/company/123") == "lever"
    assert detect_ats("https://boards.greenhouse.io/company/jobs/123") == "greenhouse"
