"""The supplier review: a short, tap-friendly conversation (one question at a time) plus the sourcing preferences."""
from types import SimpleNamespace

import pytest

from db.models import IntakeSession, Job
from server.agent import intake, review_flow, supplier_review
from server.agent.intake import IntakeUpdate, handle_message
from server.agent.supplier_review import Review
from services import filters
from services.rfq_template import build_body

BASE = dict(
    category="painter",
    title="Repaint fence",
    description="Repaint 20 metres of faded timber paling fence.",
    location_text="Brunswick 3056",
)
KNOWN_PREFS = dict(distance_preference="local", max_distance_km=10, rating_preference="no_preference", timeframe="ASAP")


def scripted(*updates):
    queue = list(updates)
    return lambda draft, transcript, text, n: queue.pop(0)


def refined(db, user, **extra):
    """Bring a chat to the confirmation summary (awaiting_confirm) without any preferences unless given."""
    replies = handle_message(db, user, "repaint my fence in brunswick", model_fn=scripted(IntakeUpdate(**BASE, **extra)), require_name=False)
    assert "Reply YES to start" in replies[0], replies
    return replies


class CountingReviewer:
    def __init__(self, result):
        self.result, self.calls = result, 0

    def __call__(self, draft, attachments):
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def Q(label, question, options=(), multi=False):
    return {"label": label, "question": question, "options": list(options), "multi": multi}


def job_for(db, user):
    return db.query(Job).filter_by(user_id=user.id).one()


def say(db, user, text, reviewer=None, **kw):
    """One customer message, website-style (options are buttons, not text)."""
    kw.setdefault("render_options", False)
    return handle_message(db, user, text, review_fn=reviewer, require_name=False, **kw)


def session_of(db, user):
    return db.query(IntakeSession).filter_by(user_id=user.id).one()


CONDITION = Q("Existing paint", "What condition is the existing paint in?", ["Peeling or flaking", "Just faded", "Bare timber"])
TRIM = Q("Includes trim", "Should the eaves, fascias and gutters be painted too?", ["Yes", "No"])


# ---- what the reviewer returns (pure helpers) --------------------------------------------------------------------
def test_essential_then_important_questions_are_kept_minor_dropped_and_capped_at_five():
    def gap(q, importance): return {"label": q, "question": q, "options": ["A", "B"], "multi": False, "why": "x", "importance": importance}
    tool_input = {"gaps": [
        gap("Important A", "important"), gap("Minor A", "minor"), gap("  ", "essential"), gap("Essential A", "essential"),
        gap("Important B", "important"), gap("Essential B", "essential"), gap("Important C", "important"),
        gap("Important D", "important"), gap("Important E", "important"),
    ]}
    chosen = supplier_review.pertinent_questions(tool_input)
    assert [q["question"] for q in chosen] == ["Essential A", "Essential B", "Important A", "Important B", "Important C"]
    assert supplier_review.MAX_QUESTIONS == 5


def test_thorough_mode_adds_minor_questions_after_the_rest():
    def gap(q, importance): return {"label": q, "question": q, "options": [], "multi": False, "why": "x", "importance": importance}
    tool_input = {"gaps": [gap("Minor 1", "minor"), gap("Essential", "essential"), gap("Minor 2", "minor"), gap("Important", "important")]}
    names = lambda qs: [q["question"] for q in qs]
    assert names(supplier_review.pertinent_questions(tool_input)) == ["Essential", "Important"]
    assert names(supplier_review.pertinent_questions(tool_input, include_minor=True)) == ["Essential", "Important", "Minor 1", "Minor 2"]
    assert supplier_review.pertinent_questions({"gaps": [{"question": "Odd", "importance": "surprising"}]}) == []  # unknown level = minor


