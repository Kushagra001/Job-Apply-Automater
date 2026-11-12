from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pypdf
import pytest

import scripts.auto_apply_ats as ats
import scripts.fetch_jds as fj
import scripts.log_and_notify as log_mod
import scripts.outreach as outreach
import scripts.run_pipeline as pipeline
import scripts.score_and_pick as scorer
import scripts.tailor_resume as tailor
from scripts.render_pdf import render_pdf


# ── Phase 1: Freshness, geo-knockouts, exclusions & boards ────────────────────

def test_parse_posted_at_and_max_age_filtering(monkeypatch):
    now = datetime.now(timezone.utc)
    recent_iso = (now - timedelta(days=3)).isoformat()
    stale_iso = (now - timedelta(days=28)).isoformat()
    stale_epoch_ms = int((now - timedelta(days=30)).timestamp() * 1000)
    recent_rfc = "Tue, 10 Nov 2026 12:00:00 +0000"

    assert fj._parse_posted_at(recent_iso)
    assert fj._parse_posted_at(stale_epoch_ms)
    assert fj._parse_posted_at(recent_rfc)
    assert fj._parse_posted_at("2026-11-09")
    assert fj._parse_posted_at("not-a-date") == ""
    assert fj._parse_posted_at("") == ""

    monkeypatch.setenv("MAX_JOB_AGE_DAYS", "14")
    assert fj._is_too_old(recent_iso) is False
    assert fj._is_too_old(stale_iso) is True
    assert fj._is_too_old("") is False  # Missing timestamp must not drop mocked/unknown items


def test_has_geo_knockout_and_updated_title_location_filters():
    # Hard US/CA/UK/clearance walls inside JD text must be caught
    assert fj.has_geo_knockout_in_text("Software Engineer", "Candidates must be legally authorized to work in the United States without sponsorship.") is True
    assert fj.has_geo_knockout_in_text("Backend Dev", "Active security clearance required for this public sector project.") is True
    assert fj.has_geo_knockout_in_text("Full Stack Engineer", "Must reside in Canada or the European Union.") is True

    # Global EOR / no-barrier remote & explicit India hubs must pass
    assert fj.has_geo_knockout_in_text("Backend Engineer", "Remote anywhere in the world. Async-first distributed team with EOR hiring.") is False
    assert fj.has_geo_knockout_in_text("Software Engineer", "Located in Bengaluru, India collaborating with our US headquarters.") is False

    # Newly added exclusions (Intermediate, ERP stacks, Canadian/UK/Polish/Gulf cities, Manager/Level II)
    assert fj._is_senior_only("Intermediate Full Stack Engineer") is True
    assert fj._is_senior_only("Manager, Site Reliability Engineer") is True
    assert fj._is_senior_only("Data Scientist II") is True
    assert fj._is_tech_job("Salesforce Service Cloud Developer") is False
    assert fj._is_tech_job("SAP ABAP Consultant") is False
    assert fj.is_location_ok("Ottawa, Canada", True) is False
    assert fj.is_location_ok("Waterloo, Ontario", False) is False
    assert fj.is_location_ok("Manchester, UK", True) is False
    assert fj.is_location_ok("Warsaw, Poland", True) is False
    assert fj.is_location_ok("Remote, KSA", True) is False

    # Rebalanced company boards
    assert "slice" not in fj.GREENHOUSE_COMPANIES
    for comp in ("rubrik", "fivetran", "inmobi", "grafanalabs", "druva", "commerceiq", "zscaler"):
        assert comp in fj.GREENHOUSE_COMPANIES
    assert "hevodata" in fj.LEVER_COMPANIES
    assert "fampay" in fj.LEVER_COMPANIES
    assert "airbyte" in fj.ASHBY_COMPANIES
    assert "livekit" in fj.ASHBY_COMPANIES


