import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pypdf
import pytest
from groq import GroqError

import scripts.auto_apply_ats as ats
import scripts.log_and_notify as log_mod
import scripts.run_pipeline as pipeline
import scripts.score_and_pick as scorer
import scripts.tailor_resume as tailor
from scripts.render_pdf import _sanitize_slug, render_pdf


# ── Resume JSON integrity ────────────────────────────────────────────────────

def test_resume_variants_valid_and_deduplicated():
    resumes_dir = Path("resumes")
    files = sorted(resumes_dir.glob("*.json"))
    assert len(files) >= 4

    for f in files:
        data = json.loads(f.read_text(encoding="utf-8"))
        assert data.get("variant") == f.stem
        assert isinstance(data.get("experience"), list) and len(data["experience"]) > 0
        for exp in data["experience"]:
            assert isinstance(exp.get("role"), str) and exp["role"].strip(), f"{f.name} missing role"
            assert isinstance(exp.get("company"), str) and exp["company"].strip()
            assert isinstance(exp.get("bullets"), list) and len(exp["bullets"]) > 0
        project_names = [p["name"] for p in data.get("projects", [])]
        assert len(project_names) == len(set(project_names)), f"Duplicate projects in {f.name}: {project_names}"

    backend_py = (resumes_dir / "backend-python.json").read_text(encoding="utf-8")
    assert "maintain ed" not in backend_py


# ── PDF rendering & filename sanitization ────────────────────────────────────

def test_sanitize_slug_prevents_path_traversal_and_overflow():
    slug = _sanitize_slug("Acme / AI & Cloud-Ops (USA) Ltd. — Super Long Subsidiary Name That Breaks Linux Path Limits")
    assert "/" not in slug
    assert "\\" not in slug
    assert len(slug) <= 50
    assert slug.startswith("acme_ai_cloud-ops")
    assert _sanitize_slug("///...   ") == "company"
    assert _sanitize_slug("", max_len=20, fallback="unknown") == "unknown"


def test_render_pdf_with_slashed_and_long_company_name(tmp_path, monkeypatch):
    monkeypatch.setattr("scripts.render_pdf.OUTPUT_DIR", tmp_path)
    base_resume = json.loads(Path("resumes/backend-python.json").read_text(encoding="utf-8"))
    weird_company = "Foo / Bar & Sons (Global/Remote) Incorporated Very Long Company Title Exceeding Fifty Characters"

    pdf_path = Path(render_pdf(base_resume, company=weird_company, date="20261111"))
    assert pdf_path.exists()
    assert pdf_path.parent == tmp_path
    assert "/" not in pdf_path.name
    assert len(pdf_path.name) <= 90
    reader = pypdf.PdfReader(str(pdf_path))
    assert len(reader.pages) == 1


# ── Scoring fixes ────────────────────────────────────────────────────────────

def test_keyword_score_jd_respects_word_boundaries():
    # "email" contains "ai", "html" contains "ml", "tailor" contains "ai" — must not trigger AI/Python/ML bonus
    unrelated_jd = {
        "title": "Email & HTML Template Coordinator",
        "description": "Manage daily email blasts, detail copywriting, and plain html formatting.",
        "remote": False,
    }
    score_unrelated = scorer.keyword_score_jd(unrelated_jd)
    assert score_unrelated["score"] == 45

    real_ai_jd = {
        "title": "Junior AI Engineer",
        "description": "Build LLM and RAG pipelines with Python.",
        "remote": True,
    }
    score_ai = scorer.keyword_score_jd(real_ai_jd)
    assert score_ai["best_variant"] == "ai-engineer"
    assert score_ai["score"] > score_unrelated["score"]


