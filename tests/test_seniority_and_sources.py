import pytest
from scripts.fetch_jds import (
    _is_senior_only,
    _requires_senior_experience,
    is_location_ok,
    fetch_all,
)
from scripts.run_pipeline import _keyword_pick_variant


def test_is_senior_only_filtering():
    # Senior / Staff / Lead roles should be excluded
    assert _is_senior_only("Senior Software Engineer") is True
    assert _is_senior_only("Sr. Full Stack Developer") is True
    assert _is_senior_only("Staff Engineer") is True
    assert _is_senior_only("Principal Architect") is True
    assert _is_senior_only("Lead Backend Engineer") is True
    assert _is_senior_only("SDE-2") is True
    assert _is_senior_only("SDE II") is True
    assert _is_senior_only("Software Engineer 3") is True
    assert _is_senior_only("Engineering Manager") is True
    assert _is_senior_only("VP of Engineering") is True
    assert _is_senior_only("Senior QA Automation Engineer") is True

    # Junior / Entry / Associate roles should NOT be excluded
    assert _is_senior_only("Junior Software Engineer") is False
    assert _is_senior_only("Jr. Developer") is False
    assert _is_senior_only("Associate Software Engineer") is False
    assert _is_senior_only("Associate Solutions Engineer") is False
    assert _is_senior_only("Software Engineer Intern") is False
    assert _is_senior_only("Graduate Engineer Trainee") is False
    assert _is_senior_only("SDE 1") is False
    assert _is_senior_only("SDE-1") is False
    assert _is_senior_only("Early Career Software Engineer") is False
    assert _is_senior_only("Software Engineer (0-2 years)") is False

    # Neutral roles (standard titles without senior signals) should NOT be excluded
    assert _is_senior_only("Software Engineer") is False
    assert _is_senior_only("Frontend Developer") is False
    assert _is_senior_only("QA Engineer") is False
    assert _is_senior_only("Solutions Engineer") is False


def test_requires_senior_experience():
    # Explicit requirements for 3+ years should be flagged as senior
    assert _requires_senior_experience("Requires at least 3 years of experience in Python.") is True
    assert _requires_senior_experience("Minimum 5+ years of experience with React and TypeScript.") is True
    assert _requires_senior_experience("Must have 4 yrs experience in cloud infrastructure.") is True
    assert _requires_senior_experience("Experience required: 3+ years in software testing.") is True
    assert _requires_senior_experience("Candidates with 7+ years of exp preferred.") is True

    # 0-2 years, freshers, or no mention should NOT be flagged
    assert _requires_senior_experience("Ideal candidate has 0-2 years of experience or is a new grad.") is False
    assert _requires_senior_experience("Requires 1-2 years of experience with REST APIs.") is False
    assert _requires_senior_experience("Open to fresh graduates with strong problem solving skills.") is False
    assert _requires_senior_experience("We are looking for a motivated developer to join our team.") is False
    assert _requires_senior_experience("") is False


def test_is_location_ok():
    # India locations
    assert is_location_ok("Bengaluru, India", False) is True
    assert is_location_ok("Jaipur, Rajasthan", False) is True
    assert is_location_ok("Remote - India", True) is True

    # Global / Unrestricted Remote
    assert is_location_ok("Remote", True) is True
    assert is_location_ok("Worldwide", True) is True
    assert is_location_ok("Anywhere", True) is True
    assert is_location_ok("Global", True) is True
    assert is_location_ok("Remote - Anywhere", True) is True

    # Foreign restricted remote roles should be rejected
    assert is_location_ok("Remote, US Only", True) is False
    assert is_location_ok("Remote - US", True) is False
    assert is_location_ok("Remote (USA)", True) is False
    assert is_location_ok("Remote, EMEA", True) is False
    assert is_location_ok("Remote (UK Only)", True) is False
    assert is_location_ok("Remote (Germany Only)", True) is False
    assert is_location_ok("Malaysia", True) is False
    assert is_location_ok("Canada, USA", True) is False

    # Foreign onsite should be rejected
    assert is_location_ok("San Francisco, CA", False) is False
    assert is_location_ok("Berlin, Germany", False) is False


def test_keyword_pick_variant_expansion():
    # QA / SDET
    assert _keyword_pick_variant({"title": "QA Automation Engineer", "description": "Playwright testing"}) == "sdet-qa"
    assert _keyword_pick_variant({"title": "SDET 1", "description": "Python pytest automation"}) == "sdet-qa"
    assert _keyword_pick_variant({"title": "Software Tester", "description": "API testing"}) == "sdet-qa"

    # Solutions / Support
    assert _keyword_pick_variant({"title": "Solutions Engineer", "description": "REST APIs and webhooks"}) == "solutions-support"
    assert _keyword_pick_variant({"title": "Developer Support Engineer", "description": "Debugging API calls"}) == "solutions-support"
    assert _keyword_pick_variant({"title": "Technical Integration Engineer", "description": "Client integration"}) == "solutions-support"

    # Frontend
    assert _keyword_pick_variant({"title": "Frontend Engineer", "description": "React and Tailwind"}) == "frontend"

    # AI Engineer
    assert _keyword_pick_variant({"title": "AI Engineer", "description": "LLMs and RAG pipelines"}) == "ai-engineer"

    # Backend
    assert _keyword_pick_variant({"title": "Python Developer", "description": "FastAPI microservices"}) == "backend-python"
    assert _keyword_pick_variant({"title": "Backend Node Engineer", "description": "Express and PostgreSQL"}) == "backend-node"


def test_fetch_all_remote_first_ordering(monkeypatch):
    # Mock individual fetchers to return mock listings
    monkeypatch.setattr("scripts.fetch_jds.fetch_ashby", lambda: [
        {"title": "Onsite Eng", "company": "Co A", "remote": False, "apply_url": "https://jobs.ashbyhq.com/coa/12345678-1234-1234-1234-123456789012", "source": "ashby"},
        {"title": "Remote Eng 1", "company": "Co B", "remote": True, "apply_url": "https://jobs.ashbyhq.com/cob/22345678-1234-1234-1234-123456789012", "source": "ashby"},
    ])
    monkeypatch.setattr("scripts.fetch_jds.fetch_lever", lambda: [
        {"title": "Remote Eng 2", "company": "Co C", "remote": True, "apply_url": "https://jobs.lever.co/coc/32345678-1234-1234-1234-123456789012", "source": "lever"},
        {"title": "Onsite Eng 2", "company": "Co D", "remote": False, "apply_url": "https://jobs.lever.co/cod/42345678-1234-1234-1234-123456789012", "source": "lever"},
    ])
    monkeypatch.setattr("scripts.fetch_jds.fetch_greenhouse", lambda: [])
    monkeypatch.setattr("scripts.fetch_jds.fetch_remoteok", lambda: [])
    monkeypatch.setattr("scripts.fetch_jds.fetch_remotive", lambda: [])
    monkeypatch.setattr("scripts.fetch_jds.fetch_jobicy", lambda: [])
    monkeypatch.setattr("scripts.fetch_jds.fetch_arbeitnow", lambda: [])
    monkeypatch.setattr("scripts.fetch_jds.fetch_hackernews", lambda: [])

    # Mock get_processed_urls to avoid network call to Google Sheets
    monkeypatch.setattr("scripts.log_and_notify.get_processed_urls", lambda: set())

    results = fetch_all()
    assert len(results) == 4
    # All remote jobs must come before onsite jobs
    remote_flags = [r["remote"] for r in results]
    assert remote_flags == [True, True, False, False]