def test_recency_tier_prioritizes_fresher_and_india_jobs():
    now = datetime.now(timezone.utc)
    fresh_india = {"posted_at": (now - timedelta(hours=12)).isoformat(), "location": "Bengaluru, India"}
    fresh_global = {"posted_at": (now - timedelta(days=1)).isoformat(), "location": "Worldwide"}
    week_old = {"posted_at": (now - timedelta(days=5)).isoformat(), "location": "India"}
    unknown = {"posted_at": "", "location": "Remote"}

    assert fj._recency_tier(fresh_india) < fj._recency_tier(fresh_global)
    assert fj._recency_tier(fresh_global) < fj._recency_tier(week_old)
    assert fj._recency_tier(week_old) < fj._recency_tier(unknown)


def test_simplify_filters_stale_and_non_ats_urls(monkeypatch):
    now = datetime.now(timezone.utc)
    recent_ts = int((now - timedelta(days=2)).timestamp())
    stale_ts = int((now - timedelta(days=25)).timestamp())

    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = [
        {
            "active": True,
            "is_visible": True,
            "title": "Junior Backend Engineer",
            "company_name": "WorkdayOnlyCo",
            "locations": ["Remote"],
            "url": "https://workdayonly.wd5.myworkdayjobs.com/Careers/job/123",
            "date_posted": recent_ts,
        },
        {
            "active": True,
            "is_visible": True,
            "title": "Junior Frontend Engineer",
            "company_name": "StaleStartup",
            "locations": ["Remote"],
            "url": "https://boards.greenhouse.io/stalestartup/jobs/999",
            "date_posted": stale_ts,
        },
        {
            "active": True,
            "is_visible": True,
            "title": "New Grad Software Engineer",
            "company_name": "FreshStartup",
            "locations": ["Remote"],
            "url": "https://jobs.ashbyhq.com/freshstartup/12345678-1234-1234-1234-123456789012",
            "date_posted": recent_ts,
        },
    ]
    monkeypatch.setattr("requests.get", lambda *a, **kw: resp)

    jobs = fj.fetch_simplify_jobs()
    assert [j["company"] for j in jobs] == ["FreshStartup"]
    assert jobs[0]["posted_at"]


def test_hackernews_extracts_contact_emails(monkeypatch):
    story_resp = MagicMock()
    story_resp.status_code = 200
    story_resp.json.return_value = {"hits": [{"objectID": "111"}]}

    comments_resp = MagicMock()
    comments_resp.status_code = 200
    comments_resp.json.return_value = {
        "hits": [
            {
                "created_at": datetime.now(timezone.utc).isoformat(),
                "comment_text": (
                    "SignalAI | Junior AI Engineer | Remote<p>"
                    "Reach out at founders@signalai.dev or no-reply@signalai.dev.<p>"
                    "Apply: https://jobs.ashbyhq.com/signalai/11223344-5566-7788-99aa-bbccddeeff00"
                ),
            }
        ]
    }
    monkeypatch.setattr("requests.get", lambda url, **kw: story_resp if "search_by_date" in url else comments_resp)
    jobs = fj.fetch_hackernews()
    assert len(jobs) == 1
    assert jobs[0].get("contact_emails") == ["founders@signalai.dev"]


# ── Phase 2: Honest resumes, skill ordering, scoring calibration & PDF text ───

def test_resumes_grounded_without_inflated_rps_claims():
    for fname in ("backend-python.json", "backend-node.json", "ai-engineer.json", "frontend.json"):
        text = Path("resumes", fname).read_text(encoding="utf-8").lower()
        assert "requests/sec" not in text, f"{fname} still contains implausible requests/sec metric"
        assert "1,000+ installs" not in text, f"{fname} still contains implausible installs metric"

    py_data = json.loads(Path("resumes/backend-python.json").read_text(encoding="utf-8"))
    proj_names = [p["name"] for p in py_data["projects"]]
    assert "Automated Job Application Pipeline" in proj_names


