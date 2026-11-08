"""The browser action layer and the agent loop against real pages: basic form controls,
structured feedback, coordinate clicks (HiDPI + scrolled), dynamic pages, iframes."""

import pytest

from app.apply.agent import AdaptiveAgent
from app.apply.observe import observe
from app.apply.tools import BrowserTools, Cat, Policy
from tests.browser.conftest import chromium_path
from tests.browser.policy import HeuristicDecider, act, parse


def ref(obs, role, name):
    for r, info in obs.refs.items():
        if info["role"] == role and name.lower() in (info["name"] or info["text"]).lower():
            return r
    raise AssertionError(f"no {role} {name!r} in:\n{obs.snapshot}")


# ---- basic forms through the tool layer ----

async def test_basic_form_controls_with_structured_feedback(page, site, tmp_path):
    await page.goto(site.at("basic_form"))
    resume = tmp_path / "Resume.pdf"
    resume.write_bytes(b"%PDF-1.4\n%%EOF\n")
    obs = await observe(page)
    for name, text in (("First name", "Nathan"), ("Email", "a@example.com"), ("Phone", "5035550100")):
        tools = BrowserTools(page, policy=Policy(mode="field", values=[text]), resume=resume)
        r = await tools.run(act("fill", ref=ref(obs, "textbox", name), text=text), obs)
        assert r.ok and r.found and r.actionable and "value(s) changed" in " ".join(r.changes), r
    tools = BrowserTools(page, policy=Policy(mode="field", values=["United States"]), resume=resume)
    r = await tools.run(act("select_option", ref=ref(obs, "combobox", "Country"), option="United States"), obs)
    assert r.ok
    tools = BrowserTools(page, policy=Policy(mode="field", values=["Yes"]), resume=resume)
    r = await tools.run(act("check", ref=ref(obs, "radio", "Yes")), obs)
    assert r.ok
    tools = BrowserTools(page, policy=Policy(mode="field", values=["2026-12-01"]), resume=resume)
    r = await tools.run(act("fill", ref=ref(obs, "textbox", "start date"), text="2026-12-01"), obs)
    assert r.ok, r
    tools = BrowserTools(page, policy=Policy(mode="page"), resume=resume)
    r = await tools.run(act("check", ref=ref(obs, "checkbox", "privacy policy")), obs)
    assert r.ok
    r = await tools.run(act("check", ref=ref(obs, "checkbox", "marketing")), obs)
    assert not r.ok and r.category == Cat.REFUSED and not r.executed
    file_ref = ref(obs, "button", "Resume")  # a file input shows up as a button in the a11y tree
    r = await tools.run(act("upload_resume", ref=file_ref), obs)
    assert r.ok, r
    vals = await page.evaluate("() => [fn.value, em.value, ph.value, co.value, dt.value, "
                               "document.querySelector('[name=auth]:checked').value, agree.checked, mkt.checked, "
                               "cv.files.length]")
    assert vals == ["Nathan", "a@example.com", "5035550100", "United States", "2026-12-01", "yes", True, False, 1]


async def test_select_option_never_substitutes_a_different_option(page, site):
    await page.goto(site.at("dd_native"))
    obs = await observe(page)
    tools = BrowserTools(page, policy=Policy(mode="field", values=["Oregon"]))
    r = await tools.run(act("select_option", ref=ref(obs, "combobox", "State"), option="Ohio"), obs)
    assert r.category == Cat.REFUSED
    r = await tools.run(act("select_option", ref=ref(obs, "combobox", "State"), option="Oregn"), obs)
    assert r.category == Cat.OPTION_UNAVAILABLE and "Closest: Oregon" in r.error  # real options are reported


async def test_stale_refs_are_refused_not_guessed(page, site):
    await page.goto(site.at("add_section"))
    obs = await observe(page)
    tools = BrowserTools(page, policy=Policy(mode="page"))
    r = await tools.run(act("click", ref="e999"), obs)
    assert r.category == Cat.BAD_REQUEST and "latest observation" in r.error
    await page.evaluate("document.querySelector('#add').remove(); document.body.insertAdjacentHTML('beforeend', "
                        "'<button>Add work experience</button>')")
    r = await tools.run(act("click", ref=ref(obs, "button", "Add work")), obs)
    assert r.category == Cat.DETACHED  # the snapshot's element is gone: re-observe


async def test_inspect_options_lists_a_custom_dropdown(page, site):
    await page.goto(site.at("dd_portal"))
    obs = await observe(page)
    tools = BrowserTools(page, policy=Policy(mode="field", values=["Oregon"]))
    r = await tools.run(act("inspect_options", ref=ref(obs, "combobox", "State")), obs)
    assert r.ok and "Oregon" in r.data["options"] and r.reinspect


async def test_click_at_maps_screenshot_pixels_on_hidpi_scrolled_pages(browser, site):
    ctx = await browser.new_context(viewport={"width": 900, "height": 600}, device_scale_factor=2)
    page = await ctx.new_page()
    await page.goto(site.at("obscured"))
    await page.evaluate("document.getElementById('bar').remove(); window.scrollTo(0, 300)")
    obs = await observe(page, screenshot=True)
    shot = obs.shots[-1]
    assert shot.kind == "viewport" and shot.width == 900  # CSS pixels, not device pixels
    box = await page.locator("#cont").bounding_box()
    x, y = (box["x"] + box["width"] / 2 - shot.clip_x) * shot.scale, (box["y"] + box["height"] / 2) * shot.scale
    r = await BrowserTools(page, policy=Policy(mode="page")).run(act("click_at", x=int(x), y=int(y), image=len(obs.shots) - 1), obs)
    assert r.ok, r
    assert await page.locator("h1").inner_text() == "Step 2"
    await ctx.close()


