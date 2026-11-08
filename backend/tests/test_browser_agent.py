"""Unit tests for the browser agent's pieces (no browser needed): progress detection, snapshot
compaction, action validation and safety, failure classification, working memory, planner
"other_controls" validation, and the feature flag default."""

import pytest

from app.apply.agent import AgentOutcome, WorkingMemory, _site_error
from app.apply.observe import Observation, ProgressTracker, Shot, compact_snapshot, compare, parse_refs
from app.apply.page import PageState
from app.apply.planner import check_plan, plan_schema, system_prompt
from app.apply.tools import ActionResult, Cat, Policy, classify, click_refusal, matches_value
from app.settings_store import AutomationSettings


def st(**kw):
    base = {"url": "https://x.example/apply#a", "title": "Apply", "headings": ["Contact"], "step": ["1. Contact"],
            "dialogs": [], "expanded": [], "options": [], "errors": [], "highlighted": [],
            "fields": [["e1", "", 0], ["e2", "", 0]], "focus": None}
    base.update(kw)
    return base


# ---- progress detection ----

def test_filling_a_field_is_progress_even_when_url_labels_and_layout_stay_the_same():
    ch = compare(st(), st(fields=[["e1", "Nathan", 0], ["e2", "", 0]]))
    assert ch.meaningful and not ch.page_level and ch.values_changed == ["e1"]


def test_step_change_without_url_change_is_page_level_progress():
    ch = compare(st(), st(step=["2. Questions"], headings=["Questions"]))
    assert ch.page_level and ch.meaningful


def test_dialog_validation_and_new_sections_count():
    assert compare(st(), st(dialogs=["Join our talent community"])).page_level
    assert compare(st(), st(errors=["Phone is required"])).meaningful
    assert compare(st(errors=["Phone is required"]), st()).meaningful  # error cleared
    assert compare(st(), st(fields=[["e1", "", 0], ["e2", "", 0], ["e3", "", 0]])).page_level  # "Add" revealed
    assert compare(st(), st(fields=[["e1", "", 1], ["e2", "", 0]])).meaningful  # became invalid


def test_opening_a_menu_is_an_effect_but_not_progress():
    ch = compare(st(), st(expanded=["State"], options=["Ohio", "Oregon"]))
    assert ch.any and not ch.meaningful


def test_fragment_changes_and_body_text_are_not_progress():
    # url fragment, clocks and banners aren't part of the state at all
    assert not compare(st(), st(url="https://x.example/apply#b")).any


def test_tracker_counts_stalls_and_resets_on_progress():
    t = ProgressTracker()
    t.update(st())
    t.update(st())
    t.update(st())
    assert t.stalled == 2
    t.update(st(fields=[["e1", "x", 0], ["e2", "", 0]]))
    assert t.stalled == 0


# ---- snapshot compaction / refs ----

SNAP = """- generic [active] [ref=e1]:
  - generic [ref=e2]:
    - heading "Apply" [level=1] [ref=e3] [box=8,8,100,20]
    - generic [ref=e4]:
      - textbox "Name" [ref=e5] [box=46,66,185,21]
  - combobox "Country" [ref=e6]:
""" + "".join(f'    - option "C{i}"\n' for i in range(40)) + """  - listbox [ref=e7]:
    - option "Oregon" [ref=e8] [box=21,137,390,28]
"""


def test_compaction_drops_wrappers_and_long_native_option_lists_but_keeps_refs():
    out = compact_snapshot(SNAP, keep_boxes=True)
    assert "generic [ref=e2]" not in out and "generic [ref=e4]" not in out
    assert 'textbox "Name" [ref=e5]' in out and "[box=46,66,185,21]" in out
    assert out.count('option "C') == 12 and "(more options...)" in out
    assert 'option "Oregon" [ref=e8]' in out


def test_compaction_keeps_popups_when_over_budget():
    big = "\n".join(f'- text: filler line {i} ' + "x" * 60 for i in range(300)) + "\n- listbox [ref=e9]:\n" \
          '  - option "Oregon" [ref=e10]'
    out = compact_snapshot(big, max_chars=2000)
    assert 'option "Oregon" [ref=e10]' in out and "not shown" in out and len(out) < 2600


