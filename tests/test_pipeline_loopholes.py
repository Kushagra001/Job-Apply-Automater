import pytest
from scripts.fetch_jds import _is_tech_job, _is_senior_only
from scripts.run_pipeline import _is_viable_apply_url
from scripts.score_and_pick import keyword_score_jd
from scripts.tailor_resume import validate_tailored


def test_tech_job_filtering_rejects_loopholes():
    """Verify that false positives identified in pipeline logs are strictly rejected."""
    # False positives from pipeline run
    assert not _is_tech_job("Business Development & Client Relationships Manager: Nigeria & Beyond - 20HRS PW")
    assert not _is_tech_job("Sales Development Representative - HVAC Industry")
    assert not _is_tech_job("AML Investigator III")
    assert not _is_tech_job("Norwegian Tech Linguistic Tester")
    assert not _is_tech_job("Account Executive - Mid Market")
    assert not _is_tech_job("Product Manager, Platform")
    assert not _is_tech_job("Senior Product Designer")
    assert not _is_tech_job("Senior Technical Writer")
    assert not _is_tech_job("Recruiter / Talent Acquisition Specialist")


def test_tech_job_filtering_accepts_engineering_roles():
    """Verify that genuine engineering, QA, solutions, and AI roles are accepted."""
    assert _is_tech_job("Full Stack Engineer")
    assert _is_tech_job("Backend Software Engineer")
    assert _is_tech_job("Frontend Developer")
    assert _is_tech_job("QA Automation Engineer")
    assert _is_tech_job("SDET 1")
    assert _is_tech_job("AI Engineer")
    assert _is_tech_job("Solutions Engineer")
    assert _is_tech_job("Python Developer")
    assert _is_tech_job("React Engineer")
    assert _is_tech_job("Cloud / DevOps Engineer")


def test_is_senior_only_rejects_non_tech_managers():
    """Verify that non-tech management roles are marked senior/excluded."""
    assert _is_senior_only("Business Development Manager")
    assert _is_senior_only("Client Relationships Manager")
    assert _is_senior_only("Operations Manager")


def test_is_viable_apply_url_direct_and_himalayas():
    """Verify that direct ATS URLs and Himalayas are recognized as viable."""
    viable, _ = _is_viable_apply_url("https://jobs.ashbyhq.com/supabase/a1320bbf", "ashby")
    assert viable is True

    viable, _ = _is_viable_apply_url("https://jobs.lever.co/meesho/12345", "lever")
    assert viable is True

    viable, _ = _is_viable_apply_url("https://boards.greenhouse.io/stripe/jobs/67890", "greenhouse")
    assert viable is True

    viable, _ = _is_viable_apply_url("https://himalayas.app/companies/test/jobs/engineer", "himalayas")
    assert viable is True

    viable, reason = _is_viable_apply_url("https://unknowncompany.com/apply", "unknown")
    assert viable is False
    assert "Unsupported ATS domain" in reason


def test_keyword_score_jd_picks_appropriate_variant():
    """Verify that the zero-LLM keyword heuristic scores tech JDs accurately."""
    # Frontend JD
    fe_jd = {
        "title": "Junior Frontend Engineer",
        "description": "Looking for React, Next.js, and TypeScript experience.",
        "remote": True
    }
    res_fe = keyword_score_jd(fe_jd)
    assert res_fe["best_variant"] == "frontend"
    assert res_fe["score"] >= 65
    assert res_fe["zero_llm"] is True

    # QA / SDET JD
    qa_jd = {
        "title": "QA Automation Engineer",
        "description": "Write Playwright and pytest tests for automated web testing.",
        "remote": False
    }
    res_qa = keyword_score_jd(qa_jd)
    assert res_qa["best_variant"] == "sdet-qa"
    assert res_qa["score"] >= 60

    # Python Backend JD
    py_jd = {
        "title": "Backend Python Developer",
        "description": "FastAPI, PostgreSQL, and Django APIs.",
        "remote": True
    }
    res_py = keyword_score_jd(py_jd)
    assert res_py["best_variant"] == "backend-python"
    assert res_py["score"] >= 65


def test_validate_tailored_self_healing():
    """Verify that missing non-critical keys and hallucinated skills are repaired."""
    original = {
        "variant": "frontend",
        "contact": {"email": "test@example.com"},
        "summary": "Original summary",
        "experience": [{"company": "A", "bullets": ["b1", "b2"]}],
        "skills": ["React", "TypeScript", "HTML", "CSS"],
        "projects": [{"name": "P1", "bullets": ["p1"]}],
        "education": [{"degree": "BCA"}],
        "certifications": ["AWS"]
    }

    # LLM omitted 'skills' and 'certifications', hallucinated 'Quantum Computing'
    tailored = {
        "variant": "frontend",
        "summary": "Tailored summary",
        "experience": [{"company": "A", "bullets": ["b1_tailored", "b2_tailored"]}],
        "skills": ["React", "Quantum Computing"],
        "projects": [{"name": "P1", "bullets": ["p1_tailored"]}],
    }

    validate_tailored(original, tailored)

    # Missing certifications and contact should be restored
    assert "certifications" in tailored
    assert tailored["certifications"] == ["AWS"]
    assert "contact" in tailored

    # Hallucinated skill 'Quantum Computing' should be removed, valid skill 'React' preserved
    assert "Quantum Computing" not in tailored["skills"]
    assert "React" in tailored["skills"]

    # Tailored summary and bullets should remain intact
    assert tailored["summary"] == "Tailored summary"
    assert tailored["experience"][0]["bullets"] == ["b1_tailored", "b2_tailored"]