def test_score_jd_sends_real_role_titles_in_experience_titles(monkeypatch):
    captured_prompts = []

    class FakeCompletions:
        def create(self, **kwargs):
            captured_prompts.append(kwargs["messages"][1]["content"])
            msg = SimpleNamespace(content=json.dumps({
                "best_variant": "backend-python",
                "score": 82,
                "missing_skills": [],
                "reasoning": "Strong match",
            }))
            return SimpleNamespace(choices=[SimpleNamespace(message=msg)])

    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))
    monkeypatch.setattr(scorer, "Groq", lambda: fake_client)
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.delenv("GROQ_MODEL", raising=False)

    variants = {"backend-python": json.loads(Path("resumes/backend-python.json").read_text(encoding="utf-8"))}
    jd = {"title": "Junior Python Developer", "company": "TestCo", "location": "Remote", "remote": True, "description": "Python and Django REST APIs for early career engineer."}
    res = scorer.score_jd_against_all_variants(jd, variants)

    assert res["score"] == 87  # 82 + 5 remote bonus
    assert captured_prompts
    assert '"experience_titles": [\n      "Frontend Developer (Freelance)"' in captured_prompts[0]
    assert "Python Web Developer Intern" in captured_prompts[0]


# ── Tailoring fixes ──────────────────────────────────────────────────────────

def test_validate_tailored_sanitizes_types_and_protects_immutable_fields():
    original = {
        "variant": "ai-engineer",
        "contact": {"full_name": "Kushagra Singh Negi", "email": "real@example.com"},
        "summary": "Real summary",
        "experience": [
            {"company": "Stackform", "role": "Frontend Developer (Freelance)", "dates": "01/2026 - Present", "bullets": ["B1", "B2"]},
            {"company": "GYMYAK Pvt. Ltd.", "role": "Frontend Web Developer", "dates": "05/2025 - 12/2025", "bullets": ["B3"]},
        ],
        "skills": ["Python", "Groq API"],
        "projects": [
            {"name": "BidCopy", "stack": "Next.js, Supabase", "bullets": ["P1", "P2"]},
        ],
        "education": ["MCA, Vellore Institute of Technology, Bhopal"],
        "certifications": ["Oracle Certified Generative AI Professional (OCI 2025)"],
    }

    malformed_tailored = {
        "variant": "invented-variant",
        "contact": {"full_name": "Fake Name"},
        "summary": "Reordered summary for AI role",
        "experience": [
            "corrupted-string-entry",  # Non-dict item in list
            {"company": "FakeCo", "role": "CTO", "dates": "2010", "bullets": [123]},  # Mutated immutable fields + non-str bullet
        ],
        "skills": ["Python", "HallucinatedRust"],
        "projects": [
            {"name": "FakeProj", "stack": "Haskell", "bullets": ["Reordered P1", "Reordered P2"]},
        ],
        "education": ["MIT PhD"],
        "certifications": [],
    }

    tailor.validate_tailored(original, malformed_tailored)

    assert malformed_tailored["variant"] == "ai-engineer"
    assert malformed_tailored["contact"] == original["contact"]
    assert malformed_tailored["education"] == original["education"]
    assert malformed_tailored["certifications"] == original["certifications"]
    assert isinstance(malformed_tailored["experience"][0], dict)
    assert malformed_tailored["experience"][0] == original["experience"][0]
    assert malformed_tailored["experience"][1]["company"] == "GYMYAK Pvt. Ltd."
    assert malformed_tailored["experience"][1]["role"] == "Frontend Web Developer"
    assert malformed_tailored["experience"][1]["dates"] == "05/2025 - 12/2025"
    assert malformed_tailored["experience"][1]["bullets"] == ["B3"]
    assert malformed_tailored["skills"] == ["Python"]
    assert malformed_tailored["projects"][0]["name"] == "BidCopy"
    assert malformed_tailored["projects"][0]["stack"] == "Next.js, Supabase"
    assert malformed_tailored["projects"][0]["bullets"] == ["Reordered P1", "Reordered P2"]


def test_tailor_resume_raises_daily_rate_limit_exceeded_across_all_models(monkeypatch):
    class QuotaExhaustedCompletions:
        def create(self, **kwargs):
            raise GroqError("Error code: 429 - rate_limit_exceeded: tokens per day (TPD) limit 100000 reached")

    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=QuotaExhaustedCompletions()))
    monkeypatch.setattr(tailor, "Groq", lambda: fake_client)
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.delenv("GROQ_MODEL", raising=False)

    variant = json.loads(Path("resumes/ai-engineer.json").read_text(encoding="utf-8"))
    with pytest.raises(tailor.DailyRateLimitExceeded):
        tailor.tailor_resume(variant, {"title": "AI Engineer", "company": "TestCo", "description": "Python AI"})


