"""Supplier-eye review + the three sourcing preferences (distance, rating, timeframe)."""
from types import SimpleNamespace

from db.models import IntakeSession, Job
from server.agent import intake, supplier_review
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

    def __call__(self, draft, n_photos):
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def job_for(db, user):
    return db.query(Job).filter_by(user_id=user.id).one()


# ---- what the reviewer returns ------------------------------------------------------------------------------
def test_essential_then_important_questions_are_kept_minor_dropped_and_capped_at_five():
    def gap(q, importance): return {"question": q, "why": "x", "importance": importance}
    tool_input = {"gaps": [
        gap("Important A", "important"), gap("Minor A", "minor"), gap("  ", "essential"), gap("Essential A", "essential"),
        gap("Important B", "important"), gap("Essential B", "essential"), gap("Important C", "important"),
        gap("Important D", "important"), gap("Important E", "important"),
    ]}
    # essential first, then important in the order given; minor and empty text dropped; at most five
    assert supplier_review.pertinent_questions(tool_input) == [
        "Essential A", "Essential B", "Important A", "Important B", "Important C",
    ]
    assert supplier_review.MAX_QUESTIONS == 5


# ---- the review step in the conversation -------------------------------------------------------------------
def test_yes_runs_the_review_asks_gaps_and_unknown_preferences_and_creates_no_job(db, user):
    refined(db, user)
    reviewer = CountingReviewer(Review(["How many storeys is the house?"], True))
    reply = handle_message(db, user, "yes", review_fn=reviewer, require_name=False)[0]

    assert "How many storeys is the house?" in reply
    assert "How close do suppliers need to be to Brunswick 3056?" in reply  # proximity, with the location filled in
    assert "4.5 stars" in reply  # rating preference
    assert "When does the job need to be done?" in reply  # timeframe
    assert "SEND AS IS" in reply
    assert db.query(Job).filter_by(user_id=user.id).count() == 0
    assert db.query(IntakeSession).filter_by(user_id=user.id).one().state == "collecting"
    assert reviewer.calls == 1


def test_answers_flow_into_the_job_and_the_review_never_repeats(db, user):
    refined(db, user)
    reviewer = CountingReviewer(Review(["How many storeys is the house?"], True))
    handle_message(db, user, "yes", review_fn=reviewer, require_name=False)

    answer = IntakeUpdate(
        **{**BASE, "description": BASE["description"] + " Single storey."},
        distance_preference="local", max_distance_km=8, rating_preference="top_rated_only", timeframe="within 2 weeks",
    )
    summary = handle_message(db, user, "single storey, within 8km, 4.5+ only, in 2 weeks", model_fn=scripted(answer), require_name=False)[0]
    assert "Distance: within 8 km" in summary
    assert "Reviews: 4.5 stars or higher" in summary
    assert "When: within 2 weeks" in summary

    reply = handle_message(db, user, "yes", review_fn=reviewer, require_name=False)[0]
    assert "Confirmed" in reply
    assert reviewer.calls == 1  # asked once, never again

    job = job_for(db, user)
    assert (job.radius_m, job.min_rating, job.timeframe) == (8000, 4.5, "within 2 weeks")


def test_preferences_already_stated_are_not_asked_again(db, user):
    refined(db, user, distance_preference="local", max_distance_km=10, rating_preference="no_preference", timeframe="ASAP")
    reviewer = CountingReviewer(Review(["How many storeys is the house?"], True))
    reply = handle_message(db, user, "yes", review_fn=reviewer, require_name=False)[0]
    assert "How many storeys" in reply
    assert "How close" not in reply and "4.5 stars" not in reply and "When does the job" not in reply
    assert "here's what a supplier would want to know" in reply


def test_nothing_missing_goes_straight_to_creating_the_job(db, user):
    refined(db, user, distance_preference="local", max_distance_km=10, rating_preference="no_preference", timeframe="ASAP")
    assert "Confirmed" in handle_message(db, user, "yes", review_fn=CountingReviewer(Review([], True)), require_name=False)[0]
    assert job_for(db, user).status == "QUEUED"


def test_remote_friendly_work_skips_the_distance_question_and_searches_wide(db, user):
    refined(db, user, rating_preference="no_preference", timeframe="flexible")
    reply = handle_message(db, user, "yes", review_fn=CountingReviewer(Review([], False)), require_name=False)[0]
    assert "Confirmed" in reply  # nothing left to ask
    job = job_for(db, user)
    assert job.radius_m == 50_000
    assert db.query(IntakeSession).filter_by(user_id=user.id).one().draft["distance_preference"] == "anywhere"


