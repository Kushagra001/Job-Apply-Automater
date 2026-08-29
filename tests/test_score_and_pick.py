import pytest
from unittest.mock import patch, MagicMock
from scripts.score_and_pick import score_and_pick

@patch("scripts.score_and_pick.score_jd_against_variant")
def test_pick_best_variant_success(mock_score_variant):
    # Setup the mock to return different scores for different variants
    def mock_score(jd, variant):
        role = variant.get("basics", {}).get("label", "")
        if "Frontend" in role:
            return {"score": 50, "missing_skills": [], "reasoning": "Not a match"}
        elif "Python" in role:
            return {"score": 90, "missing_skills": [], "reasoning": "Perfect match"}
        return {"score": 60, "missing_skills": [], "reasoning": "Okay match"}
    
    mock_score_variant.side_effect = mock_score

    jd = {"title": "Python Developer", "description": "Need Python Django."}
    
    # We pass in mock variants instead of reading from disk
    variants = {
        "frontend": {"basics": {"label": "Frontend Engineer"}},
        "backend-python": {"basics": {"label": "Python Backend Developer"}},
        "ai-engineer": {"basics": {"label": "AI Engineer"}},
    }
    
    # Patch the glob load to just return our mock variants
    with patch("scripts.score_and_pick.load_all_variants", return_value=variants):
        result = score_and_pick(jd)
        
        assert result is not None
        assert result["score"] == 90
        assert result["best_variant"] == "backend-python"

@patch("scripts.score_and_pick.score_jd_against_variant")
def test_pick_best_variant_below_threshold(mock_score_variant):
    # Setup mock to return scores below 65
    mock_score_variant.return_value = {"score": 40, "missing_skills": ["everything"], "reasoning": "No"}
    
    jd = {"title": "Senior Rust Developer"}
    variants = {"frontend": {"basics": {"label": "Frontend"}}}
    
    with patch("scripts.score_and_pick.load_all_variants", return_value=variants):
        result = score_and_pick(jd)
        assert result.get("status") == "skipped"
