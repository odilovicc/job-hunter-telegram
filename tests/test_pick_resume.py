from services.job_pipeline import pick_resume


def test_pick_resume_uses_recommended_when_available():
    resumes = {"default": "/r/default.pdf", "frontend": "/r/frontend.pdf"}
    name, path = pick_resume("frontend", resumes)
    assert name == "frontend"
    assert path == "/r/frontend.pdf"


def test_pick_resume_falls_back_to_default_when_recommendation_unknown():
    resumes = {"default": "/r/default.pdf", "frontend": "/r/frontend.pdf"}
    name, path = pick_resume("backend", resumes)
    assert name == "default"
    assert path == "/r/default.pdf"


def test_pick_resume_falls_back_to_first_entry_without_default_key():
    resumes = {"frontend": "/r/frontend.pdf"}
    name, path = pick_resume(None, resumes)
    assert name == "frontend"
    assert path == "/r/frontend.pdf"


def test_pick_resume_handles_empty_resumes():
    name, path = pick_resume("frontend", {})
    assert name == "default"
    assert path == ""