def test_parse_refs_reads_roles_names_states_and_boxes():
    refs = parse_refs('- combobox "State" [expanded] [ref=e4] [box=8,87.5,264,18]: Select One\n')
    assert refs["e4"]["role"] == "combobox" and refs["e4"]["name"] == "State"
    assert refs["e4"]["box"] == [8, 88, 264, 18] and "expanded" in refs["e4"]["states"]


def test_screenshot_coordinates_map_back_to_css_pixels():
    crop = Shot(b"", "crop", clip_x=100, clip_y=200, scale=2.0, width=400, height=300)
    assert crop.to_css(50, 40) == (125, 220)
    assert crop.contains(399, 299) and not crop.contains(401, 10)


# ---- values and safety ----

def test_matches_value_is_word_based_and_never_substitutes():
    assert matches_value("Oregon (OR)", ["Oregon"])
    assert matches_value("Corporate Website", ["Career Websites > Corporate Website"])
    assert matches_value("United States of America", ["USA"])
    assert not matches_value("Not applicable", ["No"])
    assert not matches_value("Ohio", ["Oregon"])
    assert matches_value("Oregon (US state)", ["Oregon"])  # the option contains the value as words


def test_field_mode_only_types_text_from_the_approved_value():
    p = Policy(mode="field", values=["Oregon"])
    assert p.text_ok("Oregon") and p.text_ok("Ore") and p.text_ok("")
    assert not p.text_ok("123-45-6789") and not p.text_ok("Washington")
    phone = Policy(mode="field", values=["(503) 555-0100"])
    assert phone.text_ok("5035550100")  # same digits, another format
    assert not Policy(mode="page").text_ok("anything")


def btn(text, **kw):
    return {"tag": "button", "type": kw.pop("type", "button"), "role": "", "text": text, "name": text, "label": "",
            "filled": kw.pop("filled", 0), **kw}


@pytest.mark.parametrize("text", ["Submit Application", "Finish", "Send application", "Complete my application"])
def test_agent_never_presses_final_submit(text):
    assert click_refusal(btn(text), Policy(mode="page"))


@pytest.mark.parametrize("text", ["Delete my account", "Withdraw application", "Cancel", "Sign in with LinkedIn",
                                  "Log out"])
def test_agent_never_presses_destructive_or_social_buttons(text):
    assert click_refusal(btn(text), Policy(mode="page"))


def test_apply_on_filled_form_and_form_submits_in_field_mode_are_refused():
    assert click_refusal(btn("Apply", filled=4), Policy(mode="page"))
    assert click_refusal(btn("Apply", filled=0), Policy(mode="page")) is None
    assert click_refusal(btn("Go", type="submit"), Policy(mode="field", values=["x"]))
    assert click_refusal(btn("Next", type="submit"), Policy(mode="page")) is None


def test_marketing_checkboxes_and_unapproved_options_are_refused():
    box = {"tag": "input", "type": "checkbox", "is_check": True, "checked": False,
           "label": "Text me about other job openings"}
    assert "marketing" in click_refusal(box, Policy(mode="field", values=["check"], checkbox_ok=True))
    agree = {**box, "label": "I certify the information is true and complete"}
    assert click_refusal(agree, Policy(mode="page")) is None
    other = {**box, "label": "Remember me"}
    assert click_refusal(other, Policy(mode="page"))
    opt = {"tag": "div", "role": "option", "is_option": True, "text": "Ohio", "name": "Ohio"}
    assert click_refusal(opt, Policy(mode="field", values=["Oregon"]))
    assert click_refusal({**opt, "text": "Oregon"}, Policy(mode="field", values=["Oregon"])) is None


def test_failure_classification():
    assert classify(Exception("<div> intercepts pointer events"))[0] == Cat.OBSCURED
    assert classify(Exception("element is not enabled"))[0] == Cat.DISABLED
    assert classify(Exception("Element is not attached to the DOM"))[0] == Cat.DETACHED
    assert classify(Exception("Execution context was destroyed"))[0] == Cat.DETACHED
    assert classify(TimeoutError("Timeout 5000ms exceeded"))[0] == Cat.UNKNOWN