def test_questions_are_cleaned_options_deduplicated_trimmed_and_capped():
    gap = {"label": " Paint ", "question": " What condition? ", "importance": "essential", "multi": True,
           "options": [" Peeling ", "peeling", "Faded", "", "Bare", "Fresh", "Other one", "Sixth"]}
    q = supplier_review.pertinent_questions({"gaps": [gap]})[0]
    assert q == {"label": "Paint", "question": "What condition?", "options": ["Peeling", "Faded", "Bare", "Fresh", "Other one"], "multi": True,
                 "photo_estimable": False, "typical": ""}


@pytest.mark.parametrize("photo_estimable, typical, kept", [
    (True, "about 3 m wide (single car)", "about 3 m wide (single car)"),
    (True, "No, not yet", ""),  # not a number with a unit: it would put an unconfirmed guess into the brief
    (False, "about 3 m wide", ""),  # not something a photo can show, so no shortcut either
])
def test_a_typical_value_is_only_offered_for_a_visible_measurable_thing(photo_estimable, typical, kept):
    gap = {"label": "Width", "question": "How wide?", "importance": "essential", "options": [],
           "photo_estimable": photo_estimable, "typical_answer": typical}
    assert supplier_review.pertinent_questions({"gaps": [gap]})[0]["typical"] == kept


def test_suggestion_lines_are_cleaned_and_capped():
    lines = supplier_review.attachment_lines({"attachment_suggestions": [
        {"item": "Drawings", "why": "layout", "kind": "document"},
        {"item": "  ", "why": "x", "kind": "photo"},
        {"item": "Photo of the site", "why": "", "kind": "photo"},
        {"item": "Title", "why": "boundaries", "kind": "document"},
        {"item": "Fifth", "why": "x", "kind": "photo"},
    ]})
    assert lines == ["Drawings: layout", "Photo of the site", "Title: boundaries"]


def test_reviewer_is_told_exactly_what_is_already_attached():
    text = supplier_review.describe_attachments(["/x/ab12cd34_Structural Drawings.pdf", "/x/aaaaaaaaaaaa.jpg", "/x/bbbbbbbbbbbb.jpg"])
    assert "2 photo(s)" in text and "documents: Structural Drawings.pdf" in text
    assert supplier_review.describe_attachments([]) == "none"
    assert supplier_review.describe_attachments(0) == "none" and supplier_review.describe_attachments(3) == "3 attachment(s)"
    described = supplier_review._describe({"category": "surveyor", "project_type": "residential"}, ["/x/ab12cd34_Plan.pdf"])
    assert "Already attached: documents: Plan.pdf" in described and "Project type: residential" in described


# ---- asked in turn ----------------------------------------------------------------------------------------------------
def test_yes_asks_only_the_first_question_and_creates_no_job(db, user):
    refined(db, user, **KNOWN_PREFS)
    reply = say(db, user, "yes", CountingReviewer(Review([CONDITION, TRIM], True)))[0]
    assert "a few quick questions" in reply and "type your own" in reply  # a one-time intro
    assert "1 of 2: What condition is the existing paint in?" in reply
    assert "eaves" not in reply  # the second question is NOT asked yet
    assert db.query(Job).filter_by(user_id=user.id).count() == 0
    s = session_of(db, user)
    assert s.state == "collecting" and review_flow.current_choices(s) == {
        "kind": "pro", "label": "Existing paint", "options": ["Peeling or flaking", "Just faded", "Bare timber"], "multi": False,
        "typical": None, "photo_estimable": False, "photos_attached": False,
    }


def test_each_answer_moves_to_the_next_question_then_the_summary_and_becomes_a_job_detail(db, user):
    refined(db, user, **KNOWN_PREFS)
    reviewer = CountingReviewer(Review([CONDITION, TRIM], True))
    say(db, user, "yes", reviewer)

    second = say(db, user, "Peeling or flaking", reviewer)[0]
    assert second.startswith("2 of 2: Should the eaves") and "a few quick questions" not in second  # intro only once

    summary = say(db, user, "Yes", reviewer)[0]  # a bare "yes" is an ANSWER here, not a confirmation
    assert "Reply YES to start" in summary
    assert "- Existing paint: Peeling or flaking" in summary and "- Includes trim: Yes" in summary
    assert db.query(Job).filter_by(user_id=user.id).count() == 0

    assert "Confirmed" in say(db, user, "yes", reviewer)[0]
    job = job_for(db, user)
    assert "Additional details:" in job.description
    assert "- Existing paint: Peeling or flaking" in job.description and "- Includes trim: Yes" in job.description
    assert reviewer.calls == 1  # the review ran once


