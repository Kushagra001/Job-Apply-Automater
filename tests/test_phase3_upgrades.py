"""
test_phase3_upgrades.py
========================
Tests for Phase 3 Sourcing Upgrades:
- Upgrade 4: Himalayas Remote Jobs Fetcher
- Upgrade 5: SimplifyJobs Live New-Grad Feed with ATS Validation
- Upgrade 6: Y-Combinator Startup Discovery & Aggregation
- Enhanced Geo-fencing & Anti-preposition location filtering
"""

import pytest
from unittest.mock import MagicMock
from scripts.fetch_jds import (
    fetch_himalayas,
    fetch_simplify_jobs,
    fetch_yc_jobs,
    is_location_ok,
)


def test_location_ok_preposition_and_state_safeguards():
    # Prepositions like "Remote in USA" or "Remote in UK" must be rejected by foreign geo-fence
    assert is_location_ok("Remote in USA", True) is False
    assert is_location_ok("Remote in UK", True) is False
    assert is_location_ok("Remote in Germany", True) is False

    # US State Indiana / Indianapolis must not match India country code
    assert is_location_ok("Indiana, USA", False) is False
    assert is_location_ok("Indianapolis, IN", False) is False

    # Genuine India & Global Remote must be accepted
    assert is_location_ok("Bengaluru, India", False) is True
    assert is_location_ok("Jaipur, Rajasthan", False) is True
    assert is_location_ok("Remote - India", True) is True
    assert is_location_ok("Remote", True) is True
    assert is_location_ok("Worldwide", True) is True
    assert is_location_ok("Anywhere", True) is True


def test_himalayas_parsing_and_filtering(monkeypatch):
    mock_payload = {
        "jobs": [
            {
                "title": "Junior Full Stack Engineer",
                "companyName": "Acme AI",
                "locationRestrictions": [],  # Worldwide
                "description": "Build modern React and FastAPI features.",
                "applicationLink": "https://himalayas.app/companies/acme-ai/jobs/junior-full-stack-engineer",
            },
            {
                "title": "Principal Infrastructure Architect",  # Senior only -> rejected
                "companyName": "BigCo",
                "locationRestrictions": [],
                "description": "Lead multi-cloud architecture.",
                "applicationLink": "https://himalayas.app/companies/bigco/jobs/principal-architect",
            },
            {
                "title": "Junior Python Developer",
                "companyName": "US Only Corp",
                "locationRestrictions": ["United States"],  # Foreign restricted -> rejected
                "description": "Python microservices.",
                "applicationLink": "https://himalayas.app/companies/us-only/jobs/junior-dev",
            },
            {
                "title": "Customer Support Representative",  # Non-tech -> rejected
                "companyName": "SupportCo",
                "locationRestrictions": [],
                "description": "Answer phone calls.",
                "applicationLink": "https://himalayas.app/companies/supportco/jobs/csr",
            },
        ]
    }

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = mock_payload
    monkeypatch.setattr("requests.get", lambda *args, **kwargs: mock_resp)

    results = fetch_himalayas(limit=20)
    assert len(results) == 1
    job = results[0]
    assert job["title"] == "Junior Full Stack Engineer"
    assert job["company"] == "Acme AI"
    assert job["remote"] is True
    assert job["source"] == "himalayas"
    assert "applicationLink" not in job  # Canonical schema uses apply_url
    assert job["apply_url"] == "https://himalayas.app/companies/acme-ai/jobs/junior-full-stack-engineer"


def test_simplify_jobs_parsing_and_ats_validation(monkeypatch):
    mock_payload = [
        {
            "active": True,
            "is_visible": True,
            "title": "Software Engineer - Early Career",
            "company_name": "Modern Corp",
            "locations": ["Remote"],
            "url": "https://jobs.ashbyhq.com/moderncorp/11223344-5566-7788-99aa-bbccddeeff00/application",
            "category": "Software Engineering",
        },
        {
            "active": False,  # Inactive -> rejected
            "is_visible": True,
            "title": "Junior Software Engineer",
            "company_name": "Old Corp",
            "locations": ["Remote"],
            "url": "https://jobs.ashbyhq.com/oldcorp/11223344-5566-7788-99aa-bbccddeeff00",
        },
        {
            "active": True,
            "is_visible": True,
            "title": "Associate Backend Engineer",
            "company_name": "Generic Portal",
            "locations": ["Remote"],
            "url": "https://jobs.ashbyhq.com/genericportal",  # Index board URL without job ID -> rejected by validate_ats_job_url
        },
        {
            "active": True,
            "is_visible": True,
            "title": "Software Engineer - New Grad",
            "company_name": "US Restricted Corp",
            "locations": ["Remote in USA"],  # US only -> rejected
            "url": "https://jobs.lever.co/uscorp/11223344-5566-7788-99aa-bbccddeeff00",
        },
    ]

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = mock_payload
    monkeypatch.setattr("requests.get", lambda *args, **kwargs: mock_resp)

    results = fetch_simplify_jobs()
    assert len(results) == 1
    job = results[0]
    assert job["title"] == "Software Engineer - Early Career"
    assert job["company"] == "Modern Corp"
    assert job["source"] == "simplify"
    assert job["remote"] is True
    assert "jobs.ashbyhq.com" in job["apply_url"]


def test_yc_jobs_discovery(monkeypatch):
    monkeypatch.setattr(
        "scripts.fetch_jds._fetch_single_ashby",
        lambda slug: [
            {
                "title": f"Junior Engineer at {slug}",
                "company": slug.title(),
                "location": "Remote",
                "remote": True,
                "description": "Tech work",
                "apply_url": f"https://jobs.ashbyhq.com/{slug}/11223344-5566-7788-99aa-bbccddeeff00",
                "source": "ashby",
                "fetched_at": "2026-09-20T00:00:00Z",
            }
        ] if slug == "resend" else []
    )
    monkeypatch.setattr(
        "scripts.fetch_jds._fetch_single_greenhouse",
        lambda slug: [
            {
                "title": f"Associate SDE at {slug}",
                "company": slug.title(),
                "location": "Remote",
                "remote": True,
                "description": "Tech work",
                "apply_url": f"https://boards.greenhouse.io/{slug}/jobs/12345678",
                "source": "greenhouse",
                "fetched_at": "2026-09-20T00:00:00Z",
            }
        ] if slug == "posthog" else []
    )

    results = fetch_yc_jobs(slugs=["resend", "posthog", "unknown"])
    assert len(results) == 2
    sources = {r["source"] for r in results}
    # All YC discovery jobs must be normalized to source="yc_startups"
    assert sources == {"yc_startups"}
    companies = {r["company"] for r in results}
    assert companies == {"Resend", "Posthog"}
