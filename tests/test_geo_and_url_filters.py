import pytest
from scripts.fetch_jds import is_location_ok
from scripts.auto_apply_ats import validate_ats_job_url


def test_is_location_ok_india():
    assert is_location_ok("Bengaluru, India", False) is True
    assert is_location_ok("Jaipur, Rajasthan", False) is True
    assert is_location_ok("Remote, India", True) is True


def test_is_location_ok_worldwide_remote():
    assert is_location_ok("Remote", True) is True
    assert is_location_ok("Worldwide", True) is True
    assert is_location_ok("Anywhere", True) is True
    assert is_location_ok("Global", True) is True


def test_is_location_ok_geo_restricted_rejection():
    # US / Americas restricted
    assert is_location_ok("Remote, US Only", True) is False
    assert is_location_ok("Remote (USA Only)", True) is False
    assert is_location_ok("Remote, AMER", True) is False
    assert is_location_ok("Remote - North America", True) is False

    # Europe / UK restricted
    assert is_location_ok("Remote, EMEA", True) is False
    assert is_location_ok("Remote, UK Only", True) is False
    assert is_location_ok("Remote (Germany Only)", True) is False

    # Non-remote outside India
    assert is_location_ok("San Francisco, CA", False) is False
    assert is_location_ok("London, UK", False) is False


def test_validate_ats_job_url_ashby():
    # Valid Ashby job posts (must contain company + UUID)
    assert validate_ats_job_url("https://jobs.ashbyhq.com/supabase/620bcab3-787c-4ffe-bd37-5d45e209925e")[0] is True
    assert validate_ats_job_url("https://jobs.ashbyhq.com/linear/0c7c2e26-0a98-42cf-a47c-9a3999fb513b/application")[0] is True

    # Invalid Ashby company board indexes
    valid, reason = validate_ats_job_url("https://jobs.ashbyhq.com/sentilink")
    assert valid is False
    assert "board index" in reason

    valid, reason = validate_ats_job_url("https://jobs.ashbyhq.com/sentilink?utm_source=hn")
    assert valid is False


def test_validate_ats_job_url_lever():
    # Valid Lever job posting
    assert validate_ats_job_url("https://jobs.lever.co/meesho/12345678-abcd-1234-5678-1234567890ab")[0] is True

    # Invalid Lever company board index
    valid, reason = validate_ats_job_url("https://jobs.lever.co/meesho")
    assert valid is False


def test_validate_ats_job_url_greenhouse():
    # Valid Greenhouse job posting
    assert validate_ats_job_url("https://boards.greenhouse.io/vercel/jobs/123456")[0] is True
    assert validate_ats_job_url("https://job-boards.greenhouse.io/vercel/jobs/5818258004")[0] is True
    assert validate_ats_job_url("https://stripe.com/jobs?gh_jid=12345")[0] is True

    # Invalid Greenhouse company board index
    valid, reason = validate_ats_job_url("https://boards.greenhouse.io/vercel")
    assert valid is False