def test_options_can_be_chosen_by_number_or_label_and_not_sure_is_the_last_number(db, user):
    refined(db, user, **KNOWN_PREFS)
    reviewer = CountingReviewer(Review([CONDITION, TRIM, Q("Access", "Is there easy access?", ["Easy", "Tight"])], True))
    say(db, user, "yes", reviewer)
    say(db, user, "2", reviewer)  # number -> "Just faded"
    say(db, user, "no", reviewer)  # label, any case -> "No"
    summary = say(db, user, "3", reviewer)[0]  # two options + "Not sure" -> 3 is "Not sure"
    assert "- Existing paint: Just faded" in summary and "- Includes trim: No" in summary
    assert "Access" not in summary  # "Not sure" records nothing


def test_free_text_elaboration_is_kept_verbatim(db, user):
    refined(db, user, **KNOWN_PREFS)
    reviewer = CountingReviewer(Review([CONDITION], True))
    say(db, user, "yes", reviewer)
    summary = say(db, user, "Mostly peeling on the sunny side, lead paint likely as the house is from 1960", reviewer)[0]
    assert "- Existing paint: Mostly peeling on the sunny side, lead paint likely as the house is from 1960" in summary


@pytest.mark.parametrize("skip", ["Not sure", "skip", "I don't know", "unsure"])
def test_skipping_a_question_records_nothing_and_moves_on(db, user, skip):
    refined(db, user, **KNOWN_PREFS)
    reviewer = CountingReviewer(Review([CONDITION, TRIM], True))
    say(db, user, "yes", reviewer)
    nxt = say(db, user, skip, reviewer)[0]
    assert nxt.startswith("2 of 2:")
    summary = say(db, user, "No", reviewer)[0]
    assert "Existing paint" not in summary and "- Includes trim: No" in summary


def test_multi_select_answers(db, user):
    options = ["Eaves", "Fascia", "Gutters"]
    for text, expected in [("1, 3", "Eaves, Gutters"), ("eaves and fascia", "Eaves, Fascia"), ("Gutters", "Gutters")]:
        assert review_flow.resolve_option(text, options, multi=True) == expected
    reviewer = CountingReviewer(Review([Q("Trim", "Which trim is included?", options, multi=True)], True))
    refined(db, user, **KNOWN_PREFS)
    say(db, user, "yes", reviewer)
    assert "- Trim: Eaves, Gutters" in say(db, user, "1, 3", reviewer)[0]
    assert review_flow.current_choices(session_of(db, user)) is None  # the queue is finished


def test_questions_without_options_are_open_ended(db, user):
    refined(db, user, **KNOWN_PREFS)
    reviewer = CountingReviewer(Review([Q("Size", "Roughly how many square metres is the roof?")], True))
    assert "square metres" in say(db, user, "yes", reviewer)[0]
    assert review_flow.current_choices(session_of(db, user))["options"] == []
    assert "- Size: about 120" in say(db, user, "about 120", reviewer)[0]


def test_plain_string_questions_from_simple_reviewers_still_work(db, user):
    refined(db, user, **KNOWN_PREFS)
    reply = say(db, user, "yes", CountingReviewer(Review(["How many storeys is the house?"], True)))[0]
    assert "How many storeys is the house?" in reply and "1 of" not in reply


# ---- WhatsApp: options are a numbered list in the text ------------------------------------------------------------
def test_whatsapp_gets_a_numbered_list_and_the_website_does_not(db, user):
    refined(db, user, **KNOWN_PREFS)
    reviewer = CountingReviewer(Review([CONDITION], True))
    whatsapp = handle_message(db, user, "yes", review_fn=reviewer, require_name=False, render_options=True)[0]
    assert "1) Peeling or flaking" in whatsapp and "3) Bare timber" in whatsapp and "4) Not sure" in whatsapp
    assert "Reply with a number" in whatsapp


