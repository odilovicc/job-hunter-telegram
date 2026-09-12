from models.job import Job
from services.deduplication import fingerprint, job_dedup_key


def test_fingerprint_is_stable_across_case_and_whitespace():
    a = fingerprint(company="Acme  Inc.", title="Frontend Developer", location="Tashkent")
    b = fingerprint(company="acme inc", title="frontend developer", location="  tashkent  ")
    assert a == b


def test_fingerprint_differs_for_different_jobs():
    a = fingerprint(company="Acme", title="Frontend Developer", location="Tashkent")
    b = fingerprint(company="Acme", title="Backend Developer", location="Tashkent")
    assert a != b


def test_job_dedup_key_prefers_external_id():
    job = Job(source="telegram", external_id="123:456", title="Vue dev")
    assert job_dedup_key(job) == "telegram:123:456"


def test_job_dedup_key_falls_back_to_fingerprint_when_no_stable_external_id():
    job1 = Job(source="indeed", external_id="", company="Acme", title="Frontend Developer", location="Tashkent")
    job2 = Job(source="indeed", external_id="", company="acme", title="frontend developer", location="tashkent")
    assert job_dedup_key(job1) == job_dedup_key(job2)
    assert job_dedup_key(job1).startswith("indeed:fp_")
