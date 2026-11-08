"""The plan checker is the safety layer between the AI and the browser."""

from app.apply.page import PageState
from app.apply.planner import check_plan


def _state(fields, buttons=(), text=""):
    return PageState(frames=[{"url": "https://x.test/apply", "title": "Apply", "headings": [], "errors": [],
                              "text": text, "fields": list(fields), "buttons": list(buttons)}])


FIRST = {"id": "e1", "kind": "text", "label": "First name", "required": True, "value": ""}
EMAIL = {"id": "e2", "kind": "email", "label": "Email", "required": True, "value": ""}
AUTH = {"id": "e3", "kind": "select", "label": "Authorized to work in the US?", "required": True,
        "value": "Select...", "options": ["Select...", "Yes", "No"]}
VET = {"id": "e4", "kind": "radio", "label": "Veteran status", "required": False, "value": "",
       "options": [{"id": "e5", "label": "I am a veteran"}, {"id": "e6", "label": "I am not a veteran"}]}
CV = {"id": "e7", "kind": "file", "label": "Resume", "required": True, "value": "", "accept": ".pdf"}
PW = {"id": "e8", "kind": "password", "label": "Create password", "required": True, "value": ""}
NEXT = {"id": "b1", "text": "Next", "type": "button"}
SUBMIT = {"id": "b2", "text": "Submit Application", "type": "submit"}
APPLY = {"id": "b3", "text": "Apply", "type": "button"}


def plan(fills=(), questions=(), btn="", kind="next", page_kind="application_form"):
    return {"page_kind": page_kind, "fills": list(fills), "questions": list(questions),
            "advance_button_id": btn, "advance_kind": kind, "blocker": "", "notes": ""}


def test_fill_backed_by_fact_is_accepted():
    cp = check_plan(plan([{"field_id": "0:e1", "action": "fill", "value": "Nathan", "fact_ids": [1]}]),
                    _state([FIRST]), {1})
    assert [(a.action, a.value) for a in cp.actions] == [("fill", "Nathan")]
    assert cp.questions == []


def test_fill_without_fact_becomes_question():
    cp = check_plan(plan([{"field_id": "0:e1", "action": "fill", "value": "Nathan", "fact_ids": []}]),
                    _state([FIRST]), {1})
    assert cp.actions == [] and cp.questions[0].field_id == "0:e1"


def test_fill_citing_unknown_fact_becomes_question():
    cp = check_plan(plan([{"field_id": "0:e1", "action": "fill", "value": "X", "fact_ids": [99]}]),
                    _state([FIRST]), {1})
    assert cp.actions == [] and len(cp.questions) == 1


def test_unknown_field_is_ignored():
    cp = check_plan(plan([{"field_id": "0:nope", "action": "fill", "value": "x", "fact_ids": [1]}]),
                    _state([]), {1})
    assert cp.actions == [] and "unknown field" in cp.problems[0]


def test_select_must_match_real_option():
    ok = check_plan(plan([{"field_id": "0:e3", "action": "select", "value": "yes", "fact_ids": [2]}]),
                    _state([AUTH]), {2})
    assert ok.actions[0].action == "select_option" and ok.actions[0].value == "Yes"
    bad = check_plan(plan([{"field_id": "0:e3", "action": "select", "value": "Maybe", "fact_ids": [2]}]),
                     _state([AUTH]), {2})
    assert bad.actions == [] and bad.questions[0].options == ["Yes", "No"]


def test_radio_maps_to_option_element():
    cp = check_plan(plan([{"field_id": "0:e4", "action": "select", "value": "I am not a veteran",
                           "fact_ids": [3]}]), _state([VET]), {3})
    assert cp.actions[0].action == "select_radio" and cp.actions[0].option_id == "e6"


def test_password_is_never_filled():
    cp = check_plan(plan([{"field_id": "0:e8", "action": "fill", "value": "hunter2", "fact_ids": [1]}]),
                    _state([PW]), {1})
    assert cp.actions == []


