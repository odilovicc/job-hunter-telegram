import os
import tempfile

import pytest

from database import Database
from models.job import Job
from services.deduplication import job_dedup_key


@pytest.fixture
def db():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "test_bot_state.db")
        instance = Database(db_name=db_path)
        yield instance
        instance.conn.close()


def _job(source="indeed", external_id="job-1", **kwargs):
    defaults = dict(title="Frontend Developer", company="Acme", location="Tashkent",
                     url="https://example.com/1", description="Vue, Nuxt, remote")
    defaults.update(kwargs)
    return Job(source=source, external_id=external_id, **defaults)


def test_new_tables_are_created(db):
    tables = {row[0] for row in db.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    assert {"jobs", "applications", "application_answers"} <= tables


def test_upsert_job_is_idempotent_by_dedup_key(db):
    job = _job()
    key = job_dedup_key(job)

    job_id_1, is_new_1 = db.upsert_job(
        dedup_key=key, source=job.source, external_id=job.external_id, title=job.title,
        company=job.company, location=job.location, url=job.url, description=job.description,
        score=None, status="discovered",
    )
    job_id_2, is_new_2 = db.upsert_job(
        dedup_key=key, source=job.source, external_id=job.external_id, title=job.title,
        company=job.company, location=job.location, url=job.url, description=job.description,
        score=7, status="discovered",
    )

    assert is_new_1 is True
    assert is_new_2 is False
    assert job_id_1 == job_id_2

    row = db.get_job_by_id(job_id_1)
    assert row["score"] == 7  # score обновляется даже при повторном upsert


def test_job_status_transitions_and_application_lifecycle(db):
    job = _job()
    key = job_dedup_key(job)
    job_id, _ = db.upsert_job(
        dedup_key=key, source=job.source, external_id=job.external_id, title=job.title,
        company=job.company, location=job.location, url=job.url, description=job.description,
    )

    db.update_job_score_status(job_id, 8, "qualified")
    application_id = db.create_application(job_id, status="ready", resume_name="frontend", cover_letter="Hi!")
    db.update_application_status(application_id, "applied", mark_applied=True)
    db.set_job_status(job_id, "applied")

    job_row = db.get_job_by_id(job_id)
    app_row = db.get_application(application_id)

    assert job_row["status"] == "applied"
    assert job_row["score"] == 8
    assert app_row["status"] == "applied"
    assert app_row["resume_name"] == "frontend"


def test_get_source_stats_counts_by_source(db):
    telegram_job = _job(source="telegram", external_id="1:100")
    indeed_job = _job(source="indeed", external_id="job-2")

    for job, outcome in ((telegram_job, "qualified"), (indeed_job, "qualified")):
        key = job_dedup_key(job)
        job_id, _ = db.upsert_job(
            dedup_key=key, source=job.source, external_id=job.external_id, title=job.title,
            company=job.company, location=job.location, url=job.url, description=job.description,
            score=5, status=outcome,
        )
        if job.source == "indeed":
            app_id = db.create_application(job_id, status="applied", resume_name="default", cover_letter="x")
            db.update_application_status(app_id, "applied", mark_applied=True)

    stats = db.get_source_stats()
    assert stats["telegram"]["found"] == 1
    assert stats["telegram"]["qualified"] == 1
    assert stats["telegram"]["applied"] == 0
    assert stats["indeed"]["found"] == 1
    assert stats["indeed"]["qualified"] == 1
    assert stats["indeed"]["applied"] == 1


def test_screener_answers_are_saved(db):
    job = _job()
    key = job_dedup_key(job)
    job_id, _ = db.upsert_job(
        dedup_key=key, source=job.source, external_id=job.external_id, title=job.title,
        company=job.company, location=job.location, url=job.url, description=job.description,
    )
    application_id = db.create_application(job_id, status="ready", resume_name="default", cover_letter="x")
    db.save_screener_answers(application_id, [
        {"question": "Years of Vue?", "answer": "3", "confidence": 0.9,
         "source": "ai", "requires_confirmation": False},
        {"question": "Work authorization?", "answer": "", "confidence": 0.0,
         "source": "policy:needs_confirmation", "requires_confirmation": True},
    ])
    rows = db.conn.execute(
        "SELECT question, answer, requires_confirmation FROM application_answers WHERE application_id = ?",
        (application_id,)
    ).fetchall()
    assert len(rows) == 2
    assert any(r[2] == 1 for r in rows)


def test_legacy_tables_untouched_for_backward_compatibility(db):
    # Старые таблицы должны продолжать работать ровно как до Job-абстракции.
    db.mark_processed(1, -100123)
    assert db.is_processed(1, -100123) is True
    db.save_pending(1, -100123, "hr_username", "letter text")
    pending = db.get_pending(1, -100123)
    assert pending == {"author": "hr_username", "cover_letter": "letter text"}