def test_prioritize_matching_skills_surfaces_jd_skills_without_invention():
    skills = ["Docker", "React.js", "PostgreSQL", "FastAPI (basic)", "Python", "Tailwind CSS"]
    jd = {
        "title": "Junior Backend Python Engineer",
        "description": "Looking for Python, FastAPI, and Postgres experience for REST APIs.",
    }
    reordered = tailor._prioritize_matching_skills(skills, jd)
    assert set(reordered) == set(skills)
    assert len(reordered) == len(skills)
    assert reordered[:3] == ["PostgreSQL", "FastAPI (basic)", "Python"]


def test_keyword_scorer_and_prompt_penalize_geo_and_senior_walls(monkeypatch):
    geo_walled_jd = {
        "title": "Junior AI Engineer",
        "description": "Build LLM and Python pipelines. Candidates must be legally authorized to work in the United States.",
        "remote": True,
    }
    res_geo = scorer.keyword_score_jd(geo_walled_jd)
    assert res_geo["score"] <= 35

    # Ensure sparse JDs do NOT receive the removed "Be generous — score 70+" instruction
    captured = []
    class FakeCompletions:
        def create(self, **kwargs):
            captured.append(kwargs["messages"][1]["content"])
            msg = SimpleNamespace(content=json.dumps({
                "best_variant": "ai-engineer",
                "score": 50,
                "missing_skills": [],
                "reasoning": "Sparse JD",
            }))
            return SimpleNamespace(choices=[SimpleNamespace(message=msg)])

    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))
    monkeypatch.setattr(scorer, "Groq", lambda: fake_client)
    monkeypatch.delenv("GROQ_MODEL", raising=False)

    variants = {"ai-engineer": json.loads(Path("resumes/ai-engineer.json").read_text(encoding="utf-8"))}
    scorer.score_jd_against_all_variants(
        {"title": "Software Engineer", "company": "TestCo", "location": "Remote", "remote": True, "description": "Short."},
        variants,
    )
    assert "Be generous" not in captured[0]
    assert "score conservatively" in captured[0].lower()


def test_render_pdf_preserves_word_spacing_on_metrics(tmp_path, monkeypatch):
    monkeypatch.setattr("scripts.render_pdf.OUTPUT_DIR", tmp_path)
    resume = json.loads(Path("resumes/backend-python.json").read_text(encoding="utf-8"))
    pdf_path = render_pdf(resume, company="SpacingCheckCo", date="20261112")
    text = pypdf.PdfReader(pdf_path).pages[0].extract_text()
    assert "by 20%" in text or "by ~20%" in text


# ── Phase 3: Geo-safe ATS autofill & company cooldowns ───────────────────────

def test_candidate_qa_profile_removes_hardcoded_us_salary():
    assert "$60,000" not in ats.CANDIDATE_QA_PROFILE["expected_salary"]
    assert "negotiable" in ats.CANDIDATE_QA_PROFILE["expected_salary"].lower()


def test_fill_common_custom_fields_does_not_falsely_claim_us_work_auth():
    class FakeOption:
        def __init__(self, text):
            self._text = text
        def inner_text(self):
            return self._text

    class FakeSelect:
        def __init__(self, attrs, options):
            self.attrs = attrs
            self._options = [FakeOption(o) for o in options]
            self.selected_idx = None

        def get_attribute(self, k):
            return self.attrs.get(k)

        def locator(self, sel):
            loc = MagicMock()
            if sel == "option":
                loc.all.return_value = self._options
            else:
                loc.count.return_value = 0
            return loc

        def select_option(self, index):
            self.selected_idx = index

    us_auth_select = FakeSelect(
        {"name": "work_auth_us", "aria-label": "Are you legally authorized to work in the United States?"},
        ["Select...", "Yes", "No"],
    )
    india_auth_select = FakeSelect(
        {"name": "work_auth_in", "aria-label": "Are you legally authorized to work in India?"},
        ["Select...", "Yes", "No"],
    )

    class FakePage:
        def locator(self, sel):
            loc = MagicMock()
            if sel == "select":
                loc.all.return_value = [us_auth_select, india_auth_select]
            else:
                loc.all.return_value = []
                loc.count.return_value = 0
            return loc

    page = FakePage()
    ats._fill_common_custom_fields(page)

    assert us_auth_select.selected_idx == 2     # Must pick "No", NEVER "Yes"
    assert india_auth_select.selected_idx == 1  # Must pick "Yes" for India

    blocked, reason = ats._detect_foreign_work_auth_knockout(page)
    assert blocked is True
    assert "united states" in reason.lower()