def test_resume_upload_needs_no_fact_but_only_on_file_inputs():
    cp = check_plan(plan([{"field_id": "0:e7", "action": "upload_resume", "value": "", "fact_ids": []},
                          {"field_id": "0:e1", "action": "upload_resume", "value": "", "fact_ids": []}]),
                    _state([CV, FIRST]), set())
    assert [a.action for a in cp.actions] == ["upload_resume"]


def test_required_empty_field_not_planned_is_asked():
    cp = check_plan(plan([]), _state([FIRST, EMAIL]), set())
    assert {q.field_id for q in cp.questions} == {"0:e1", "0:e2"}


def test_submit_detected_in_code_even_if_ai_says_next():
    cp = check_plan(plan(btn="0:b2", kind="next"), _state([], [SUBMIT]), set())
    assert cp.advance_kind == "submit"


def test_apply_button_on_filled_form_counts_as_submit():
    filled = [dict(FIRST, value="Nathan"), dict(EMAIL, value="n@x.com"), dict(AUTH, value="Yes")]
    cp = check_plan(plan(btn="0:b3", kind="next"), _state(filled, [APPLY]), set())
    assert cp.advance_kind == "submit"
    # ...but "Apply" on a job description page (no form) just starts the application.
    cp2 = check_plan(plan(btn="0:b3", kind="apply", page_kind="job_description"), _state([], [APPLY]), set())
    assert cp2.advance_kind == "apply"


def test_unknown_button_means_no_advance():
    cp = check_plan(plan(btn="0:zzz", kind="next"), _state([], [NEXT]), set())
    assert cp.advance_id is None and cp.advance_kind == "none"


TERMS = {"id": "c1", "kind": "checkbox", "label": "I agree to the Terms and Privacy Policy *", "required": True,
         "value": "unchecked"}
CERT = {"id": "c2", "kind": "checkbox", "label": "I certify that the information provided is true and complete.",
        "required": True, "value": "unchecked"}
MKT = {"id": "c3", "kind": "checkbox", "label": "Yes, send me marketing emails and job alerts", "required": False,
       "value": "unchecked"}
MKT_ON = dict(MKT, id="c4", value="checked")


def test_terms_and_certifications_are_agreed_without_asking():
    cp = check_plan(plan([], [{"field_id": "0:c1", "question": "Agree?", "options": []}]), _state([TERMS, CERT]),
                    set())
    assert {a.field_id for a in cp.actions if a.action == "check"} == {"0:c1", "0:c2"}
    assert cp.questions == []


def test_marketing_is_never_checked_and_is_unchecked_if_prechecked():
    fills = [{"field_id": "0:c3", "action": "check", "value": "", "fact_ids": []},
             {"field_id": "0:c4", "action": "uncheck", "value": "", "fact_ids": []}]
    cp = check_plan(plan(fills, [{"field_id": "0:c3", "question": "Want emails?", "options": []}]),
                    _state([MKT, MKT_ON]), set())
    assert [(a.field_id, a.action) for a in cp.actions] == [("0:c4", "uncheck")]
    assert cp.questions == []


def test_decline_and_continue_later_are_never_clicked():
    for text in ("I DECLINE", "CONTINUE LATER", "Cancel"):
        cp = check_plan(plan(btn="0:x", kind="next"), _state([], [{"id": "x", "text": text, "type": "button"}]),
                        set())
        assert cp.advance_id is None


def test_acknowledge_button_advances():
    cp = check_plan(plan(btn="0:a", kind="next", page_kind="other"),
                    _state([], [{"id": "a", "text": "I ACKNOWLEDGE AND CONFIRM", "type": "button"}]), set())
    assert cp.advance_kind == "next" and cp.advance_id == "0:a"