# ── Logging & deduplication fixes ────────────────────────────────────────────

def test_get_processed_urls_caches_terminal_statuses_and_respects_dry_run(monkeypatch):
    rows = [
        {"apply_url": "https://boards.greenhouse.io/a/jobs/1", "status": "apply_success_tailored"},
        {"apply_url": "https://boards.greenhouse.io/b/jobs/2", "status": "skipped_low_score"},
        {"apply_url": "https://boards.greenhouse.io/c/jobs/3", "status": "job_closed"},
        {"apply_url": "https://boards.greenhouse.io/d/jobs/4", "status": "requires_captcha"},
        {"apply_url": "https://boards.greenhouse.io/e/jobs/5", "status": "skipped_unsupported_ats"},
        {"apply_url": "https://boards.greenhouse.io/f/jobs/6", "status": "dry_run_success"},
        {"apply_url": "https://boards.greenhouse.io/g/jobs/7", "status": "apply_failed"},
        {"apply_url": "https://boards.greenhouse.io/g/jobs/7", "status": "apply_error"},
        {"apply_url": "https://boards.greenhouse.io/h/jobs/8", "status": "apply_failed"},
        {"apply_url": "https://boards.greenhouse.io/h/jobs/8", "status": "apply_failed"},
        {"apply_url": "https://boards.greenhouse.io/h/jobs/8", "status": "apply_error"},
    ]
    mock_sheet = MagicMock()
    mock_sheet.get_all_records.return_value = rows
    monkeypatch.setattr(log_mod, "_get_sheet", lambda: mock_sheet)

    monkeypatch.setattr(log_mod, "_PROCESSED_URLS_CACHE", None)
    monkeypatch.setattr(log_mod, "_PROCESSED_URLS_CACHE_DRY_RUN", None)

    live_urls = log_mod.get_processed_urls(include_dry_run=False)
    assert "https://boards.greenhouse.io/a/jobs/1" in live_urls
    assert "https://boards.greenhouse.io/b/jobs/2" in live_urls  # skipped_low_score cached!
    assert "https://boards.greenhouse.io/c/jobs/3" in live_urls  # job_closed cached!
    assert "https://boards.greenhouse.io/d/jobs/4" in live_urls  # requires_captcha cached!
    assert "https://boards.greenhouse.io/e/jobs/5" in live_urls  # skipped_unsupported_ats cached!
    assert "https://boards.greenhouse.io/f/jobs/6" not in live_urls  # dry_run_success retried in live mode!
    assert "https://boards.greenhouse.io/g/jobs/7" not in live_urls  # only 2 failures (<3)
    assert "https://boards.greenhouse.io/h/jobs/8" in live_urls      # 3 failures (>=3)

    dry_urls = log_mod.get_processed_urls(include_dry_run=True)
    assert "https://boards.greenhouse.io/f/jobs/6" in dry_urls


def test_notify_captcha_uses_explicit_score_and_escapes_html(monkeypatch):
    sent = []
    monkeypatch.setattr(log_mod, "notify_telegram", lambda msg: sent.append(msg))
    monkeypatch.setattr(log_mod, "append_row", lambda row: None)
    monkeypatch.setattr(
        log_mod,
        "_DIGEST_BUFFER",
        {"applied_tailored": [], "applied_base": [], "requires_captcha": [], "failed": [], "skipped": []},
    )

    jd = {
        "company": "R&D <ScriptCo>",
        "title": "AI & ML <Engineer>",
        "source": "greenhouse",
        "apply_url": "https://boards.greenhouse.io/rd/jobs/1?a=1&b=2",
    }
    log_mod.log_result(jd=jd, score=78, variant_used="ai-engineer", status="requires_captcha")

    assert len(sent) == 1
    assert "<b>Score:</b> 78" in sent[0]
    assert "R&amp;D &lt;ScriptCo&gt;" in sent[0]
    assert "AI &amp; ML &lt;Engineer&gt;" in sent[0]
    assert "https://boards.greenhouse.io/rd/jobs/1?a=1&amp;b=2" in sent[0]


