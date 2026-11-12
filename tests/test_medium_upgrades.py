from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests

import scripts.fetch_jds as fj
import scripts.track_applications as tracker


# ── fetch_jds.py upgrades ────────────────────────────────────────────────────

def test_is_senior_only_token_isolation():
    # Substrings like 'cto'/'cio' inside normal tech words must NOT reject valid roles
    assert fj._is_senior_only("Vector Database Engineer") is False
    assert fj._is_senior_only("Contractor Software Engineer") is False
    assert fj._is_senior_only("Decision Systems Developer") is False
    assert fj._is_senior_only("Factory Automation QA Engineer") is False

    # Substrings like 'grad'/'intern' inside senior words must NOT override senior gating
    assert fj._is_senior_only("Senior Upgrade Engineer") is True
    assert fj._is_senior_only("Senior Internal Tools Engineer") is True

    # End-of-title 'Lead' and 'Sr.', and Senior Grade I ('Senior Engineer I') must still be rejected
    assert fj._is_senior_only("Frontend Lead") is True
    assert fj._is_senior_only("Tech Lead") is True
    assert fj._is_senior_only("CTO") is True
    assert fj._is_senior_only("Senior Engineer I, Systems Engineering") is True
    assert fj._is_senior_only("Software Engineer I") is False


def test_is_location_ok_indiana_and_foreign_edge_cases():
    # Indiana cities must never match India via ", IN"
    assert fj.is_location_ok("Fort Wayne, IN", False) is False
    assert fj.is_location_ok("Bloomington, IN", True) is False
    assert fj.is_location_ok("Austin, TX (Remote)", True) is False

    # Multi-word & punctuated foreign restrictions must be rejected
    assert fj.is_location_ok("United States (Remote)", True) is False
    assert fj.is_location_ok("Remote (U.S.)", True) is False
    assert fj.is_location_ok("Remote - United Kingdom", True) is False
    assert fj.is_location_ok("Anywhere in USA", True) is False
    assert fj.is_location_ok("Global (United States)", True) is False
    assert fj.is_location_ok("San Francisco (Remote)", True) is False
    assert fj.is_location_ok("Philippines", True) is False
    assert fj.is_location_ok("Thailand", True) is False
    assert fj.is_location_ok("Bosnia and Herzegovina", True) is False
    assert fj.is_location_ok("Remote (EST)", True) is False
    assert fj.is_location_ok("Remote (MENA)", True) is False
    assert fj.is_location_ok("Any State", True) is False

    # India hubs & truly unrestricted remote must pass
    assert fj.is_location_ok("Kochi, Kerala", False) is True
    assert fj.is_location_ok("Pune, Maharashtra", False) is True
    assert fj.is_location_ok("Bengaluru, India (IST)", False) is True
    assert fj.is_location_ok("Remote - Anywhere", True) is True
    assert fj.is_location_ok("Worldwide", True) is True


def test_himalayas_rejects_arbitrary_country_allowlist(monkeypatch):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "jobs": [
            {
                "title": "Junior Backend Engineer",
                "companyName": "BosniaCo",
                "locationRestrictions": ["Bosnia and Herzegovina"],
                "description": "Python APIs",
                "applicationLink": "https://himalayas.app/companies/b/jobs/1",
            },
            {
                "title": "Junior Backend Engineer",
                "companyName": "AndorraCo",
                "locationRestrictions": ["Andorra"],
                "description": "Python APIs",
                "applicationLink": "https://himalayas.app/companies/a/jobs/2",
            },
            {
                "title": "Junior Backend Engineer",
                "companyName": "IndiaCo",
                "locationRestrictions": ["India"],
                "description": "Python APIs",
                "applicationLink": "https://himalayas.app/companies/i/jobs/3",
            },
        ]
    }
    monkeypatch.setattr("requests.get", lambda *a, **kw: resp)
    jobs = fj.fetch_himalayas(limit=20)
    assert [j["company"] for j in jobs] == ["IndiaCo"]