def test_best_option_matches_long_dropdown_choices():
    from app.apply.page import best_option

    opts = ["Yes, I will require sponsorship", "No, I will not need sponsorship"]
    assert best_option("No", opts) == 1
    assert best_option("yes, i will require sponsorship", opts) == 0
    assert best_option("Maybe", opts) is None
    assert best_option("No", ["No", "Not sure"]) == 0  # exact wins over prefix
    assert best_option("Oregon", ["Ohio", "Oklahoma", "Oregon"]) == 2


def test_write_allowed_only_for_low_stakes_questions():
    from app.apply.page import PageState
    from app.apply.planner import check_plan

    fields = [
        {"id": "a", "kind": "textarea", "label": "What had you heard about First Tech before applying?",
         "required": True, "value": ""},
        {"id": "b", "kind": "text", "label": "Desired salary", "required": True, "value": ""},
        {"id": "c", "kind": "textarea", "label": "Why did you apply for this position?",
         "required": True, "value": ""},
    ]
    state = PageState(frames=[{"fields": fields, "buttons": [], "text": ""}])
    raw = {"page_kind": "application_form", "clicks": [], "questions": [], "advance_button_id": "",
           "advance_kind": "none", "blocker": "", "notes": "",
           "fills": [{"field_id": f"0:{i}", "action": "write", "value": "Sounds great", "fact_ids": []}
                     for i in "abc"]}
    plan = check_plan(raw, state, set())
    written = {a.field_id for a in plan.actions if a.written}
    assert written == {"0:a", "0:c"}
    assert [q.field_id for q in plan.questions] == ["0:b"]  # salary is a real fact: asked


def test_i_agree_dropdown_is_agreed_not_asked():
    from app.apply.page import PageState
    from app.apply.planner import check_plan

    fields = [
        {"id": "a", "kind": "combobox", "label": "I hereby authorize First Tech to investigate my references",
         "required": True, "value": "", "options": ["I Agree"]},
        {"id": "b", "kind": "select", "label": "Send me job alerts and marketing", "required": False,
         "value": "", "options": ["I agree", "No thanks"]},
        {"id": "c", "kind": "select", "label": "Are you authorized to work in the US?", "required": True,
         "value": "", "options": ["Yes", "No"]},
    ]
    state = PageState(frames=[{"fields": fields, "buttons": [], "text": ""}])
    raw = {"page_kind": "application_form", "clicks": [], "fills": [], "advance_button_id": "",
           "advance_kind": "none", "blocker": "", "notes": "",
           "questions": [{"field_id": "0:a", "question": "Agree?", "options": []}]}
    plan = check_plan(raw, state, set())
    assert [(a.field_id, a.action, a.value) for a in plan.actions] == [("0:a", "select_combo", "I Agree")]
    assert [q.field_id for q in plan.questions] == ["0:c"]  # real question still asked; marketing left alone


def test_checkbox_group_is_one_question_and_optional_fields_are_skipped():
    from app.apply.page import PageState
    from app.apply.planner import check_plan

    q = "What is your desired salary?"
    fields = [
        {"id": f"e{i}", "kind": "checkbox", "label": f"{q} - {c}", "group": q, "required": True,
         "value": "unchecked"}
        for i, c in enumerate(["$80,000 - $90,000", "$90,000 - $100,000", "$100,000+"], 1)
    ] + [{"id": "e9", "kind": "textarea", "label": "Anything else?", "required": False, "value": ""}]
    state = PageState(frames=[{"fields": fields, "buttons": [], "errors": []}])
    plan = check_plan({"page_kind": "application_form", "fills": [], "questions": [
        {"field_id": "0:e9", "question": "Anything else?"}]}, state, {1})
    assert len(plan.questions) == 1
    assert plan.questions[0].question == q
    assert "$90,000 - $100,000" in plan.questions[0].options
    # Checking a box from the salary fact answers the whole group.
    plan = check_plan({"page_kind": "application_form", "questions": [], "fills": [
        {"field_id": "0:e2", "action": "check", "fact_ids": [1]}]}, state, {1})
    assert not plan.questions
    assert [a.field_id for a in plan.actions] == ["0:e2"]