def test_company_cooldown_blocks_burst_and_recent_reapplies(monkeypatch):
    now = datetime.now(timezone.utc)
    rows = [
        {"company": "Airtable", "status": "apply_success_tailored", "timestamp": (now - timedelta(days=5)).isoformat()},
        {"company": "OldCo", "status": "apply_success_base", "timestamp": (now - timedelta(days=35)).isoformat()},
        {"company": "CapCo", "status": "apply_success_tailored", "timestamp": (now - timedelta(days=20)).isoformat()},
        {"company": "CapCo", "status": "application_received", "timestamp": (now - timedelta(days=16)).isoformat()},
    ]
    mock_sheet = MagicMock()
    mock_sheet.get_all_records.return_value = rows
    monkeypatch.setattr(log_mod, "_get_sheet", lambda: mock_sheet)
    monkeypatch.setattr(log_mod, "_COMPANY_HISTORY_CACHE", None)

    history = log_mod.get_company_apply_history(days=30)
    assert "oldco" not in history  # Expired >30d

    assert log_mod.check_company_cooldown("Airtable", history=history)[0] is True
    assert log_mod.check_company_cooldown("CapCo", history=history)[0] is True
    assert log_mod.check_company_cooldown("OldCo", history=history)[0] is False
    assert log_mod.check_company_cooldown("FreshCo", history=history, run_counts={"freshco": 1})[0] is True