def test_action_result_feedback_has_hint_and_changes():
    r = ActionResult("click", target='option "Yes"', executed=True, category=Cat.NOT_PERSISTED,
                     error="only highlighted")
    out = r.for_model()
    assert out["ok"] is False and out["error_category"] == "selection_did_not_persist"
    assert "Enter" in out["hint"] and out["what_changed"] == ["nothing visible changed"]


# ---- memory ----

class _Res:
    def __init__(self, ok, cat="", err="", target='option "Yes"'):
        self.ok, self.category, self.error, self.changes, self.target = ok, cat, err, [], target


def _obs(refs):
    return Observation(url="", title="", snapshot="", refs=refs, state={})


def test_memory_bans_an_action_after_two_identical_failures_even_with_new_refs():
    m = WorkingMemory(objective="Set State")
    a1, o1 = {"type": "click", "ref": "e5"}, _obs({"e5": {"role": "option", "name": "Yes"}})
    a2, o2 = {"type": "click", "ref": "e9"}, _obs({"e9": {"role": "option", "name": "Yes"}})  # re-rendered
    m.record(a1, o1, _Res(False, Cat.NOT_PERSISTED, "only highlighted"))
    assert not m.banned(a2, o2)
    m.record(a2, o2, _Res(False, Cat.NOT_PERSISTED, "only highlighted"))
    assert m.banned({"type": "click", "ref": "e11"}, _obs({"e11": {"role": "option", "name": "Yes"}}))
    assert not m.banned({"type": "press_key", "key": "Enter"}, o2)
    s = m.summary()
    assert "Current objective: Set State" in s and "FAILED (selection_did_not_persist)" in s
    assert "Do NOT repeat" in s


def test_memory_summary_stays_compact():
    m = WorkingMemory()
    for i in range(50):
        m.record({"type": "scroll", "ref": f"e{i}"}, _obs({}), _Res(True, target=f"t{i}"))
    assert m.summary(recent=10).count("\n") < 14


def test_site_error_pages_are_recognized():
    assert _site_error(Observation(url="", title="", snapshot="", refs={},
                                   state={"headings": ["500 Internal Server Error"]}))
    assert not _site_error(Observation(url="", title="Apply", snapshot="", refs={}, state={"headings": ["Apply"]}))


# ---- planner: other_controls + nested paths, behind the flag ----

def _state(text, fields=None):
    return PageState(frames=[{"url": "u", "title": "t", "headings": [], "errors": [], "text": text,
                              "fields": fields or [], "buttons": []}])


def test_other_controls_need_the_label_on_the_page_and_a_real_fact():
    state = _state("State * Select... Why do you want to work here? Salary expectations")
    raw = {"page_kind": "application_form", "fills": [], "other_controls": [
        {"label": "State", "value": "Oregon", "fact_ids": [3], "write": False},
        {"label": "Shoe size", "value": "10", "fact_ids": [3], "write": False},  # not on the page
        {"label": "Why do you want to work here?", "value": "I like the mission.", "fact_ids": [], "write": True},
        {"label": "Salary expectations", "value": "$90,000", "fact_ids": [], "write": True},  # not low-stakes
        {"label": "State", "value": "Oregon", "fact_ids": [999], "write": False},  # unknown fact
    ]}
    plan = check_plan(raw, state, {3})
    labels = [(w.label, w.value, w.written) for w in plan.widgets]
    assert ("State", "Oregon", False) in labels
    assert ("Why do you want to work here?", "I like the mission.", True) in labels
    assert not any(w.label in ("Shoe size", "Salary expectations") for w in plan.widgets)
    assert len([w for w in plan.widgets if w.label == "State"]) == 1


def test_nested_dropdown_answer_keeps_its_full_path():
    f = {"id": "e1", "kind": "combobox", "label": "How did you hear about us?", "required": True, "value": "",
         "options": ["Job Board", "Career Websites", "Referral"]}
    raw = {"page_kind": "application_form", "fills": [
        {"field_id": "0:e1", "action": "select", "value": "Career Websites > Corporate Website", "fact_ids": [1]}]}
    plan = check_plan(raw, _state("", [f]), {1})
    assert plan.actions[0].value == "Career Websites > Corporate Website"


