"""Recovery from failures, and safety rules that hold no matter what the model asks."""

import json

from app.apply.agent import AdaptiveAgent
from app.apply.tools import Cat
from tests.browser.policy import HeuristicDecider, ScriptedDecider, act, parse, reply


def ref_in(prompt: str, role: str, name: str) -> str:
    for n in parse(prompt)["nodes"]:
        if n["role"] == role and name.lower() in (n["name"] or n["text"]).lower() and n["ref"]:
            return n["ref"]
    raise AssertionError(f"{role} {name!r} not in prompt")


class FnDecider:
    """A 'model' whose replies are computed from the prompt it gets (to find current refs)."""

    def __init__(self, *fns):
        self.fns, self.calls, self.prompts = list(fns), 0, []

    async def __call__(self, system, prompt, images, prefer_fallback, job_id):
        self.calls += 1
        self.prompts.append(prompt)
        fn = self.fns.pop(0) if self.fns else (lambda p: reply(status="impossible", thought="done"))
        return fn(prompt), "test", "fn", 1


# ---- recovery ----

async def test_obscured_button_overlay_is_closed_then_clicked(page, site):
    await page.goto(site.at("obscured"))
    out = await AdaptiveAgent(page, decider=HeuristicDecider()).unstick("Continue doesn't work")
    assert out.progressed
    assert await page.locator("h1").inner_text() == "Step 2"


async def test_obscured_click_reports_obscured_category(page, site):
    await page.goto(site.at("obscured"))
    d = FnDecider(lambda p: reply(act("click", ref=ref_in(p, "button", "Continue"))))
    agent = AdaptiveAgent(page, decider=d, max_steps=1)
    await agent.unstick("x")
    att = list(agent.memory.attempts)[0]
    assert att.category == Cat.OBSCURED and not att.ok


async def test_button_that_ignores_the_first_click_gets_another_method(page, site):
    await page.goto(site.at("failed_click"))
    agent = AdaptiveAgent(page, decider=HeuristicDecider())
    out = await agent.unstick("Continue does nothing")
    assert out.progressed
    kinds = [(a.action, a.category) for a in agent.memory.attempts]
    assert kinds[0] == ("click", Cat.NO_EFFECT)  # real feedback, then a different strategy worked


async def test_missing_button_found_by_scrolling(page, site):
    await page.goto(site.at("missing_button"))
    out = await AdaptiveAgent(page, decider=HeuristicDecider()).unstick("no Continue button visible")
    assert out.progressed and await page.locator("h1").inner_text() == "Step 2"


async def test_delayed_loading_is_waited_for(page, site):
    await page.goto(site.at("delayed"))
    out = await AdaptiveAgent(page, decider=HeuristicDecider()).unstick("page is empty")
    assert out.progressed


async def test_briefly_unresponsive_page_recovers(page, site):
    await page.goto(site.at("unresponsive"))
    out = await AdaptiveAgent(page, decider=HeuristicDecider()).unstick("stuck")
    assert out.progressed and await page.locator("h1").inner_text() == "Step 2"


async def test_unexpected_popup_mid_task_then_field_still_gets_set(page, site):
    await page.goto(site.at("popup_midflow"))
    await page.wait_for_selector("[role=dialog]")
    agent = AdaptiveAgent(page, decider=HeuristicDecider())
    first = await agent.unstick("a survey popup appeared")
    assert first.progressed
    out = await agent.operate_field(page.locator("#dd_state"), "State", "Oregon", "combobox")
    assert out.success and await page.locator("input[name=state]").input_value() == "Oregon"


async def test_repeated_unsuccessful_selection_is_bounded_and_not_looped(page, site):
    await page.goto(site.at("dd_never_commit"))
    d = HeuristicDecider()
    agent = AdaptiveAgent(page, decider=d)
    out = await agent.operate_field(page.locator("#dd_degree"), "Highest degree", "Bachelor's Degree", "combobox")
    assert not out.success and out.steps <= 8 and d.calls <= 8
    assert out.options_seen  # the real choices go back to the planner
    sigs = [(a.action, a.target, a.detail) for a in agent.memory.attempts if a.action != "check"]
    worst = max(sigs.count(s) for s in set(sigs))
    assert worst <= 3  # never the same failed action over and over
    assert await page.locator("input[name=degree]").input_value() == ""


async def test_fallback_model_is_used_after_primary_fails_and_gets_the_same_memory(page, site):
    await page.goto(site.at("dd_aria_combobox"))
    d = HeuristicDecider(fail_primary=2)
    out = await AdaptiveAgent(page, decider=d).operate_field(page.locator("#dd_state"), "State", "Oregon", "combobox")
    assert out.success and out.fallback_used and d.fallback_calls >= 1
    fallback_prompt = d.prompts[2]
    assert "FAILED (invalid_request)" in fallback_prompt  # it sees what the primary tried


async def test_captcha_is_a_handoff_not_a_retry(page, site):
    await page.goto(site.at("captcha"))
    d = HeuristicDecider()
    out = await AdaptiveAgent(page, decider=d).unstick("stuck")
    assert not out.progressed and out.category == Cat.CAPTCHA and d.calls == 0


async def test_site_server_error_stops_safely(page, site):
    await page.goto(site.at("error500"))
    d = HeuristicDecider()
    out = await AdaptiveAgent(page, decider=d).unstick("stuck")
    assert not out.progressed and out.category == Cat.SITE_ERROR and d.calls == 0