def test_website_text_has_no_numbered_list(db, user):
    refined(db, user, **KNOWN_PREFS)
    web = say(db, user, "yes", CountingReviewer(Review([CONDITION], True)))[0]
    assert "1)" not in web and "Reply with a number" not in web


# ---- sourcing preferences: tappable options, parsed into fields ----------------------------------------------------
def test_preferences_are_asked_in_turn_with_options_and_set_the_job(db, user):
    refined(db, user)  # nothing known
    reviewer = CountingReviewer(Review([], True, True))
    first = say(db, user, "yes", reviewer)[0]
    assert "1 of 4: Is this a residential or a commercial project?" in first
    assert review_flow.current_choices(session_of(db, user))["options"] == ["Residential", "Commercial"]

    assert "2 of 4: How close do suppliers need to be to Brunswick 3056?" in say(db, user, "Residential", reviewer)[0]
    assert "3 of 4" in say(db, user, "Within 10 km", reviewer)[0]
    assert "4 of 4: When does the job need to be done?" in say(db, user, "Any rating is fine", reviewer)[0]
    summary = say(db, user, "Within 2 weeks", reviewer)[0]
    for line in ("- Project type: residential", "- Distance: within 10 km", "- Reviews: any rating", "- When: Within 2 weeks"):
        assert line in summary

    say(db, user, "yes", reviewer)
    job = job_for(db, user)
    assert (job.project_type, job.radius_m, job.min_rating, job.timeframe) == ("residential", 10_000, None, "Within 2 weeks")
    assert reviewer.calls == 1


def test_top_rated_and_anywhere_options(db, user):
    refined(db, user, project_type="commercial", timeframe="ASAP")
    reviewer = CountingReviewer(Review([], True, True))
    say(db, user, "yes", reviewer)
    say(db, user, "Anywhere", reviewer)
    say(db, user, "4.5 stars or higher only", reviewer)
    say(db, user, "yes", reviewer)
    job = job_for(db, user)
    assert (job.project_type, job.radius_m, job.min_rating) == ("commercial", 50_000, 4.5)


def test_preferences_already_known_are_not_asked_again(db, user):
    refined(db, user, project_type="commercial", **KNOWN_PREFS)
    reply = say(db, user, "yes", CountingReviewer(Review([CONDITION], True, True)))[0]
    assert "1 of 1" not in reply and "What condition" in reply  # a single question is not numbered
    assert "commercial project" not in reply


def test_project_type_is_not_asked_when_it_does_not_matter(db, user):
    refined(db, user, **KNOWN_PREFS)
    assert "Confirmed" in say(db, user, "yes", CountingReviewer(Review([], True, False)))[0]


def test_remote_friendly_work_skips_the_distance_question_and_searches_wide(db, user):
    refined(db, user, rating_preference="no_preference", timeframe="flexible")
    assert "Confirmed" in say(db, user, "yes", CountingReviewer(Review([], False)))[0]
    assert job_for(db, user).radius_m == 50_000


@pytest.mark.parametrize("text, expected", [
    ("Residential", "residential"), ("Commercial", "commercial"), ("it is my house", "residential"),
    ("I run a cafe", "commercial"), ("our warehouse", "commercial"), ("a townhouse", "residential"), ("not sure really", None),
])
def test_parse_project_type(text, expected):
    assert review_flow.parse_project_type(text) == expected


@pytest.mark.parametrize("text, expected", [
    ("Within 5 km", ("local", 5.0)), ("within about 8km please", ("local", 8.0)), ("Within 25 km", ("local", 25.0)),
    ("Anywhere", ("anywhere", None)), ("doesn't matter", ("anywhere", None)), ("no limit", ("anywhere", None)),
    ("close by", (None, None)),
])
def test_parse_distance(text, expected):
    assert review_flow.parse_distance(text) == expected