def test_run_pipeline_enforces_strict_65_threshold_cooldown_and_geo_gate(tmp_path, monkeypatch):
    monkeypatch.setattr("scripts.render_pdf.OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(outreach, "OUTREACH_OUTPUT_DIR", tmp_path / "outreach")
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("ENABLE_OUTREACH", "true")

    now = datetime.now(timezone.utc)
    monkeypatch.setattr(pipeline, "get_company_apply_history", lambda days=30: {
        "cooldownco": [now - timedelta(days=2)],
    })
    monkeypatch.setattr(pipeline, "fetch_all", lambda: [
        {
            "title": "Software Engineer",
            "company": "CooldownCo",
            "location": "Remote",
            "remote": True,
            "description": "Python FastAPI role " * 30,
            "apply_url": "https://boards.greenhouse.io/cooldownco/jobs/1",
            "source": "greenhouse",
        },
        {
            "title": "Backend Engineer",
            "company": "USWallCo",
            "location": "Remote",
            "remote": True,
            "description": ("Python backend engineer. Must be legally authorized to work in the United States. " * 6),
            "apply_url": "https://boards.greenhouse.io/uswallco/jobs/2",
            "source": "greenhouse",
        },
        {
            "title": "Average Fit Role",
            "company": "MidScoreCo",
            "location": "Remote",
            "remote": True,
            "description": "Some Node.js and REST APIs work. " * 20,
            "apply_url": "https://boards.greenhouse.io/midscoreco/jobs/3",
            "source": "greenhouse",
        },
    ])

    scored_companies = []
    def fake_score(jd, zero_llm_mode=False):
        scored_companies.append(jd["company"])
        return {
            "best_variant": "backend-node",
            "score": 52,  # Above old 20 floor, strictly below 65!
            "missing_skills": ["Go"],
            "reasoning": "Below threshold",
            "rate_limit_tripped": False,
        }

    monkeypatch.setattr(pipeline, "score_and_pick", fake_score)
    apply_called = []
    monkeypatch.setattr(pipeline, "apply", lambda *a, **kw: (apply_called.append(a), (True, "ok"))[1])

    logged = []
    monkeypatch.setattr(pipeline, "log_result", lambda **kw: logged.append(kw))
    monkeypatch.setattr(pipeline, "send_daily_digest", lambda: None)
    monkeypatch.setattr("time.sleep", lambda s: None)
    monkeypatch.setattr("sys.argv", ["run_pipeline.py", "--max-jobs", "5"])

    pipeline.main()

    assert scored_companies == ["MidScoreCo"]  # CooldownCo and USWallCo skipped before Groq call!
    assert apply_called == []                 # Score 52 NEVER applied (strict >= 65 gate!)
    statuses = {r["jd"]["company"]: r["status"] for r in logged}
    assert statuses["CooldownCo"] == "skipped_company_cooldown"
    assert statuses["USWallCo"] == "skipped_geo_knockout"
    assert statuses["MidScoreCo"] == "skipped_low_score"

    assert pipeline.classify_apply_status(False, "skipped_geo_knockout: US auth required", False, "apply_success_tailored") == "skipped_geo_knockout"


# ── Phase 4: Cold outreach discovery, pitch & Gmail draft persistence ─────────

def test_outreach_draft_generation_and_imap_draft_append(tmp_path, monkeypatch):
    monkeypatch.setattr(outreach, "OUTREACH_OUTPUT_DIR", tmp_path)
    monkeypatch.setenv("ENABLE_OUTREACH", "true")
    monkeypatch.setenv("OUTREACH_SCORE_THRESHOLD", "72")
    monkeypatch.setenv("GMAIL_USER", "kushagra@example.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "test-app-pass")

    appended = []
    class FakeIMAP:
        def __init__(self, host):
            self.host = host
        def login(self, u, p):
            return ("OK", [])
        def append(self, folder, flags, dt, raw_bytes):
            appended.append({"folder": folder, "flags": flags, "raw": raw_bytes})
            return ("OK", [b"1"])
        def logout(self):
            return ("BYE", [])

    monkeypatch.setattr("imaplib.IMAP4_SSL", FakeIMAP)
    monkeypatch.setattr(log_mod, "_DIGEST_BUFFER", {
        "applied_tailored": [], "applied_base": [], "requires_captcha": [], "outreach_drafts": [], "failed": [], "skipped": []
    })

    pdf_file = tmp_path / "signalai_ai-engineer.pdf"
    pdf_file.write_bytes(b"%PDF-1.4 fake pdf")
    variant = json.loads(Path("resumes/ai-engineer.json").read_text(encoding="utf-8"))
    jd = {
        "title": "Junior AI Engineer",
        "company": "SignalAI",
        "location": "Remote",
        "remote": True,
        "source": "hackernews",
        "apply_url": "https://jobs.ashbyhq.com/signalai/11223344-5566-7788-99aa-bbccddeeff00",
        "description": "Building agentic LLM pipelines with Python.",
        "contact_emails": ["founders@signalai.dev"],
    }

    assert outreach.should_draft_outreach(jd, score=75) is True
    res = outreach.create_outreach_draft(
        jd=jd,
        variant_json=variant,
        score=75,
        pdf_path=str(pdf_file),
        dry_run=False,
        zero_llm_mode=True,
        probe_website=False,
    )

    assert res["created"] is True
    assert res["recipient"] == "founders@signalai.dev"
    assert res["gmail_draft_saved"] is True
    assert len(res["body"].split()) <= 125
    assert Path(res["artifact_path"]).exists()
    assert len(appended) == 1
    assert appended[0]["folder"] == "[Gmail]/Drafts"
    assert appended[0]["flags"] == "\\Draft"
    assert b"founders@signalai.dev" in appended[0]["raw"]
    assert len(log_mod._DIGEST_BUFFER["outreach_drafts"]) == 1