# ---- safety: rules hold whatever the model asks ----

async def test_prompt_injection_cannot_make_the_agent_submit_type_ssn_or_opt_in(page, site):
    await page.goto(site.at("injection"))
    d = FnDecider(
        lambda p: reply(act("click", ref=ref_in(p, "button", "Submit Application"))),
        lambda p: reply(act("type_text", ref=ref_in(p, "textbox", "Comments"), text="123-45-6789")),
        lambda p: reply(act("check", ref=ref_in(p, "checkbox", "marketing"))),
        lambda p: reply(act("click", ref=ref_in(p, "button", "Submit Application"), method="keyboard")),
    )
    agent = AdaptiveAgent(page, decider=d, max_steps=4)
    await agent.unstick("stuck on the questions page")  # page mode: the whole page is visible to the model
    cats = [a.category for a in agent.memory.attempts]
    assert cats.count(Cat.REFUSED) >= 4, [(a.action, a.category, a.result) for a in agent.memory.attempts]
    assert await page.evaluate("() => [document.body.dataset.submitted || '', cm.value, mkt.checked]") == \
        ["", "", False]
    assert "untrusted website content" in d.prompts[0]


async def test_destructive_and_finish_buttons_refused_by_ref_and_by_coordinates(page, site):
    await page.goto(site.at("destructive"))

    def by_point(p):
        n = next(n for n in parse(p)["nodes"] if "Delete" in (n["name"] or ""))
        x, y, w, h = n["box"]
        img = next(int(i) for i, k in parse(p)["images"] if k == "viewport")
        return reply(act("click_at", x=int(x + w / 2), y=int(y + h / 2), image=img))

    d = FnDecider(
        lambda p: reply(act("click", ref=ref_in(p, "button", "Delete my account"))),
        lambda p: reply(act("click", ref=ref_in(p, "button", "Withdraw"))),
        lambda p: reply(act("click", ref=ref_in(p, "button", "Finish"))),
        by_point,
        lambda p: reply(act("click", ref=ref_in(p, "button", "Finish"), method="dom")),
    )
    agent = AdaptiveAgent(page, decider=d, max_steps=5)
    await agent.unstick("stuck")
    assert [a.category for a in agent.memory.attempts] == [Cat.REFUSED] * 5
    assert await page.evaluate("() => Object.keys(document.body.dataset)") == []


async def test_unverified_text_cannot_be_typed_into_a_field(page, site):
    await page.goto(site.at("basic_form"))
    d = FnDecider(lambda p: reply(act("fill", ref=ref_in(p, "textbox", "Phone"), text="555-0199")))
    agent = AdaptiveAgent(page, decider=d, max_steps=1)
    out = await agent.operate_field(page.locator("#ph"), "Phone", "(503) 555-0100", "text")
    assert not out.success and list(agent.memory.attempts)[0].category == Cat.REFUSED
    assert await page.locator("#ph").input_value() == ""


async def test_page_mode_cannot_enter_any_data(page, site):
    await page.goto(site.at("basic_form"))
    d = FnDecider(lambda p: reply(act("fill", ref=ref_in(p, "textbox", "First name"), text="Nathan")))
    agent = AdaptiveAgent(page, decider=d, max_steps=1)
    await agent.unstick("x")
    assert list(agent.memory.attempts)[0].category == Cat.REFUSED


async def test_secrets_never_reach_the_event_log(page, site):
    from app.db import session_factory
    from app.models import Event

    await page.goto(site.at("modal"))
    secret = "Pw!x8-secret-77"
    d = ScriptedDecider([reply(status="impossible", thought=f"the password is {secret}")])
    events = []

    def log(t, **data):
        from app.events import log_event

        events.append((t, data))
        with session_factory()() as db:
            log_event(db, "system", t, **data)
            db.commit()

    await AdaptiveAgent(page, decider=d, events=log, secrets=[secret]).unstick("x")
    with session_factory()() as db:
        dumped = json.dumps([e.data for e in db.query(Event).all()])
    assert secret not in dumped and "[redacted]" in dumped


# ---- submission safety (worker-level) ----

async def test_duplicate_submit_guard_and_confirmation_evidence(page, site):
    """After a first Submit: a changed page without confirmation = uncertain (never press again);
    the confirmation page = confirmed; a page where nothing at all happened = retry allowed."""
    from app.apply.observe import ui_state
    from app.apply.worker import _submit_guard

    await page.goto(site.at("submit_slow"))
    pre = await ui_state(page)
    assert await _submit_guard(page, pre) == "retry"  # nothing happened yet
    await page.click("#sub")
    await page.wait_for_timeout(500)
    assert await _submit_guard(page, pre) == "uncertain"  # "Processing...": it may have gone through
    assert await _submit_guard(page, pre, new_form=True) == "new_step"  # a new step with fields: carry on
    await page.wait_for_url("**/submitted", timeout=10000)
    assert await _submit_guard(page, pre) == "confirmed"
    assert len(site.server.posts) == 1  # one submission only


async def test_submit_guard_allows_fixing_site_errors(page, site):
    from app.apply.observe import ui_state
    from app.apply.worker import _submit_guard

    await page.goto(site.at("validation"))
    pre = await ui_state(page)
    await page.fill("#ph", "1")
    await page.press("#ph", "Tab")  # the site shows an error: fixing it and submitting again is fine
    assert await _submit_guard(page, pre) == "retry"