def test_review_failure_never_blocks_the_customer(db, user):
    refined(db, user)
    reply = handle_message(db, user, "yes", review_fn=CountingReviewer(RuntimeError("api down")), require_name=False)[0]
    assert "Confirmed" in reply
    assert job_for(db, user).radius_m == 15_000  # defaults apply


def test_send_as_is_skips_the_questions_and_uses_defaults(db, user):
    refined(db, user)
    reviewer = CountingReviewer(Review(["How many storeys is the house?"], True))
    handle_message(db, user, "yes", review_fn=reviewer, require_name=False)

    reply = handle_message(db, user, "Send as is", review_fn=reviewer, require_name=False)[0]
    assert "Confirmed" in reply
    job = job_for(db, user)
    assert (job.radius_m, job.min_rating, job.timeframe) == (15_000, None, None)
    assert reviewer.calls == 1


def test_unknown_never_overwrites_what_the_customer_already_said(db, user):
    refined(db, user, distance_preference="local", max_distance_km=10, rating_preference="top_rated_only", timeframe="ASAP")
    follow_up = IntakeUpdate(**BASE, distance_preference="unknown", rating_preference="unknown", timeframe=None, max_distance_km=None, next_question=None)
    handle_message(db, user, "also the gate is rusty", model_fn=scripted(follow_up), require_name=False)
    draft = db.query(IntakeSession).filter_by(user_id=user.id).one().draft
    assert (draft["distance_preference"], draft["max_distance_km"], draft["rating_preference"], draft["timeframe"]) == ("local", 10, "top_rated_only", "ASAP")


# ---- preference helpers ----------------------------------------------------------------------------------------
def test_radius_and_rating_rules():
    assert intake.radius_km_for({}) == 15
    assert intake.radius_km_for({"distance_preference": "local", "max_distance_km": 8}) == 8
    assert intake.radius_km_for({"distance_preference": "local", "max_distance_km": 500}) == 50  # capped
    assert intake.radius_km_for({"distance_preference": "anywhere"}) == 50
    assert intake.radius_km_for({"distance_preference": "local"}) == 15  # local but no distance given
    assert intake.min_rating_for({"rating_preference": "top_rated_only"}) == 4.5
    assert intake.min_rating_for({"rating_preference": "no_preference"}) is None
    assert intake.min_rating_for({}) is None


# ---- the preferences actually change what happens ------------------------------------------------------------
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


def test_supplier_email_states_when_the_work_needs_doing():
    job = SimpleNamespace(
        category="painter", title="Repaint fence", description="Repaint 20m fence.", location_text="Brunswick 3056",
        budget_min=None, budget_max=None, deadline=None, timeframe="within 2 weeks", ref_prefix="ABC",
    )
    body = build_body(job, SimpleNamespace(name="Acme Painting"), "ABC-123", "Irene")
    assert "When the work needs to be done: within 2 weeks." in body
    job.timeframe = None
    assert "When the work needs to be done" not in build_body(job, SimpleNamespace(name="Acme Painting"), "ABC-123", "Irene")


def test_send_as_is_works_even_before_the_review_has_asked_anything(db, user):
    """'Send as is' while the summary is showing is a clear go-ahead: skip the questions entirely."""
    refined(db, user)
    reviewer = CountingReviewer(Review(["How many storeys is the house?"], True))
    reply = handle_message(db, user, "send as is", review_fn=reviewer, require_name=False)[0]
    assert "Confirmed" in reply
    assert reviewer.calls == 0
    assert job_for(db, user).radius_m == 15_000


def test_send_as_is_needs_a_complete_request(db, user):
    reply = handle_message(db, user, "send as is", model_fn=scripted(IntakeUpdate(is_job_request=False, next_question="What do you need done?")), require_name=False)
    assert reply == ["What do you need done?"]


def test_thorough_mode_adds_minor_questions_after_the_rest():
    def gap(q, importance): return {"question": q, "why": "x", "importance": importance}
    tool_input = {"gaps": [gap("Minor 1", "minor"), gap("Essential", "essential"), gap("Minor 2", "minor"), gap("Important", "important")]}
    assert supplier_review.pertinent_questions(tool_input) == ["Essential", "Important"]
    assert supplier_review.pertinent_questions(tool_input, include_minor=True) == ["Essential", "Important", "Minor 1", "Minor 2"]
    assert supplier_review.pertinent_questions({"gaps": [{"question": "Odd", "why": "x", "importance": "surprising"}]}) == []  # unknown level = minor