@pytest.mark.parametrize("text, expected", [
    ("4.5 stars or higher only", "top_rated_only"), ("Any rating is fine", "no_preference"), ("only the best ones", "top_rated_only"),
    ("yes", "top_rated_only"), ("no", "no_preference"), ("don't mind", "no_preference"), ("hmm", None),
])
def test_parse_rating(text, expected):
    assert review_flow.parse_rating(text) == expected


def test_unparseable_preference_answers_leave_the_defaults(db, user):
    refined(db, user)
    reviewer = CountingReviewer(Review([], True, True))
    say(db, user, "yes", reviewer)
    for answer in ("hmm", "close by", "dunno", "whenever"):
        say(db, user, answer, reviewer)
    say(db, user, "yes", reviewer)
    job = job_for(db, user)
    assert (job.project_type, job.radius_m, job.min_rating) == (None, 15_000, None)


# ---- leaving the review ----------------------------------------------------------------------------------------------
def test_send_as_is_mid_review_creates_the_job_with_what_was_answered_so_far(db, user):
    refined(db, user, **KNOWN_PREFS)
    reviewer = CountingReviewer(Review([CONDITION, TRIM], True))
    say(db, user, "yes", reviewer)
    say(db, user, "Just faded", reviewer)
    reply = say(db, user, "Send as is", reviewer)[0]
    assert "Confirmed" in reply
    job = job_for(db, user)
    assert "- Existing paint: Just faded" in job.description and "Includes trim" not in job.description
    assert reviewer.calls == 1


def test_send_as_is_works_straight_from_the_summary_and_skips_the_review(db, user):
    refined(db, user)
    reviewer = CountingReviewer(Review([CONDITION], True))
    assert "Confirmed" in say(db, user, "send as is", reviewer)[0]
    assert reviewer.calls == 0
    assert job_for(db, user).radius_m == 15_000


def test_send_as_is_needs_a_complete_request(db, user):
    reply = handle_message(db, user, "send as is", model_fn=scripted(IntakeUpdate(is_job_request=False, next_question="What do you need done?")), require_name=False)
    assert reply == ["What do you need done?"]


def test_cancel_mid_review_drops_the_request(db, user):
    refined(db, user, **KNOWN_PREFS)
    reviewer = CountingReviewer(Review([CONDITION, TRIM], True))
    say(db, user, "yes", reviewer)
    assert "dropped" in say(db, user, "cancel", reviewer)[0]
    assert db.query(Job).filter_by(user_id=user.id).count() == 0
    assert session_of(db, user).state == "cancelled"


def test_review_failure_never_blocks_the_customer(db, user):
    refined(db, user)
    assert "Confirmed" in say(db, user, "yes", CountingReviewer(RuntimeError("api down")))[0]
    assert job_for(db, user).radius_m == 15_000  # defaults apply


def test_nothing_to_ask_goes_straight_to_creating_the_job(db, user):
    refined(db, user, **KNOWN_PREFS)
    assert "Confirmed" in say(db, user, "yes", CountingReviewer(Review([], True)))[0]


def test_reviewer_gets_the_attachment_paths_and_a_draft_without_internal_keys(db, user, tmp_path):
    seen = {}

    def spy(draft, attachments):
        seen.update(draft=draft, attachments=attachments)
        return Review([], True)

    photo = tmp_path / "p.jpg"
    photo.write_bytes(b"x")
    handle_message(db, user, "repaint my fence", [str(photo)], model_fn=scripted(IntakeUpdate(**BASE, **KNOWN_PREFS)), require_name=False)
    say(db, user, "yes", spy)
    assert seen["attachments"] == [str(photo)]
    assert not [k for k in seen["draft"] if k.startswith("_")]


# ---- attachments step ----------------------------------------------------------------------------------------------------
SUBDIVISION = ("Engineering drawings (PDF): they show the proposed lot layout", "A copy of the title: confirms the boundaries")