# ── ATS auto-apply & pipeline orchestration fixes ────────────────────────────

def test_candidate_qa_profile_education_matches_resumes():
    edu = ats.CANDIDATE_QA_PROFILE["education"]
    assert "Vellore Institute of Technology, Bhopal" in edu
    assert "JECRC University, Jaipur" in edu
    assert "Poornima" not in edu


def test_frame_without_title_method_does_not_crash_helpers():
    frame_like = MagicMock(spec=["locator"])
    body = MagicMock()
    body.count.return_value = 0
    frame_like.locator.return_value = body

    closed, _ = ats._is_job_closed(frame_like)
    assert closed is False
    assert ats._detect_captcha(frame_like) is False


def test_verify_submission_does_not_return_false_positive_on_disappearing_button():
    page = MagicMock()
    page.url = "https://jobs.lever.co/example/12345678-abcd"
    page.title.return_value = "Example Co"
    empty_loc = MagicMock()
    empty_loc.count.return_value = 0
    body_loc = MagicMock()
    body_loc.count.return_value = 1
    body_loc.inner_text.return_value = "Software Engineer application form loading"

    def locator_router(sel):
        if sel == "body":
            return body_loc
        return empty_loc

    page.locator.side_effect = locator_router

    ok, reason = ats._verify_submission(page, "lever")
    assert ok is False
    assert "unverified" in reason.lower()


def test_answer_custom_questions_skips_groq_when_zero_llm_mode(monkeypatch):
    page = MagicMock()
    monkeypatch.setattr("groq.Groq", lambda: (_ for _ in ()).throw(AssertionError("Groq should not be called")))
    ats._answer_custom_questions(page, {"title": "Dev", "company": "Co"}, zero_llm_mode=True)
    page.evaluate.assert_not_called()


def test_greenhouse_switches_into_grnhse_iframe_when_top_form_absent(monkeypatch):
    filled = []
    monkeypatch.setattr(ats, "_human_type", lambda loc, txt, **kw: filled.append(txt))
    monkeypatch.setattr(ats, "_fill_common_custom_fields", lambda scope: None)
    monkeypatch.setattr(ats, "_answer_custom_questions", lambda scope, jd, zero_llm_mode=False: None)
    monkeypatch.setattr(ats, "_detect_captcha", lambda scope: False)
    monkeypatch.setattr(ats, "_is_job_closed", lambda scope: (False, ""))

    frame_inputs = MagicMock()
    frame_inputs.count.return_value = 1

    frame = MagicMock(spec=["wait_for_selector", "locator"])
    frame.locator.return_value = frame_inputs

    iframe_handle = MagicMock()
    iframe_handle.content_frame.return_value = frame

    iframe_loc = MagicMock()
    iframe_loc.count.return_value = 1
    iframe_loc.first.element_handle.return_value = iframe_handle

    no_match = MagicMock()
    no_match.count.return_value = 0

    page = MagicMock()
    page.url = "https://example.com/careers?gh_jid=999"

    def page_locator(sel):
        if "iframe#grnhse_iframe" == sel:
            return iframe_loc
        return no_match

    page.locator.side_effect = page_locator

    ok, notes = ats._apply_greenhouse(
        page,
        jd={"title": "Backend Engineer", "company": "ExampleCo"},
        pdf_path="dummy.pdf",
        dry_run=True,
        zero_llm_mode=True,
    )

    assert ok is True
    assert "Dry run" in notes
    frame.wait_for_selector.assert_called_once()
    assert ats.USER_PROFILE["first_name"] in filled
    assert ats.USER_PROFILE["email"] in filled


