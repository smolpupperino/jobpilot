import pytest

from app.jobs_util import InvalidJobURL, canonicalize_url, detect_ats
from tests.conftest import H


def test_canonicalize_strips_tracking_but_keeps_job_ids():
    a = canonicalize_url("https://www.Example.com/jobs/123/?utm_source=linkedin&gh_jid=55&ref=x#apply")
    b = canonicalize_url("https://example.com/jobs/123?gh_jid=55")
    assert a == b == "https://example.com/jobs/123?gh_jid=55"


def test_canonicalize_rejects_non_http():
    with pytest.raises(InvalidJobURL):
        canonicalize_url("javascript:alert(1)")
    with pytest.raises(InvalidJobURL):
        canonicalize_url("file:///etc/passwd")


@pytest.mark.parametrize(
    "url,ats",
    [
        ("https://acme.wd5.myworkdayjobs.com/en-US/careers/job/123", "workday"),
        ("https://boards.greenhouse.io/acme/jobs/1", "greenhouse"),
        ("https://jobs.lever.co/acme/abc", "lever"),
        ("https://careers-acme.icims.com/jobs/1/job", "icims"),
        ("https://evil-myworkdayjobs.com.attacker.net/x", "unknown"),
        ("https://acme.com/careers", "unknown"),
    ],
)
def test_detect_ats(url, ats):
    assert detect_ats(url) == ats


def test_add_job_dedupes(owner):
    r = owner.post("/api/jobs", json={"url": "https://acme.wd1.myworkdayjobs.com/x/job/1?utm_source=li",
                                      "employer": "Acme", "title": "SOC Analyst"}, headers=H)
    assert r.status_code == 200
    job = r.json()
    assert job["ats"] == "workday" and job["status"] == "new"
    r2 = owner.post("/api/jobs", json={"url": "https://acme.wd1.myworkdayjobs.com/x/job/1/"}, headers=H)
    assert r2.status_code == 409
    assert r2.json()["detail"]["job_id"] == job["id"]
    assert len(owner.get("/api/jobs").json()) == 1


def test_status_change_is_logged(owner):
    job = owner.post("/api/jobs", json={"url": "https://example.com/j/9"}, headers=H).json()
    r = owner.patch(f"/api/jobs/{job['id']}", json={"status": "submitted_confirmed"}, headers=H)
    assert r.json()["status"] == "submitted_confirmed"
    events = owner.get(f"/api/jobs/{job['id']}").json()["events"]
    types = [e["type"] for e in events]
    assert types == ["job.added", "job.status_changed"]
    assert events[1]["data"] == {"before": "new", "after": "submitted_confirmed"}


def test_invalid_status_rejected(owner):
    job = owner.post("/api/jobs", json={"url": "https://example.com/j/10"}, headers=H).json()
    assert owner.patch(f"/api/jobs/{job['id']}", json={"status": "hired!!"}, headers=H).status_code == 400


def test_overview_counts_match(owner):
    for i in range(3):
        owner.post("/api/jobs", json={"url": f"https://example.com/j/{i}"}, headers=H)
    j = owner.get("/api/jobs").json()[0]
    owner.patch(f"/api/jobs/{j['id']}", json={"status": "skipped"}, headers=H)
    ov = owner.get("/api/overview?hours=24").json()
    assert ov["jobs_total"] == 3
    assert ov["new_jobs_in_window"] == 3
    assert ov["jobs_by_status"]["new"] == 2
    assert ov["jobs_by_status"]["skipped"] == 1
    assert ov["status_changes_in_window"] == {"skipped": 1}


def test_ttcportals_is_jobvite_and_tracking_stripped():
    from app.jobs_util import canonicalize_url, detect_ats

    url = (
        "https://firsttechfedcareers.ttcportals.com/jobs/18359216-cybersecurity-operations-analyst"
        "?__jvsd=LinkedIn&__jvst=Job+Board&bid=56&tm_company=48443&tm_event=view"
    )
    assert canonicalize_url(url) == (
        "https://firsttechfedcareers.ttcportals.com/jobs/18359216-cybersecurity-operations-analyst"
    )
    assert detect_ats(url) == "jobvite"
