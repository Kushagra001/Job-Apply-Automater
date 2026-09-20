"""
test_enrich_jd.py — Unit Tests for JD Enrichment Engine
========================================================
Tests native ATS resolution (Greenhouse, Lever, Ashby), universal Jina reader fallback,
HTML stripping, text capping, and passthrough behavior when descriptions are already detailed.
"""

from unittest.mock import MagicMock, patch
import pytest

from scripts.enrich_jd import (
    _strip_html,
    resolve_greenhouse,
    resolve_lever,
    resolve_ashby,
    resolve_jina_reader,
    enrich_jd,
    MAX_DESCRIPTION_CHARS,
)


def test_strip_html():
    raw = "<div><h1>Job Title</h1><p>We are hiring a <b>Backend Engineer</b>.</p><script>alert(1)</script></div>"
    cleaned = _strip_html(raw)
    assert "alert(1)" not in cleaned
    assert "Job Title We are hiring a Backend Engineer." in cleaned
    assert _strip_html("&amp; &lt;tag&gt;") == "& <tag>"
    assert _strip_html("") == ""


@patch("requests.get")
def test_resolve_greenhouse_success(mock_get):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "content": "<p>Build real-time distributed systems using Python and Docker.</p>"
    }
    mock_get.return_value = mock_resp

    url = "https://boards.greenhouse.io/vercel/jobs/54321"
    desc = resolve_greenhouse(url)
    assert "Build real-time distributed systems using Python and Docker." in desc
    assert mock_get.called
    assert "boards-api.greenhouse.io/v1/boards/vercel/jobs/54321" in mock_get.call_args[0][0]


@patch("requests.get")
def test_resolve_greenhouse_gh_jid_query(mock_get):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "content": "<p>Stripe custom domain greenhouse position.</p>"
    }
    mock_get.return_value = mock_resp

    url = "https://stripe.com/jobs/search?gh_jid=998877"
    desc = resolve_greenhouse(url)
    assert "Stripe custom domain greenhouse position." in desc
    assert "boards-api.greenhouse.io/v1/boards/stripe/jobs/998877" in mock_get.call_args[0][0]


@patch("requests.get")
def test_resolve_lever_success(mock_get):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "descriptionPlain": "Responsibilities: Develop REST and GraphQL APIs.",
        "additionalPlain": "Requirements: 1-2 years experience with TypeScript and PostgreSQL.",
    }
    mock_get.return_value = mock_resp

    url = "https://jobs.lever.co/linear/12345-abcde"
    desc = resolve_lever(url)
    assert "Responsibilities: Develop REST and GraphQL APIs." in desc
    assert "Requirements: 1-2 years experience with TypeScript and PostgreSQL." in desc
    assert "api.lever.co/v0/postings/linear/12345-abcde" in mock_get.call_args[0][0]


@patch("requests.get")
def test_resolve_ashby_success(mock_get):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "jobs": [
            {
                "id": "job-uuid-1",
                "title": "Software Engineer",
                "descriptionPlain": "Full description for Ashby job posting.",
            },
            {
                "id": "job-uuid-2",
                "title": "Unrelated",
                "descriptionPlain": "Other job.",
            }
        ]
    }
    mock_get.return_value = mock_resp

    url = "https://jobs.ashbyhq.com/retool/job-uuid-1"
    desc = resolve_ashby(url)
    assert "Full description for Ashby job posting." in desc


@patch("urllib.request.urlopen")
def test_resolve_jina_reader_success(mock_urlopen):
    sample_markdown = (
        "# Senior Software Engineer\n\n"
        "We are looking for a developer who loves building web apps.\n"
        "Tech stack: React, Python, PostgreSQL.\n"
        "Qualifications: Computer science degree, 1 year experience."
    )
    mock_resp = MagicMock()
    mock_resp.read.return_value = sample_markdown.encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp
    mock_urlopen.return_value = mock_resp

    desc = resolve_jina_reader("https://startup.com/careers/engineer")
    assert "Senior Software Engineer" in desc
    assert "Tech stack: React, Python, PostgreSQL." in desc


@patch("urllib.request.urlopen")
def test_resolve_jina_reader_blocks_cloudflare_and_404(mock_urlopen):
    mock_resp = MagicMock()
    mock_resp.read.return_value = b"Warning: target URL returned error 404\n\n### Page Not Found"
    mock_resp.__enter__.return_value = mock_resp
    mock_urlopen.return_value = mock_resp

    desc = resolve_jina_reader("https://startup.com/dead-link")
    assert desc == ""


def test_enrich_jd_passthrough_when_already_detailed():
    long_desc = "X" * 300
    jd = {
        "title": "Backend Dev",
        "company": "Acme",
        "apply_url": "https://boards.greenhouse.io/acme/jobs/123",
        "description": long_desc,
    }
    enriched = enrich_jd(jd)
    assert enriched["description"] == long_desc


@patch("scripts.enrich_jd.resolve_greenhouse")
def test_enrich_jd_triggers_when_sparse(mock_greenhouse):
    mock_greenhouse.return_value = "Detailed requirements: Python, FastAPI, Docker, and 1 year experience."

    sparse_jd = {
        "title": "Backend Dev",
        "company": "Acme",
        "apply_url": "https://boards.greenhouse.io/acme/jobs/123",
        "description": "Short summary",
    }
    enriched = enrich_jd(sparse_jd)
    assert "Detailed requirements: Python, FastAPI, Docker" in enriched["description"]


@patch("scripts.enrich_jd.resolve_greenhouse")
def test_enrich_jd_caps_excessive_length(mock_greenhouse):
    mock_greenhouse.return_value = "W" * 5000

    sparse_jd = {
        "title": "Backend Dev",
        "company": "Acme",
        "apply_url": "https://boards.greenhouse.io/acme/jobs/123",
        "description": "Short summary",
    }
    enriched = enrich_jd(sparse_jd)
    assert len(enriched["description"]) == MAX_DESCRIPTION_CHARS + 3
    assert enriched["description"].endswith("...")