def test_retry_actually_retries_transient_failures_and_logs_permanent_errors(monkeypatch):
    # Skip tenacity wait backoff in unit tests
    monkeypatch.setattr("tenacity.nap.time.sleep", lambda s: None)

    calls = {"count": 0}
    ok_resp = MagicMock()
    ok_resp.status_code = 200
    ok_resp.json.return_value = {
        "jobs": [
            {
                "jobTitle": "Junior Python Engineer",
                "companyName": "RetryCo",
                "jobGeo": "Anywhere",
                "jobDescription": "Build Python APIs.",
                "url": "https://jobicy.com/jobs/123",
            }
        ]
    }

    def flaky_get(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] < 3:
            raise requests.Timeout("transient read timeout")
        return ok_resp

    monkeypatch.setattr("requests.get", flaky_get)
    results = fj.fetch_jobicy()
    assert calls["count"] == 3
    assert len(results) == 1
    assert results[0]["company"] == "RetryCo"

    # Permanent failures should call log_failure after exhausting retries and return []
    failures = []
    monkeypatch.setattr("requests.get", lambda *a, **kw: (_ for _ in ()).throw(requests.ConnectionError("dns fail")))
    monkeypatch.setattr("scripts.log_and_notify.log_failure", lambda src, err: failures.append((src, str(err))))
    assert fj.fetch_arbeitnow() == []
    assert len(failures) == 1
    assert failures[0][0] == "arbeitnow"


def test_remoteok_and_remotive_enforce_location_filter(monkeypatch):
    # RemoteOK must filter out US-only listings even though remote=True
    remoteok_resp = MagicMock()
    remoteok_resp.status_code = 200
    remoteok_resp.json.return_value = [
        {"legal": "notice"},
        {"slug": "1", "position": "Backend Engineer", "company": "USCo", "location": "USA Only", "description": "Python", "apply_url": "https://boards.greenhouse.io/usco/jobs/1"},
        {"slug": "2", "position": "Backend Engineer", "company": "WorldCo", "location": "Worldwide", "description": "Python", "apply_url": "https://boards.greenhouse.io/worldco/jobs/2"},
    ]
    monkeypatch.setattr("requests.get", lambda *a, **kw: remoteok_resp)
    ro_jobs = fj.fetch_remoteok()
    assert [j["company"] for j in ro_jobs] == ["WorldCo"]

    # Remotive must not let "Anywhere in USA" bypass is_location_ok
    remotive_resp = MagicMock()
    remotive_resp.status_code = 200
    remotive_resp.json.return_value = {
        "jobs": [
            {"title": "Frontend Dev", "company_name": "BypassTrap", "candidate_required_location": "Anywhere in USA", "description": "React", "url": "https://remotive.com/jobs/1"},
            {"title": "Frontend Dev", "company_name": "GlobalOk", "candidate_required_location": "Anywhere", "description": "React", "url": "https://remotive.com/jobs/2"},
        ]
    }
    monkeypatch.setattr("requests.get", lambda *a, **kw: remotive_resp)
    rm_jobs = fj.fetch_remotive()
    assert [j["company"] for j in rm_jobs] == ["GlobalOk"]


def test_fetch_hackernews_extracts_first_line_without_company_dump(monkeypatch):
    story_resp = MagicMock()
    story_resp.status_code = 200
    story_resp.json.return_value = {"hits": [{"objectID": "99999"}]}

    comments_resp = MagicMock()
    comments_resp.status_code = 200
    comments_resp.json.return_value = {
        "hits": [
            {
                "comment_text": (
                    "Acme AI (https://acme.example) | Junior Backend Engineer | Remote<p>"
                    "We build distributed inference pipelines with Python and FastAPI.<p>"
                    "Apply here: https://jobs.lever.co/acmeai/11223344-5566-7788-99aa-bbccddeeff00"
                )
            },
            {
                "comment_text": (
                    "GeoLockedCo | Remote (US) | Full-time | https://geolocked.example<p>"
                    "https://boards.greenhouse.io/geolocked/jobs/123456"
                )
            },
            {
                "comment_text": (
                    "UkRoleCo | Remote | https://ukrole.example<p>"
                    "&gt;&gt; Backend Engineer | UK (Remote): https://jobs.ashbyhq.com/ukrole/11223344-5566-7788-99aa-bbccddeeff00"
                )
            },
            {
                "comment_text": (
                    "Just wanted to chime in about how great this company was during my interview loop last year, "
                    "their entire engineering bar is really high and you can see more on "
                    "https://jobs.ashbyhq.com/someco/11223344-5566-7788-99aa-bbccddeeff00 for anyone curious."
                )
            },
        ]
    }

    def hn_router(url, **kwargs):
        return story_resp if "search_by_date" in url else comments_resp

    monkeypatch.setattr("requests.get", hn_router)
    jobs = fj.fetch_hackernews()
    assert len(jobs) == 1
    assert jobs[0]["company"] == "Acme AI"
    assert jobs[0]["title"] == "Junior Backend Engineer"
    assert jobs[0]["remote"] is True
    assert "lever.co/acmeai/" in jobs[0]["apply_url"]


