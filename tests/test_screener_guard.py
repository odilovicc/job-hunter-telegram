from models.candidate import CandidateProfile
from services.job_pipeline import sanitize_screener_questions
from ai_generator import NEEDS_USER_INPUT


def make_candidate(**overrides):
    base = dict(salary_expectation="", work_authorization="")
    base.update(overrides)
    return CandidateProfile(**base)


def test_work_authorization_question_always_requires_confirmation():
    candidate = make_candidate()
    raw = [{"question": "Are you authorized to work in the United States?",
            "answer": "Yes", "confidence": 0.95, "requires_confirmation": False}]
    result = sanitize_screener_questions(raw, candidate)
    assert len(result) == 1
    assert result[0].requires_confirmation is True
    assert result[0].answer == ""  # AI-ответ на чувствительный вопрос всегда обнуляется


def test_visa_sponsorship_question_is_blocked_even_in_russian():
    candidate = make_candidate()
    raw = [{"question": "Нужна ли вам виза или спонсорство?", "answer": "Нет", "confidence": 0.9}]
    result = sanitize_screener_questions(raw, candidate)
    assert result[0].requires_confirmation is True
    assert result[0].answer == ""


def test_salary_question_blocked_when_not_configured():
    candidate = make_candidate(salary_expectation="")
    raw = [{"question": "What is your expected salary?", "answer": "$2000", "confidence": 0.8}]
    result = sanitize_screener_questions(raw, candidate)
    assert result[0].requires_confirmation is True
    assert result[0].answer == ""


def test_salary_question_allowed_when_configured():
    candidate = make_candidate(salary_expectation="$1500")
    raw = [{"question": "What is your expected salary?", "answer": "$1500", "confidence": 0.9,
            "requires_confirmation": False}]
    result = sanitize_screener_questions(raw, candidate)
    assert result[0].answer == "$1500"
    assert result[0].requires_confirmation is False


def test_non_sensitive_question_passes_through():
    candidate = make_candidate()
    raw = [{"question": "How many years of Vue.js experience do you have?",
            "answer": "3", "confidence": 0.9, "requires_confirmation": False}]
    result = sanitize_screener_questions(raw, candidate)
    assert result[0].answer == "3"
    assert result[0].requires_confirmation is False
    assert result[0].source == "ai"


def test_needs_user_input_marker_always_blocked_regardless_of_category():
    candidate = make_candidate()
    raw = [{"question": "How many years of Vue.js experience do you have?",
            "answer": NEEDS_USER_INPUT, "confidence": 0.0}]
    result = sanitize_screener_questions(raw, candidate)
    assert result[0].requires_confirmation is True
    assert result[0].answer == ""


def test_empty_question_is_skipped():
    candidate = make_candidate()
    raw = [{"question": "", "answer": "whatever"}]
    assert sanitize_screener_questions(raw, candidate) == []
