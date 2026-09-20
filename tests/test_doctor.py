"""
test_doctor.py — Unit Tests for Pre-Flight Doctor Diagnostics
=============================================================
Tests all doctor diagnostic probes: environment variables audit, Groq API test,
Google Sheets connectivity, Telegram bot handshake, Playwright headless launch,
endpoint reachability, and overall health status determination.
"""

import json
from unittest.mock import MagicMock, patch
import pytest

from scripts.doctor import (
    CheckResult,
    _check_env_vars,
    _check_resumes,
    _check_groq_api,
    _check_google_sheets,
    _check_telegram_bot,
    _check_playwright,
    _check_sourcing_endpoints,
    run_doctor,
)


def test_check_env_vars_complete(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test1234567890abcdef")
    monkeypatch.setenv("GOOGLE_SHEET_ID", "test_sheet_id_123")
    monkeypatch.setenv("GOOGLE_SA_JSON", json.dumps({"client_email": "test@serviceaccount.com"}))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:ABC-DEF")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "987654321")

    results = _check_env_vars()
    statuses = {r.name: r.status for r in results}

    assert statuses["Env: GROQ_API_KEY"] == "OK"
    assert statuses["Env: Google Sheets"] == "OK"
    assert statuses["Env: Telegram Alerts"] == "OK"


def test_check_env_vars_missing(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_SHEET_ID", raising=False)
    monkeypatch.delenv("GOOGLE_SA_JSON", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)

    results = _check_env_vars()
    statuses = {r.name: r.status for r in results}

    assert statuses["Env: GROQ_API_KEY"] == "FAIL"
    assert statuses["Env: Google Sheets"] == "FAIL"
    assert statuses["Env: Telegram Alerts"] == "WARN"


def test_check_resumes():
    result = _check_resumes()
    assert result.status == "OK"
    assert "core variant profiles valid" in result.message


@patch("groq.Groq")
def test_check_groq_api_success(mock_groq_cls, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test_key")
    mock_client = MagicMock()
    mock_resp = MagicMock()
    mock_resp.choices[0].message.content = "PONG"
    mock_client.chat.completions.create.return_value = mock_resp
    mock_groq_cls.return_value = mock_client

    res = _check_groq_api()
    assert res.status == "OK"
    assert "PONG" in res.message


@patch("groq.Groq")
def test_check_groq_api_failure(mock_groq_cls, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test_key")
    mock_client = MagicMock()
    mock_client.chat.completions.create.side_effect = Exception("API rate limit exceeded")
    mock_groq_cls.return_value = mock_client

    res = _check_groq_api()
    assert res.status == "FAIL"
    assert "failed" in res.message


def test_check_google_sheets_missing_creds(monkeypatch):
    monkeypatch.delenv("GOOGLE_SHEET_ID", raising=False)
    monkeypatch.delenv("GOOGLE_SA_JSON", raising=False)
    res = _check_google_sheets()
    assert res.status == "FAIL"


@patch("scripts.log_and_notify._get_sheet")
def test_check_google_sheets_success(mock_get_sheet, monkeypatch):
    monkeypatch.setenv("GOOGLE_SHEET_ID", "test_id")
    monkeypatch.setenv("GOOGLE_SA_JSON", "{}")
    mock_sheet = MagicMock()
    mock_sheet.spreadsheet.title = "Applications Tracker"
    mock_sheet.row_count = 150
    mock_get_sheet.return_value = mock_sheet

    res = _check_google_sheets()
    assert res.status == "OK"
    assert "Applications Tracker" in res.message


@patch("requests.get")
def test_check_telegram_bot_success(mock_get, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"result": {"username": "SkillHireBot"}}
    mock_get.return_value = mock_resp

    res = _check_telegram_bot()
    assert res.status == "OK"
    assert "@SkillHireBot" in res.message


@patch("playwright.sync_api.sync_playwright")
def test_check_playwright_success(mock_sync_pw):
    mock_pw = MagicMock()
    mock_browser = MagicMock()
    mock_page = MagicMock()
    mock_page.evaluate.return_value = 4
    mock_browser.new_page.return_value = mock_page
    mock_pw.chromium.launch.return_value = mock_browser
    mock_sync_pw.return_value.__enter__.return_value = mock_pw

    res = _check_playwright()
    assert res.status == "OK"
    assert "launched & evaluated JS" in res.message


@patch("requests.get")
def test_check_sourcing_endpoints_mock(mock_get):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_get.return_value = mock_resp

    results = _check_sourcing_endpoints()
    assert len(results) >= 8
    assert all(r.status == "OK" for r in results)


@patch("scripts.doctor._check_env_vars")
@patch("scripts.doctor._check_resumes")
@patch("scripts.doctor._check_groq_api")
@patch("scripts.doctor._check_google_sheets")
@patch("scripts.doctor._check_telegram_bot")
@patch("scripts.doctor._check_playwright")
@patch("scripts.doctor._check_sourcing_endpoints")
def test_run_doctor_healthy(
    mock_sourcing, mock_pw, mock_tg, mock_sheets, mock_groq, mock_resumes, mock_env
):
    mock_env.return_value = [CheckResult("Env", "OK", "Configured")]
    mock_resumes.return_value = CheckResult("Resumes", "OK", "All valid")
    mock_groq.return_value = CheckResult("Groq", "OK", "OK")
    mock_sheets.return_value = CheckResult("Sheets", "OK", "OK")
    mock_tg.return_value = CheckResult("Telegram", "OK", "OK")
    mock_pw.return_value = CheckResult("Playwright", "OK", "OK")
    mock_sourcing.return_value = [CheckResult("Endpoint", "OK", "OK")]

    healthy = run_doctor()
    assert healthy is True


@patch("scripts.doctor._check_env_vars")
@patch("scripts.doctor._check_resumes")
@patch("scripts.doctor._check_groq_api")
@patch("scripts.doctor._check_google_sheets")
@patch("scripts.doctor._check_telegram_bot")
@patch("scripts.doctor._check_playwright")
@patch("scripts.doctor._check_sourcing_endpoints")
def test_run_doctor_critical_failure(
    mock_sourcing, mock_pw, mock_tg, mock_sheets, mock_groq, mock_resumes, mock_env
):
    mock_env.return_value = [CheckResult("Env", "FAIL", "Missing key")]
    mock_resumes.return_value = CheckResult("Resumes", "OK", "All valid")
    mock_groq.return_value = CheckResult("Groq", "OK", "OK")
    mock_sheets.return_value = CheckResult("Sheets", "OK", "OK")
    mock_tg.return_value = CheckResult("Telegram", "OK", "OK")
    mock_pw.return_value = CheckResult("Playwright", "OK", "OK")
    mock_sourcing.return_value = [CheckResult("Endpoint", "OK", "OK")]

    healthy = run_doctor()
    assert healthy is False