def test_flag_is_off_by_default_and_off_means_the_old_planner_contract():
    assert AutomationSettings().browser_agent is False
    assert "other_controls" not in plan_schema(False)["properties"]
    assert "other_controls" in plan_schema(True)["required"]
    assert "other_controls" not in system_prompt(False) and "other_controls" in system_prompt(True)


def test_outcome_defaults():
    o = AgentOutcome(False)
    assert o.ai_calls == 0 and not o.fallback_used


# ---- how-to-apply choices (CHKD / HealthcareSource "Begin Apply Options", 2026-11-08) ----

def _apply_options(required=False, star=True):
    return {"id": "e67", "kind": "radio", "required": required, "value": "",
            "label": "Begin Apply Options" + (" *" if star else "") + " Manually enter information Prefill application from a resume",
            "options": [{"id": "e68", "label": "Manually enter information"},
                        {"id": "e69", "label": "Prefill application from a resume"}]}


def test_how_to_apply_choice_needs_no_fact():
    raw = {"page_kind": "application_form", "fills": [
        {"field_id": "0:e67", "action": "select", "value": "Manually enter information", "fact_ids": []}]}
    plan = check_plan(raw, _state("", [_apply_options()]), set())
    assert [(a.action, a.value, a.option_id) for a in plan.actions] == \
        [("select_radio", "Manually enter information", "e68")]
    assert not plan.questions


def test_how_to_apply_choice_is_picked_even_if_the_ai_skips_it_and_never_asked():
    raw = {"page_kind": "application_form", "fills": [],
           "questions": [{"field_id": "0:e67", "question": "How do you want to apply?", "options": []}]}
    plan = check_plan(raw, _state("", [_apply_options()]), set())
    assert [a.value for a in plan.actions] == ["Manually enter information"] and not plan.questions


def test_how_to_apply_never_picks_a_linkedin_import():
    f = _apply_options()
    f["options"] = [{"id": "a", "label": "Apply with LinkedIn"}, {"id": "b", "label": "Upload resume"}]
    raw = {"page_kind": "application_form", "fills": [
        {"field_id": "0:e67", "action": "select", "value": "Apply with LinkedIn", "fact_ids": []}]}
    plan = check_plan(raw, _state("", [f]), set())
    assert [a.value for a in plan.actions] == ["Upload resume"]


def test_asterisk_marks_a_field_required_so_it_is_asked_not_skipped():
    f = {"id": "e5", "kind": "text", "label": "Desired salary *", "required": False, "value": ""}
    plan = check_plan({"page_kind": "application_form", "fills": [],
                       "questions": [{"field_id": "0:e5", "question": "Desired salary?", "options": []}]},
                      _state("", [f]), set())
    assert [q.field_id for q in plan.questions] == ["0:e5"]


def test_dates_must_come_from_the_cited_facts():
    facts = [{"id": 1, "label": "CompTIA Security+ issue date", "value": "Jun 2025"},
             {"id": 2, "label": "Help Desk Technician dates", "value": "Nov 2024 - Present"}]
    f = {"id": "e1", "kind": "text", "label": "Date Expires: *", "required": True, "value": ""}
    raw = {"page_kind": "application_form", "fills": [
        {"field_id": "0:e1", "action": "fill", "value": "November 2027", "fact_ids": [2]}]}
    plan = check_plan(raw, _state("", [f]), {1, 2}, facts)
    assert not plan.actions and [q.field_id for q in plan.questions] == ["0:e1"]  # invented +3 years: asked
    g = {**f, "label": "Date Issued: *"}
    ok = {"page_kind": "application_form", "fills": [
        {"field_id": "0:e1", "action": "fill", "value": "06/2025", "fact_ids": [1]}]}
    assert [a.value for a in check_plan(ok, _state("", [g]), {1, 2}, facts).actions] == ["06/2025"]