def test_social_sign_in_is_never_clicked():
    for text in ("Sign in with LinkedIn", "Sign in with Facebook", "Apply with LinkedIn", "Sign up with Google",
                 "Log in with X", "LinkedIn"):
        cp = check_plan(plan(btn="0:x", kind="next", page_kind="sign_in"),
                        _state([], [{"id": "x", "text": text, "type": "button"}]), set())
        assert cp.advance_id is None, text
    ok = check_plan(plan(btn="0:x", kind="next", page_kind="sign_in"),
                    _state([], [{"id": "x", "text": "Don't have an account yet?", "type": "a"}]), set())
    assert ok.advance_id == "0:x" and ok.advance_kind == "next"


def test_security_questions_are_made_up_not_asked():
    q = {"id": "q1", "kind": "select", "label": "Security question 1", "required": True, "value": "Select",
         "options": ["Select", "What was your first pet's name?", "What city were you born in?"]}
    a = {"id": "a1", "kind": "text", "label": "Security answer 1", "required": True, "value": ""}
    raw = plan([{"field_id": "0:q1", "action": "select", "value": "What was your first pet's name?",
                 "fact_ids": []},
                {"field_id": "0:a1", "action": "account_security_answer", "value": "", "fact_ids": []}],
               page_kind="create_account")
    cp = check_plan(raw, _state([q, a]), set())
    assert not cp.questions
    assert [(x.action, x.value) for x in cp.actions] == [("select_option", "What was your first pet's name?"),
                                                         ("account_security_answer", "")]
    # Not on application forms: there it needs a real fact like everything else.
    cp = check_plan({**raw, "page_kind": "application_form"}, _state([q, a]), set())
    assert not any(x.action == "account_security_answer" for x in cp.actions)


def test_security_answers_are_stable_readable_and_distinct():
    from app.vault import security_answer

    one = security_answer("sjobs.brassring.com", "Answer#1")
    assert one == security_answer("https://sjobs.brassring.com/x", "answer#1")
    assert one != security_answer("sjobs.brassring.com", "Answer#2")
    assert one != security_answer("other.example.com", "Answer#1")
    w1, w2, num = one.split("-")
    assert w1.isalpha() and w2.isalpha() and 10 <= int(num) <= 99


def test_account_wrecking_buttons_never_clicked():
    for text in ("Delete My Information", "Delete my account", "Change Email", "Settings", "Remove my profile"):
        cp = check_plan(plan(btn="0:x", kind="next"), _state([], [{"id": "x", "text": text, "type": "button"}]),
                        set())
        assert cp.advance_id is None, text
    ok = check_plan(plan(btn="0:x", kind="next"), _state([], [{"id": "x", "text": "Save and Continue",
                                                              "type": "button"}]), set())
    assert ok.advance_id == "0:x"


def test_placeholder_rows_are_not_options():
    from app.apply.page import NO_MATCH

    for t in ("No Items.", "No matches found", "No results", "Loading...", "Type to search"):
        assert NO_MATCH.match(t), t
    assert not NO_MATCH.match("No, I've never been employed")


def test_finish_on_account_pages_is_not_the_application_submit():
    q = {"id": "q1", "kind": "select", "label": "Question 1", "required": True,
         "value": "What high school did you attend?", "options": ["What high school did you attend?"]}
    a = {"id": "a1", "kind": "text", "label": "Answer 1", "required": True, "value": ""}
    raw = plan([{"field_id": "0:a1", "action": "account_security_answer", "value": "", "fact_ids": []}],
               btn="0:f", kind="submit", page_kind="create_account")
    cp = check_plan(raw, _state([q, a], [{"id": "f", "text": "Finish", "type": "button"}]), set())
    assert cp.advance_kind == "next"
    cp = check_plan({**raw, "fills": []}, _state([], [{"id": "f", "text": "Finish", "type": "button"}]), set())
    assert cp.advance_kind == "next"  # any account page