def test_attachment_suggestions_come_last_and_are_not_numbered(db, user):
    refined(db, user, **KNOWN_PREFS)
    reviewer = CountingReviewer(Review([CONDITION], True, False, SUBDIVISION))
    say(db, user, "yes", reviewer)
    reply = say(db, user, "Just faded", reviewer)[0]
    assert "1 of" not in reply and "2 of" not in reply
    assert "- Engineering drawings (PDF): they show the proposed lot layout" in reply and "- A copy of the title" in reply
    assert "paperclip" in reply
    assert review_flow.current_choices(session_of(db, user))["kind"] == "attach"


def test_suggestions_alone_are_shown_with_a_lead_in(db, user):
    refined(db, user, **KNOWN_PREFS)
    reply = say(db, user, "yes", CountingReviewer(Review([], True, False, SUBDIVISION)))[0]
    assert reply.startswith("Before I send this out: It would also help suppliers quote accurately if you can attach:")


def test_attaching_a_file_completes_the_attachment_step(db, user, tmp_path):
    refined(db, user, **KNOWN_PREFS)
    reviewer = CountingReviewer(Review([], True, False, SUBDIVISION))
    say(db, user, "yes", reviewer)
    drawing = tmp_path / "ab12cd34_Subdivision Drawings.pdf"
    drawing.write_bytes(b"%PDF-1.4 stub")
    summary = say(db, user, "", reviewer, media_paths=[str(drawing)])[0]
    assert "Attachments: 1 document" in summary
    assert "Confirmed" in say(db, user, "yes", reviewer)[0]
    assert reviewer.calls == 1


def test_continue_without_attaching_completes_the_step(db, user):
    refined(db, user, **KNOWN_PREFS)
    reviewer = CountingReviewer(Review([], True, False, SUBDIVISION))
    say(db, user, "yes", reviewer)
    summary = say(db, user, "Continue without attaching", reviewer)[0]
    assert "Attachments: none" in summary


def test_typed_text_at_the_attachment_step_is_kept_as_a_note(db, user):
    refined(db, user, **KNOWN_PREFS)
    reviewer = CountingReviewer(Review([], True, False, SUBDIVISION))
    say(db, user, "yes", reviewer)
    assert "- Note: no drawings yet, sorry" in say(db, user, "no drawings yet, sorry", reviewer)[0]


def test_a_file_sent_while_a_question_is_open_is_kept_and_the_question_repeats(db, user, tmp_path):
    refined(db, user, **KNOWN_PREFS)
    reviewer = CountingReviewer(Review([CONDITION], True))
    say(db, user, "yes", reviewer)
    photo = tmp_path / "wall.jpg"
    photo.write_bytes(b"x")
    reply = say(db, user, "", reviewer, media_paths=[str(photo)])[0]
    assert reply.startswith("Got it, thanks.") and "What condition is the existing paint in?" in reply
    s = session_of(db, user)
    assert s.photo_paths == [str(photo)] and review_flow.current_question(s)["label"] == "Existing paint"


# ---- the pieces of the flow --------------------------------------------------------------------------------------------
def test_resolve_option_rules():
    options = ["Peeling", "Faded"]
    assert review_flow.resolve_option("1", options) == "Peeling"
    assert review_flow.resolve_option("faded", options) == "Faded"
    assert review_flow.resolve_option("3", options) == "Not sure"  # the number after the last option
    assert review_flow.resolve_option("9", options) == "9"  # out of range: the customer's own text
    assert review_flow.resolve_option("a bit of both", options) == "a bit of both"


def test_unknown_never_overwrites_what_the_customer_already_said(db, user):
    refined(db, user, distance_preference="local", max_distance_km=10, rating_preference="top_rated_only", timeframe="ASAP")
    follow_up = IntakeUpdate(**BASE, distance_preference="unknown", rating_preference="unknown", timeframe=None, max_distance_km=None, next_question=None)
    handle_message(db, user, "also the gate is rusty", model_fn=scripted(follow_up), require_name=False)
    draft = session_of(db, user).draft
    assert (draft["distance_preference"], draft["max_distance_km"], draft["rating_preference"], draft["timeframe"]) == ("local", 10, "top_rated_only", "ASAP")