def test_classify_apply_status_and_viable_url_case_insensitivity():
    assert pipeline.classify_apply_status(True, "Dry run ok", True, "apply_success_tailored") == "dry_run_success"
    assert pipeline.classify_apply_status(True, "Confirmation URL reached", False, "apply_success_tailored") == "apply_success_tailored"
    assert pipeline.classify_apply_status(True, "Confirmation URL reached", False, "apply_success_base") == "apply_success_base"
    assert pipeline.classify_apply_status(False, "requires_captcha: Cloudflare detected", False, "apply_success_base") == "requires_captcha"
    assert pipeline.classify_apply_status(False, "job_closed: Job 404 in page title", False, "apply_success_base") == "job_closed"
    assert pipeline.classify_apply_status(False, "RemoteOK redirect did not land on a supported ATS: https://foo.com", False, "apply_success_base") == "skipped_unsupported_ats"
    assert pipeline.classify_apply_status(False, "Submit button not found on Lever form", False, "apply_success_base") == "apply_failed"

    viable, _ = pipeline._is_viable_apply_url("HTTPS://Boards.Greenhouse.IO/acme/jobs/123", "greenhouse")
    assert viable is True


def test_run_pipeline_budget_and_tailor_daily_limit_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr("scripts.render_pdf.OUTPUT_DIR", tmp_path)
    monkeypatch.setenv("DRY_RUN", "false")

    saw_dry_run_during_fetch = []
    def fake_fetch_all():
        saw_dry_run_during_fetch.append(log_mod.os.environ.get("DRY_RUN"))
        return [
            {
                "title": "Unsupported Role",
                "company": "WorkdayCo",
                "location": "Remote",
                "remote": True,
                "description": "A" * 300,
                "apply_url": "https://workday.example.com/job/1",
                "source": "company",
            },
            {
                "title": "Junior AI Engineer",
                "company": "TargetAI",
                "location": "Remote",
                "remote": True,
                "description": "B" * 300,
                "apply_url": "https://boards.greenhouse.io/targetai/jobs/123456",
                "source": "greenhouse",
            },
        ]

    monkeypatch.setattr(pipeline, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(pipeline, "score_and_pick", lambda jd, zero_llm_mode=False: {
        "best_variant": "ai-engineer",
        "score": 80,
        "missing_skills": [],
        "reasoning": "Good AI fit",
        "rate_limit_tripped": False,
    })

    def fail_daily(*args, **kwargs):
        raise tailor.DailyRateLimitExceeded("TPD quota exceeded")

    monkeypatch.setattr(pipeline, "tailor_resume", fail_daily)
    monkeypatch.setattr(pipeline, "render_pdf", lambda data, company: str(tmp_path / f"{company}.pdf"))

    apply_calls = []
    def fake_apply(jd, pdf_path, dry_run=False, cover_letter="", use_persistent_profile=False, zero_llm_mode=False):
        apply_calls.append({"dry_run": dry_run, "zero_llm_mode": zero_llm_mode, "cover_letter": cover_letter})
        return True, "Confirmation URL reached"

    monkeypatch.setattr(pipeline, "apply", fake_apply)

    logged = []
    monkeypatch.setattr(pipeline, "log_result", lambda **kw: logged.append(kw))
    monkeypatch.setattr(pipeline, "send_daily_digest", lambda: None)
    monkeypatch.setattr("time.sleep", lambda s: None)

    monkeypatch.setattr("sys.argv", ["run_pipeline.py", "--max-jobs", "1", "--dry-run"])
    pipeline.main()

    # --dry-run sets DRY_RUN before fetch_all()
    assert saw_dry_run_during_fetch == ["true"]
    # Unsupported ATS skip did NOT consume max-jobs=1 budget — TargetAI still ran!
    assert len(apply_calls) == 1
    assert apply_calls[0]["dry_run"] is True
    assert apply_calls[0]["zero_llm_mode"] is True  # Immediately disabled LLM Q&A after DailyRateLimitExceeded!
    assert any(r["jd"]["company"] == "WorkdayCo" and r["status"] == "skipped_unsupported_ats" for r in logged)
    assert any(r["jd"]["company"] == "TargetAI" and r["status"] == "dry_run_success" for r in logged)

    # Also test live mode (non-dry-run) where tailor fallback logs apply_success_base, NOT apply_success_tailored
    logged.clear()
    apply_calls.clear()
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setattr("sys.argv", ["run_pipeline.py", "--max-jobs", "1"])
    pipeline.main()
    assert any(r["jd"]["company"] == "TargetAI" and r["status"] == "apply_success_base" for r in logged)