def test_marketing_yes_no_is_always_no():
    texts = {"id": "t1", "kind": "radio", "required": True, "value": "Yes",
             "label": "Would you like to receive text messages regarding employment opportunities?",
             "options": [{"id": "y", "label": "Yes"}, {"id": "n", "label": "No"}]}
    cp = check_plan(plan(), _state([texts]), set())
    assert [(a.action, a.value, a.option_id) for a in cp.actions] == [("select_radio", "No", "n")]
    assert not cp.questions


def test_marketing_no_found_when_option_carries_question():
    q = "Would you like to receive text messages regarding employment opportunities?*"
    texts = {"id": "t1", "kind": "radio", "required": False, "value": "Yes", "label": q + " Yes No",
             "options": [{"id": "y", "label": "Yes"}, {"id": "n", "label": q + " - No"}]}
    cp = check_plan(plan(), _state([texts]), set())
    assert [(a.action, a.option_id) for a in cp.actions] == [("select_radio", "n")]


def test_education_level_uses_completed_level_not_degree_in_progress():
    lvl = {"id": "e1", "kind": "select", "label": "Education level", "required": True, "value": "",
           "options": ["Select", "High school", "Some college", "Associate's", "Bachelor's"]}
    facts = [{"id": 34, "section": "education", "label": "School 1 degree", "value": "B.S."},
             {"id": 81, "section": "education", "label": "Highest level of education completed",
              "value": "Some college"}]
    raw = plan([{"field_id": "0:e1", "action": "select", "value": "Bachelor's", "fact_ids": [34]}])
    cp = check_plan(raw, _state([lvl]), {34, 81}, facts)
    assert [(a.value, a.fact_ids) for a in cp.actions] == [("Some college", [81])]
    cp = check_plan(raw, _state([lvl]), {34}, facts[:1])  # no saved level: ask, don't guess
    assert not cp.actions and [q.field_id for q in cp.questions] == ["0:e1"]


def test_education_level_some_college_maps_to_high_school_and_fixes_prefilled():
    lvl = {"id": "e1", "kind": "select", "label": "Education level", "required": True, "value": "Bachelor's",
           "options": ["High school or equivalent", "Associate's", "Bachelor's", "Master's"]}
    facts = [{"id": 81, "section": "education", "label": "Highest level of education completed",
              "value": "Some college"}]
    cp = check_plan(plan(), _state([lvl]), {81}, facts)  # AI planned nothing; the page shows Bachelor's
    assert [(a.action, a.value, a.fact_ids) for a in cp.actions] == [
        ("select_option", "High school or equivalent", [81])]


def test_yes_no_choices_with_question_prefix_still_match():
    from app.apply.planner import _match_option

    q = "Are you living in or physically located in any of the sanctioned countries"
    opts = [q + " - Yes", q + " - No"]
    assert _match_option("No", opts) == 1 and _match_option("Yes", opts) == 0


def test_united_states_matches_the_country_not_outlying_islands():
    from app.apply.page import best_option
    from app.apply.planner import _match_option

    opts = ["American Samoa", "United Arab Emirates", "United States Minor Outlying Islands",
            "United States of America"]
    for v in ("United States", "USA", "US", "united states of america"):
        assert _match_option(v, opts) == 3 and best_option(v, opts) == 3, v


def test_searchable_dropdown_value_outside_partial_list_is_searched_not_asked():
    cit = {"id": "c1", "kind": "combobox", "label": "In which country/region do you have citizenship?",
           "required": True, "value": "", "options": ["Afghanistan", "Albania", "Algeria"]}
    raw = plan([{"field_id": "0:c1", "action": "select", "value": "United States", "fact_ids": [96]}])
    cp = check_plan(raw, _state([cit]), {96})
    assert [(a.action, a.value) for a in cp.actions] == [("select_combo", "United States")] and not cp.questions
