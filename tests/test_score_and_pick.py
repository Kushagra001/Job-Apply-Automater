import pytest
from unittest.mock import patch, MagicMock
from scripts.score_and_pick import score_and_pick

@patch("scripts.score_and_pick.score_jd_against_all_variants")
def test_pick_best_variant_success(mock_score_all):
    mock_score_all.return_value = {
        "best_variant": "backend-python",
        "score": 90,
        "missing_skills": [],
        "reasoning": "Strong Python/Django match"
    }

    jd = {"title": "Python Developer", "description": "Need Python Django."}
    variants = {
        "frontend": {"basics": {"label": "Frontend Engineer"}},
        "backend-python": {"basics": {"label": "Python Backend Developer"}},
        "ai-engineer": {"basics": {"label": "AI Engineer"}},
    }

    with patch("scripts.score_and_pick.load_all_variants", return_value=variants):
        result = score_and_pick(jd)

        assert result is not None
        assert result["score"] == 90
        assert result["best_variant"] == "backend-python"
        assert result.get("status") != "skipped"

@patch("scripts.score_and_pick.score_jd_against_all_variants")
def test_pick_best_variant_below_threshold(mock_score_all):
    mock_score_all.return_value = {
        "best_variant": "frontend",
        "score": 40,
        "missing_skills": ["rust", "systems"],
        "reasoning": "Candidate lacks Rust expertise"
    }

    jd = {"title": "Senior Rust Developer"}
    variants = {"frontend": {"basics": {"label": "Frontend"}}}

    with patch("scripts.score_and_pick.load_all_variants", return_value=variants):
        result = score_and_pick(jd)
        assert result.get("status") == "skipped"
        assert result["score"] == 40
