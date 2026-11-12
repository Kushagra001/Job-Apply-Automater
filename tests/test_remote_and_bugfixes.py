from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from groq import GroqError

import scripts.auto_apply_ats as ats
import scripts.cleanup_sheet as cleanup
import scripts.doctor as doctor
import scripts.enrich_jd as enrich
import scripts.fetch_jds as fj
import scripts.log_and_notify as log_mod
import scripts.outreach as outreach
import scripts.run_pipeline as pipeline
import scripts.score_and_pick as scorer
import scripts.tailor_resume as tailor
import scripts.track_applications as tracker


# ── Groq active models, 404 non-retry, <think> strip & cover-letter fallback ─

def test_groq_models_and_json_cleaning():
    assert scorer.FALLBACK_MODELS[0] == "openai/gpt-oss-20b"
    assert "groq/compound" not in scorer.FALLBACK_MODELS
    assert tailor.FALLBACK_MODELS == scorer.FALLBACK_MODELS

    raw = "<think>reasoning steps here</think>\n```json\n{\"score\": 84, \"best_variant\": \"backend-python\"}\n```"
    parsed = json.loads(scorer._clean_llm_json(raw))
    assert parsed["score"] == 84
    assert parsed["best_variant"] == "backend-python"


def test_groq_404_model_not_found_does_not_retry(monkeypatch):
    calls = {"count": 0}

    class FakeCompletions:
        def create(self, **kwargs):
            calls["count"] += 1
            raise GroqError("Error code: 404 - {'error': {'code': 'model_not_found'}}")

    monkeypatch.setattr(scorer, "Groq", lambda: SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions())))
    monkeypatch.setenv("GROQ_MODEL", "deprecated/model")

    variants = {"backend-python": json.loads(Path("resumes/backend-python.json").read_text(encoding="utf-8"))}
    with pytest.raises(GroqError):
        scorer.score_jd_against_all_variants(
            {"title": "Python Engineer", "company": "Acme", "location": "Remote", "remote": True, "description": "Python FastAPI"},
            variants,
        )
    assert calls["count"] == 1  # Must NOT retry 404 three times via tenacity!


def test_tailor_resume_fallback_cover_letter_and_dash_sanitization(monkeypatch):
    class FakeCompletions:
        def create(self, **kwargs):
            payload = {
                "variant": "ai-engineer",
                "summary": "Engineered low\u2011latency FastAPI & RAG services\u2014remote ready.",
                "experience": json.loads(Path("resumes/ai-engineer.json").read_text(encoding="utf-8"))["experience"],
                "skills": ["Python", "FastAPI"],
                "projects": json.loads(Path("resumes/ai-engineer.json").read_text(encoding="utf-8"))["projects"],
                "cover_letter": "",  # Simulate reasoning model omitting/emptying cover letter
            }
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)))])

    monkeypatch.setattr(tailor, "Groq", lambda: SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions())))
    monkeypatch.delenv("GROQ_MODEL", raising=False)

    variant = json.loads(Path("resumes/ai-engineer.json").read_text(encoding="utf-8"))
    jd = {"title": "Junior AI Engineer", "company": "GlobalAI", "location": "Worldwide", "remote": True, "description": "Python LLM APIs"}
    res = tailor.tailor_resume(variant, jd)

    assert "\u2011" not in res["summary"]
    assert "\u2014" not in res["summary"]
    assert "GlobalAI" in res["cover_letter"]
    assert len(res["cover_letter"]) > 50


def test_doctor_groq_uses_adequate_max_tokens_and_strips_think(monkeypatch):
    captured_kwargs = {}

    class FakeCompletions:
        def create(self, **kwargs):
            captured_kwargs.update(kwargs)
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="<think>ok</think>PONG"))])

    monkeypatch.setenv("GROQ_API_KEY", "test_key")
    monkeypatch.delenv("GROQ_MODEL", raising=False)
    monkeypatch.setattr("groq.Groq", lambda **kw: SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions())))

    res = doctor._check_groq_api()
    assert res.status == "OK"
    assert res.message.endswith("replied: 'PONG'") or "replied: 'PONG'" in res.message
    assert captured_kwargs["max_tokens"] >= 64


# ── Remote optimization, Greenhouse embed canonical URLs & filters ────────────