# ── track_applications.py upgrades ───────────────────────────────────────────

def test_classify_email_post_interview_rejection_is_rejection():
    subj = "Update following your technical interview"
    body = (
        "Thank you for taking the time for a technical interview with our team. "
        "Unfortunately, we have decided not to move forward to the next round of interviews "
        "and will pursue other candidates."
    )
    status, conf = tracker.classify_email(subj, body)
    assert status == "rejection"
    assert conf >= 0.80


def test_extract_company_from_email_handles_ats_mailers():
    assert tracker.extract_company_from_email(
        "Acme AI <no-reply@us.greenhouse-mail.io>",
        "Update on your application",
    ) == "Acme AI"

    assert tracker.extract_company_from_email(
        "no-reply@us.greenhouse-mail.io",
        "Thank you for applying to Stripe!",
    ) == "Stripe"

    assert tracker.extract_company_from_email(
        "Vercel Recruiting <no-reply@hire.lever.co>",
        "Application received",
    ) == "Vercel"

    assert tracker.extract_company_from_email(
        "notifications@ashbyhq.com",
        "Next steps with us",
        body="We loved reviewing your profile at Linear.",
        known_companies=["Linear", "Supabase"],
    ) == "Linear"

    assert tracker.extract_company_from_email(
        "Talent Team <jobs@mail.posthog.com>",
        "Application update",
    ) == "Posthog"

    assert tracker.extract_company_from_email(
        "no-reply@ashbyhq.com",
        "Thank you for your interest",
    ) == "Unknown"


def test_update_sheet_status_prefers_active_exact_row_and_blocks_downgrade(monkeypatch):
    mock_sheet = MagicMock()
    mock_sheet.get_all_records.return_value = [
        {"company": "Acme", "status": "skipped_low_score", "notes": "old skip"},       # row 2
        {"company": "Acme", "status": "apply_success_tailored", "notes": "applied"},  # row 3
        {"company": "Acme Labs", "status": "interview_scheduled", "notes": "round 1"},  # row 4
        {"company": "CampusAI", "status": "apply_success_base", "notes": ""},          # row 5
    ]
    monkeypatch.setattr("scripts.log_and_notify._get_sheet", lambda: mock_sheet)

    # Exact active match picks row 3 (not skipped row 2, not Acme Labs row 4)
    assert tracker.update_sheet_status("Acme", "oa_received", notes="OA link") is True
    mock_sheet.update_cell.assert_any_call(3, 8, "oa_received")

    # Older receipt confirmation must not downgrade interview_scheduled on row 4
    assert tracker.update_sheet_status("Acme Labs", "application_received") is False

    # Short 2-char token 'us' must not substring-match 'CampusAI'
    assert tracker.update_sheet_status("Us", "rejection") is False


# ── Workflow CI alignment check ──────────────────────────────────────────────

def test_pipeline_workflow_wires_groq_model_and_tracker():
    wf = Path(".github/workflows/pipeline.yml").read_text(encoding="utf-8")
    assert "GROQ_MODEL: ${{ vars.GROQ_MODEL }}" in wf
    assert "MAX_JOBS_PER_RUN:" in wf
    assert "scripts/track_applications.py" in wf