async def test_crop_screenshot_maps_back_to_the_control(page, site):
    await page.goto(site.at("dd_aria_combobox"))
    control = page.locator("#dd_state")
    obs = await observe(page, control, screenshot=True)
    crop = obs.shots[0]
    assert crop.kind == "crop" and crop.width < 1100
    box = await control.bounding_box()
    cx, cy = crop.to_css((box["x"] - crop.clip_x + 10) * crop.scale, (box["y"] - crop.clip_y + 5) * crop.scale)
    assert abs(cx - box["x"] - 10) < 1 and abs(cy - box["y"] - 5) < 1


# ---- dynamic pages through the agent loop ----

async def test_modal_in_the_way_is_closed_without_opting_in(page, site):
    await page.goto(site.at("modal"))
    out = await AdaptiveAgent(page, decider=HeuristicDecider()).unstick("a popup covers the form")
    assert out.progressed
    assert await page.locator("[role=dialog]").count() == 0


async def test_add_button_reveals_a_new_section(page, site):
    await page.goto(site.at("add_section"))
    out = await AdaptiveAgent(page, decider=HeuristicDecider()).unstick("the work experience fields aren't shown")
    assert out.progressed and await page.locator("#jobs input").count() == 1


async def test_conditional_field_appears_after_choosing_a_radio(page, site):
    await page.goto(site.at("conditional"))
    yes = page.locator("input[name=vet][value=yes]")
    out = await AdaptiveAgent(page, decider=HeuristicDecider()).operate_field(yes, "Are you a veteran?", "Yes",
                                                                             "checkbox")
    assert out.success and await page.locator("#br").is_visible()


async def test_react_rerender_replaces_the_input_and_the_agent_refinds_it(page, site):
    await page.goto(site.at("rerender"))
    out = await AdaptiveAgent(page, decider=HeuristicDecider()).operate_field(page.locator("#ph"), "Phone",
                                                                             "5035550100", "text")
    assert out.success, out
    assert await page.locator("#ph").input_value() == "5035550100"


async def test_spa_step_change_without_url_change_is_progress(page, site):
    await page.goto(site.at("spa"))
    url = page.url
    out = await AdaptiveAgent(page, decider=HeuristicDecider()).unstick("need the next step")
    assert out.progressed and page.url == url
    assert await page.locator("h2").inner_text() == "Questions"


async def test_field_inside_a_nested_scroll_container(page, site):
    await page.goto(site.at("scroll_container"))
    out = await AdaptiveAgent(page, decider=HeuristicDecider()).operate_field(
        page.locator("#dd_degree"), "Highest degree", "Bachelor's Degree", "combobox")
    assert out.success and await page.locator("input[name=degree]").input_value() == "Bachelor's Degree"


async def test_cross_origin_iframe_form(page, site):
    await page.goto(site.at("iframe_host"))
    await page.wait_for_load_state("load")
    frame = next(f for f in page.frames if "iframe_inner" in f.url)
    assert frame.url.startswith("http://localhost")  # really another origin
    out = await AdaptiveAgent(page, decider=HeuristicDecider()).operate_field(
        frame.locator("#dd_state"), "State", "Oregon", "combobox")
    assert out.success, out
    assert await frame.locator("input[name=state]").input_value() == "Oregon"


async def test_site_validation_error_is_reported_back(page, site):
    await page.goto(site.at("validation"))
    obs = await observe(page)
    tools = BrowserTools(page, policy=Policy(mode="field", values=["(503) 555-0100"]))
    box = ref(obs, "textbox", "Phone")
    await tools.run(act("fill", ref=box, text="5035550100"), obs)
    r = await tools.run(act("press_key", key="Tab"), obs)
    assert any("new error" in c and "(555) 555-5555" in c for c in r.changes), r.changes
    obs = await observe(page)
    r = await tools.run(act("fill", ref=ref(obs, "textbox", "Phone"), text="(503) 555-0100"), obs)
    r = await tools.run(act("press_key", key="Tab"), obs)
    assert "error cleared" in r.changes


@pytest.mark.skipif(chromium_path() is None, reason="needs chromium")
async def test_observation_is_compact_and_has_refs(page, site):
    await page.goto(site.at("app_spa"))
    obs = await observe(page)
    assert len(obs.snapshot) < 9500 and obs.refs
    p = parse("GOAL\nx\n\nOBSERVATION\n" + obs.prompt_view())
    assert any(n["role"] == "textbox" for n in p["nodes"])


async def test_value_shown_but_rejected_by_the_site_is_retyped_key_by_key(page, site):
    await page.goto(site.at("keys_only"))
    await page.fill("#sal", "80000")  # what the fast path does: the box shows it...
    await page.locator("#sal").blur()
    assert await page.locator("#err").inner_text()  # ...but the site says it's empty
    out = await AdaptiveAgent(page, decider=HeuristicDecider()).operate_field(page.locator("#sal"), "Desired salary",
                                                                             "80000", "text")
    assert out.success, out
    assert await page.locator("#err").inner_text() == ""