def test_greenhouse_normalizes_custom_corporate_urls_and_enriches(monkeypatch):
    recent_iso = datetime.now(timezone.utc).isoformat()
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "jobs": [
            {
                "id": 7890123,
                "title": "Software Engineer, Backend",
                "absolute_url": "https://www.rubrik.com/company/careers/departments/job.7890123?gh_jid=7890123",
                "location": {"name": "Bengaluru, India"},
                "first_published": recent_iso,
                "content": "<p>Build distributed Python &amp; Go backend services in India.</p>",
            },
            {
                "id": 555666,
                "title": "Frontend Software Engineer",
                "absolute_url": "https://job-boards.greenhouse.io/vercel/jobs/555666",
                "location": {"name": "Remote - Anywhere"},
                "first_published": recent_iso,
                "content": "<p>Build React and Next.js web applications remotely.</p>",
            },
        ]
    }
    monkeypatch.setattr("requests.get", lambda *a, **kw: resp)

    jobs = fj._fetch_single_greenhouse("rubrik")
    assert len(jobs) == 2
    assert jobs[0]["apply_url"] == "https://job-boards.greenhouse.io/embed/job_app?for=rubrik&token=7890123"
    assert jobs[1]["apply_url"] == "https://job-boards.greenhouse.io/vercel/jobs/555666"

    # Both formats must pass URL validation and ATS enrichment resolver
    assert ats.validate_ats_job_url(jobs[0]["apply_url"])[0] is True
    assert ats.detect_ats(jobs[0]["apply_url"]) == "greenhouse"

    enrich_resp = MagicMock()
    enrich_resp.status_code = 200
    enrich_resp.json.return_value = {"content": "<p>Full embed description loaded.</p>"}
    monkeypatch.setattr("requests.get", lambda url, **kw: enrich_resp if "boards-api.greenhouse.io/v1/boards/rubrik/jobs/7890123" in url else MagicMock(status_code=404))
    assert "Full embed description loaded." in enrich.resolve_greenhouse(jobs[0]["apply_url"])


def test_remote_only_and_remote_focus_calibration(monkeypatch):
    # REMOTE_ONLY=true filters out onsite roles in fetch_all()
    monkeypatch.setenv("REMOTE_ONLY", "true")
    monkeypatch.setattr(fj, "fetch_ashby", lambda: [
        {"title": "Onsite Dev", "company": "Co A", "remote": False, "apply_url": "https://jobs.ashbyhq.com/coa/12345678-1234-1234-1234-123456789012", "source": "ashby"},
        {"title": "Remote Dev", "company": "Co B", "remote": True, "apply_url": "https://jobs.ashbyhq.com/cob/22345678-1234-1234-1234-123456789012", "source": "ashby"},
    ])
    for fn_name in ("fetch_lever", "fetch_greenhouse", "fetch_remoteok", "fetch_remotive", "fetch_jobicy", "fetch_arbeitnow", "fetch_hackernews", "fetch_himalayas", "fetch_simplify_jobs", "fetch_yc_jobs"):
        monkeypatch.setattr(fj, fn_name, lambda: [])
    monkeypatch.setattr("scripts.log_and_notify.get_processed_urls", lambda: set())

    queued = fj.fetch_all()
    assert [j["company"] for j in queued] == ["Co B"]

    # Remote focus penalizes non-remote match by 10 points when competitive
    monkeypatch.setenv("REMOTE_FOCUS", "true")
    onsite_fe = {"title": "Junior Frontend Engineer", "description": "React and TypeScript UI engineer.", "remote": False}
    remote_fe = {"title": "Junior Frontend Engineer", "description": "React and TypeScript UI engineer.", "remote": True}
    assert scorer.keyword_score_jd(remote_fe)["score"] > scorer.keyword_score_jd(onsite_fe)["score"]


def test_geo_knockout_proximity_arbeitnow_cctld_and_email_asset_filter(monkeypatch):
    # Mentioning India in an unrelated sentence must NOT waive a US work authorization requirement
    mixed_jd = (
        "Our enterprise customers span the US, UK, and India. "
        "Requirements: Candidates must be legally authorized to work in the United States without sponsorship."
    )
    assert fj.has_geo_knockout_in_text("Backend Software Engineer", mixed_jd) is True

    # But US/CA/India explicit multi-country authorization should still pass
    inclusive_jd = "Applicants must be legally authorized to work in the US, Canada, or India."
    assert fj.has_geo_knockout_in_text("Backend Software Engineer", inclusive_jd) is False

    # Arbeitnow localized ccTLDs and foreign-title/Dutch-remote traps must be filtered
    now_ts = int(datetime.now(timezone.utc).timestamp())
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "data": [
            {"title": "Full Stack Engineer", "company_name": "FrenchCo", "location": "", "remote": True, "created_at": now_ts, "description": "React Node", "url": "https://www.arbeitnow.fr/jobs/1"},
            {"title": "Backend Engineer", "company_name": "DutchCo", "location": "", "remote": True, "created_at": now_ts, "description": "Werken op afstand in Nederland", "url": "https://www.arbeitnow.com/jobs/2"},
            {"title": "AI Engineer - Germany", "company_name": "BerlinCo", "location": "", "remote": True, "created_at": now_ts, "description": "Python FastAPI", "url": "https://www.arbeitnow.com/jobs/3"},
            {"title": "Python Backend Developer", "company_name": "GlobalOk", "location": "Worldwide", "remote": True, "created_at": now_ts, "description": "Python FastAPI PostgreSQL remote role", "url": "https://www.arbeitnow.com/jobs/4"},
        ]
    }
    monkeypatch.setattr("requests.get", lambda *a, **kw: mock_resp)
    ab_jobs = fj.fetch_arbeitnow()
    assert [j["company"] for j in ab_jobs] == ["GlobalOk"]

    # Asset filenames like @2x.png must NOT be extracted as recruiter emails
    sample_html = "Apply to founders@startup.ai or view <img src='/img/hero@2x.png'> and logo@3x.webp and app@1.0.min.js"
    assert fj._extract_contact_emails(sample_html) == ["founders@startup.ai"]
    assert outreach._extract_emails_from_text(sample_html) == ["founders@startup.ai"]


