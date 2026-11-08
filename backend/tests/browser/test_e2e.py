"""Acceptance: the real worker completes an unfamiliar multi-step application (single-page app,
no URL changes, a marketing popup, a plain-div searchable dropdown the page reader can't see,
a keyboard-only dropdown, a nested dropdown, a slow autocomplete) with no site-specific code,
and submits exactly the owner's facts. The previous path is run on the same site for comparison."""

from tests.browser.conftest import chromium_path
from tests.browser.harness import EXPECTED_POST as EXPECTED
from tests.browser.harness import run_application


def _brief(r):
    keep = ("apply.page", "apply.fill_failures", "browser.blocked", "browser.recovery_succeeded", "apply.error",
            "apply.stopped", "browser.summary", "apply.submit_result", "browser.submit_guard")
    return r.outcome, r.counters, [e for e in r.events if e["type"] in keep][-25:]


async def test_full_application_with_browser_agent(monkeypatch, tmp_path, site):
    r = await run_application(monkeypatch, tmp_path, site, chromium_path(), agent=True)
    assert r.outcome == "confirmed", _brief(r)
    assert r.submitted
    for k, v in EXPECTED.items():
        assert r.posted.get(k) == v, (k, r.posted)
    assert "mkt" not in r.posted  # never opted in to marketing
    assert not r.counters.takeovers and not r.counters.questions
    summary = next(e for e in r.events if e["type"] == "browser.summary")
    assert summary["field_fixed"] >= 2  # State (invisible to the page reader) + the keyboard-only dropdown


async def test_previous_path_on_same_site_never_claims_unverified_submit(monkeypatch, tmp_path, site):
    r = await run_application(monkeypatch, tmp_path, site, chromium_path(), agent=False)
    # Whatever it manages, it must not claim a submission without the confirmation page.
    if r.submitted:
        assert r.outcome in ("confirmed", "too_many_steps", "stopped", "error")
    assert not any(e["type"].startswith("browser.") for e in r.events)  # flag off = old behavior only
