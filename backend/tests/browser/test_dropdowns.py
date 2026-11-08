"""Acceptance: the agent works out unfamiliar dropdowns by looking and trying, with no
widget-specific code. The committed value is read from what the site would submit."""

import pytest

from app.apply.agent import AdaptiveAgent
from tests.browser.fixtures import DROPDOWN_GOALS, DROPDOWNS
from tests.browser.policy import HeuristicDecider


@pytest.mark.parametrize("name", list(DROPDOWN_GOALS))
async def test_agent_operates_dropdown(page, site, name):
    cfg = DROPDOWNS[name]
    value, expected = DROPDOWN_GOALS[name]
    await page.goto(site.at(name))
    control = page.locator(f"#dd_{cfg['name']}")
    decider = HeuristicDecider()
    out = await AdaptiveAgent(page, decider=decider).operate_field(control, cfg["label"], value, "combobox")
    committed = await page.locator(f"input[name={cfg['name']}]").input_value()
    if expected:
        assert out.success, (out, decider.prompts[-1][-3000:] if decider.prompts else "")
        assert expected in committed.split("|")
    else:  # a widget that never keeps a value: bounded failure, nothing committed
        assert not out.success
        assert committed == ""
        assert out.steps <= 8