def test_is_viable_apply_url_inspects_outbound_href_not_css_strings(monkeypatch):
    resp = MagicMock()
    resp.url = "https://remoteok.com/remote-jobs/fake"
    resp.text = "<html><head><!-- reference to boards.greenhouse.io in blog text --></head><body><a href='https://company.wd5.myworkdayjobs.com/en/job/1'>Apply</a></body></html>"
    monkeypatch.setattr("requests.get", lambda *a, **kw: resp)

    viable, _ = pipeline._is_viable_apply_url("https://remoteok.com/remote-jobs/fake", "remoteok")
    assert viable is False

    resp.text = "<html><body><a href='https://boards.greenhouse.io/acme/jobs/12345'>Apply Now</a></body></html>"
    viable_true, _ = pipeline._is_viable_apply_url("https://remoteok.com/remote-jobs/real", "remoteok")
    assert viable_true is True


# ── ATS Playwright fixes: Arbeitnow TLDs, React comboboxes, Ashby, Lever ───

def test_detect_ats_regional_arbeitnow_and_react_combobox_knockout():
    assert ats.detect_ats("https://www.arbeitnow.fr/jobs/123") == "arbeitnow"
    assert ats.detect_ats("https://www.arbeitnow.nl/jobs/456") == "arbeitnow"
    assert ats.detect_ats("https://www.arbeitnow.ch/jobs/789") == "arbeitnow"

    # Simulate Modern Greenhouse <input role="combobox"> for US authorization & Country
    clicked = []

    class FakeOpt:
        def __init__(self, text):
            self._t = text
        def inner_text(self):
            return self._t
        def click(self, timeout=5000):
            clicked.append(self._t)

    class FakeCombo:
        def __init__(self, ctx_attrs, parent_text):
            self.attrs = ctx_attrs
            self.parent_text = parent_text
        def get_attribute(self, k):
            return self.attrs.get(k)
        def click(self, timeout=5000):
            pass
        def fill(self, txt):
            pass
        def press(self, k):
            pass
        def locator(self, sel):
            loc = MagicMock()
            if "ancestor::" in sel:
                loc.count.return_value = 1
                loc.first.inner_text.return_value = self.parent_text
            else:
                loc.count.return_value = 0
            return loc

    us_combo = FakeCombo({"id": "question_1", "role": "combobox"}, "Are you legally authorized to work in the United States?")
    country_combo = FakeCombo({"id": "country", "role": "combobox"}, "Country")

    opts_state = [FakeOpt("Yes"), FakeOpt("No"), FakeOpt("India"), FakeOpt("United States")]

    class FakeModernPage:
        def wait_for_timeout(self, ms):
            pass
        def locator(self, sel):
            loc = MagicMock()
            if sel == "input[role='combobox']":
                loc.all.return_value = [us_combo, country_combo]
            elif sel == "[role='option']":
                loc.all.return_value = opts_state
            else:
                loc.all.return_value = []
                loc.count.return_value = 0
            return loc

    fake_page = FakeModernPage()
    ats._fill_react_comboboxes(fake_page)
    assert clicked == ["No", "India"]

    blocked, reason = ats._detect_foreign_work_auth_knockout(fake_page)
    assert blocked is True
    assert "united states" in reason.lower()