# ---- the preferences actually change what happens --------------------------------------------------------------------
def test_radius_and_rating_rules():
    assert intake.radius_km_for({}) == 15
    assert intake.radius_km_for({"distance_preference": "local", "max_distance_km": 8}) == 8
    assert intake.radius_km_for({"distance_preference": "local", "max_distance_km": 500}) == 50  # capped
    assert intake.radius_km_for({"distance_preference": "anywhere"}) == 50
    assert intake.radius_km_for({"distance_preference": "local"}) == 15  # local but no distance given
    assert intake.min_rating_for({"rating_preference": "top_rated_only"}) == 4.5
    assert intake.min_rating_for({"rating_preference": "no_preference"}) is None
    assert intake.min_rating_for({}) is None


def company(name, rating, included=True):
    return SimpleNamespace(name=name, rating=rating, included=included, scrape_status="pending", scrape_notes=None)


def test_rating_filter_excludes_low_and_unrated_suppliers():
    job = SimpleNamespace(min_rating=4.5)
    good, borderline, low, unrated = company("good", 4.8), company("edge", 4.5), company("low", 4.2), company("new", None)
    assert filters.apply_rating_filter(job, [good, borderline, low, unrated]) == 2
    assert [c.included for c in (good, borderline, low, unrated)] == [True, True, False, False]
    assert low.scrape_status == "excluded_rating" and "4.2" in low.scrape_notes
    assert unrated.scrape_notes == "No Google rating"


def test_rating_filter_does_nothing_without_a_preference():
    c = company("low", 3.0)
    assert filters.apply_rating_filter(SimpleNamespace(min_rating=None), [c]) == 0
    assert c.included is True


def email_job(**over):
    base = dict(
        category="painter", title="Repaint fence", description="Repaint 20m fence.", location_text="Brunswick 3056",
        budget_min=None, budget_max=None, deadline=None, timeframe=None, project_type=None, ref_prefix="ABC",
    )
    return SimpleNamespace(**{**base, **over})


def test_supplier_email_states_timeframe_and_project_type():
    acme = SimpleNamespace(name="Acme Painting")
    body = build_body(email_job(timeframe="within 2 weeks", project_type="commercial"), acme, "ABC-123", "Irene")
    assert "When the work needs to be done: within 2 weeks." in body and "Project type: commercial." in body
    plain = build_body(email_job(), acme, "ABC-123", "Irene")
    assert "When the work needs to be done" not in plain and "Project type" not in plain


def test_places_query_leads_with_the_project_type():
    from services.places_service import build_query

    assert build_query("electrician", "commercial") == "commercial electrician"
    assert build_query("electrician", "residential") == "residential electrician"
    assert build_query("electrician", None) == "electrician"
    assert build_query("electrician", "unknown") == "electrician"


# ---- regression: matching must keep digits, and unsure replies must be recognised --------------------------------
def test_distance_options_are_not_confused_with_each_other():
    options = review_flow.DISTANCE_OPTIONS
    assert review_flow.resolve_option("Within 5 km", options) == "Within 5 km"
    assert review_flow.resolve_option("Within 10 km", options) == "Within 10 km"
    assert review_flow.resolve_option("within 25 KM", options) == "Within 25 km"
    assert review_flow.parse_distance("Within 10 km") == ("local", 10.0)  # tapping "Within 10 km" must never mean 5


@pytest.mark.parametrize("text", [
    "Not sure", "not sure really", "I don't know", "i dont know", "dunno", "idk", "no idea", "Skip", "skip this", "unsure",
])
def test_unsure_replies_are_recognised(text):
    assert review_flow.is_unsure(text)


@pytest.mark.parametrize("text", ["Peeling or flaking", "Yes", "No", "about 120 square metres", "Within 10 km", "I know the roof is tiled"])
def test_real_answers_are_not_mistaken_for_unsure(text):
    assert not review_flow.is_unsure(text)