def test_ashby_hydration_closed_detection_and_lever_hcaptcha_classification(monkeypatch):
    # Closed Ashby job only reveals 'page not found' after SPA hydration wait
    hydrated = {"done": False}
    closed_page = MagicMock()
    closed_page.url = "https://jobs.ashbyhq.com/posthog/12345678-1234-1234-1234-123456789012"
    closed_page.title.return_value = "Jobs"

    def fake_wait_for_fn(*a, **kw):
        hydrated["done"] = True

    def body_locator(sel):
        loc = MagicMock()
        if sel == "body":
            loc.count.return_value = 1
            loc.inner_text.return_value = "The page you requested was not found." if hydrated["done"] else ""
        else:
            loc.count.return_value = 0
        return loc

    closed_page.wait_for_function.side_effect = fake_wait_for_fn
    closed_page.locator.side_effect = body_locator

    ok, reason = ats._apply_ashby(closed_page, {"company": "PostHog", "title": "Engineer"}, "dummy.pdf", dry_run=True, zero_llm_mode=True)
    assert ok is False
    assert reason.startswith("job_closed:")

    # Ashby resume locator must prioritize #_systemfield_resume before generic input[type='file']
    resume_selector_checked = []
    open_ashby = MagicMock()
    open_ashby.url = "https://jobs.ashbyhq.com/supabase/12345678-1234-1234-1234-123456789012/application"
    open_ashby.title.return_value = "Apply - Supabase"

    def open_loc(sel):
        if "input[type='file']" in sel:
            resume_selector_checked.append(sel)
        m = MagicMock()
        m.count.return_value = 0
        m.all.return_value = []
        return m

    open_ashby.locator.side_effect = open_loc
    monkeypatch.setattr(ats, "_detect_captcha", lambda p: False)
    monkeypatch.setattr(ats, "_is_job_closed", lambda p: (False, ""))
    monkeypatch.setattr(ats, "_fill_social_fields", lambda *a, **kw: None)
    monkeypatch.setattr(ats, "_fill_common_custom_fields", lambda p: None)
    monkeypatch.setattr(ats, "_detect_foreign_work_auth_knockout", lambda p: (False, ""))
    monkeypatch.setattr(ats, "_answer_custom_questions", lambda *a, **kw: None)

    ok_open, _ = ats._apply_ashby(open_ashby, {"company": "Supabase", "title": "Engineer"}, "dummy.pdf", dry_run=True, zero_llm_mode=True)
    assert ok_open is True
    assert resume_selector_checked and resume_selector_checked[0].startswith("input[type='file']#_systemfield_resume")

    # Lever submit blocked by hCaptcha overlay classifies as requires_captcha:
    lever_page = MagicMock()
    lever_page.url = "https://apply.lever.co/hevodata/12345678-1234-1234-1234-123456789012"
    submit_btn = MagicMock()
    submit_btn.is_visible.return_value = True
    submit_btn.click.side_effect = Exception("intercepted by hcaptcha iframe")
    captcha_state = {"calls": 0}
    monkeypatch.setattr(ats, "_detect_captcha", lambda p: (captcha_state.__setitem__("calls", captcha_state["calls"] + 1) or captcha_state["calls"] >= 3))

    def lever_loc(sel):
        m = MagicMock()
        if "btn-submit" in sel:
            m.all.return_value = [submit_btn]
        else:
            m.count.return_value = 0
            m.all.return_value = []
        return m

    lever_page.locator.side_effect = lever_loc
    ok_lv, note_lv = ats._apply_lever(lever_page, {"company": "Hevo", "title": "SDE"}, "dummy.pdf", dry_run=False, zero_llm_mode=True)
    assert ok_lv is False
    assert note_lv.startswith("requires_captcha:")


# ── Tracker ordering, cleanup priorities & render_error digest ───────────────

def test_tracker_sender_priority_and_status_updates(monkeypatch):
    # Word 'fly' or 'square' in unrelated email body must NOT override sender company Stripe!
    comp = tracker.extract_company_from_email(
        sender="Stripe Recruiting <no-reply@greenhouse.io>",
        subject="Application update",
        body="Thank you for flying through our challenge!",
        known_companies=["Fly", "Square"],
    )
    assert comp == "Stripe"

    # Downgrade guard blocks interview_scheduled -> interview_scheduled / receipt
    assert tracker._STATUS_PROGRESSION["skipped_geo_knockout"] == 0
    assert cleanup.STATUS_PRIORITY["skipped_geo_knockout"] == 1

    # Digest buffer records render_error under failed
    monkeypatch.setattr(log_mod, "append_row", lambda row: None)
    monkeypatch.setattr(
        log_mod,
        "_DIGEST_BUFFER",
        {"applied_tailored": [], "applied_base": [], "requires_captcha": [], "outreach_drafts": [], "failed": [], "skipped": []},
    )
    log_mod.log_result(jd={"company": "RenderFailCo", "title": "SDE", "source": "hn", "apply_url": "https://x"}, score=80, variant_used="frontend", status="render_error")
    assert len(log_mod._DIGEST_BUFFER["failed"]) == 1

