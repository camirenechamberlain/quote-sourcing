"""Project type (residential/commercial) and attachment suggestions from the supplier review."""
from types import SimpleNamespace

from db.models import IntakeSession, Job
from server.agent import intake, supplier_review
from server.agent.intake import IntakeUpdate, handle_message
from server.agent.supplier_review import Review
from services.rfq_template import build_body
from tests.test_supplier_review import BASE, CountingReviewer, job_for, refined, scripted

KNOWN_PREFS = dict(distance_preference="local", max_distance_km=10, rating_preference="no_preference", timeframe="ASAP")
SUBDIVISION = (
    "Engineering drawings (PDF): they show the proposed lot layout and services",
    "A copy of the title: confirms the lot boundaries",
)


# ---- residential vs commercial ---------------------------------------------------------------------------
def test_project_type_is_asked_when_it_matters_and_is_unknown(db, user):
    refined(db, user, **KNOWN_PREFS)
    reply = handle_message(db, user, "yes", review_fn=CountingReviewer(Review([], True, True)), require_name=False)[0]
    assert "residential or a commercial project" in reply
    assert "How close" not in reply  # the other preferences were already known
    assert db.query(Job).filter_by(user_id=user.id).count() == 0


def test_project_type_is_not_asked_when_it_does_not_matter(db, user):
    refined(db, user, **KNOWN_PREFS)
    reply = handle_message(db, user, "yes", review_fn=CountingReviewer(Review([], True, False)), require_name=False)[0]
    assert "Confirmed" in reply


def test_project_type_already_stated_is_not_asked_again(db, user):
    refined(db, user, project_type="commercial", **KNOWN_PREFS)
    reply = handle_message(db, user, "yes", review_fn=CountingReviewer(Review([], True, True)), require_name=False)[0]
    assert "Confirmed" in reply
    assert job_for(db, user).project_type == "commercial"


def test_project_type_answer_reaches_summary_and_job(db, user):
    refined(db, user, **KNOWN_PREFS)
    reviewer = CountingReviewer(Review([], True, True))
    handle_message(db, user, "yes", review_fn=reviewer, require_name=False)
    answer = IntakeUpdate(**BASE, project_type="residential")
    summary = handle_message(db, user, "it is my home", model_fn=scripted(answer), require_name=False)[0]
    assert "Project type: residential" in summary
    assert "Confirmed" in handle_message(db, user, "yes", review_fn=reviewer, require_name=False)[0]
    assert job_for(db, user).project_type == "residential"


def test_unknown_project_type_never_overwrites_a_known_one(db, user):
    refined(db, user, project_type="commercial", **KNOWN_PREFS)
    handle_message(db, user, "also the gate is rusty", model_fn=scripted(IntakeUpdate(**BASE, project_type="unknown")), require_name=False)
    assert db.query(IntakeSession).filter_by(user_id=user.id).one().draft["project_type"] == "commercial"


def test_places_query_leads_with_the_project_type():
    from services.places_service import build_query

    assert build_query("electrician", "commercial") == "commercial electrician"
    assert build_query("electrician", "residential") == "residential electrician"
    assert build_query("electrician", None) == "electrician"
    assert build_query("electrician", "unknown") == "electrician"


def test_supplier_email_states_the_project_type():
    job = SimpleNamespace(
        category="electrician", title="Rewire", description="Rewire the office.", location_text="Carlton 3053",
        budget_min=None, budget_max=None, deadline=None, timeframe=None, project_type="commercial", ref_prefix="ABC",
    )
    assert "Project type: commercial." in build_body(job, SimpleNamespace(name="Acme Electrical"), "ABC-1", "Irene")
    job.project_type = None
    assert "Project type" not in build_body(job, SimpleNamespace(name="Acme Electrical"), "ABC-1", "Irene")


# ---- attachment suggestions ------------------------------------------------------------------------------------
def test_suggestions_are_shown_even_when_nothing_else_is_missing(db, user):
    refined(db, user, **KNOWN_PREFS)
    reply = handle_message(db, user, "yes", review_fn=CountingReviewer(Review([], True, False, SUBDIVISION)), require_name=False)[0]
    assert "suppliers will quote more accurately if you can attach" in reply
    assert "- Engineering drawings (PDF): they show the proposed lot layout and services" in reply
    assert "- A copy of the title" in reply
    assert "attach files with the paperclip" in reply and "SEND AS IS" in reply
    assert db.query(IntakeSession).filter_by(user_id=user.id).one().state == "collecting"
    assert db.query(Job).filter_by(user_id=user.id).count() == 0


def test_suggestions_come_after_the_questions_in_one_message(db, user):
    refined(db, user)  # no preferences known
    reviewer = CountingReviewer(Review(["How many lots?"], True, False, SUBDIVISION))
    reply = handle_message(db, user, "yes", review_fn=reviewer, require_name=False)[0]
    assert reply.index("How many lots?") < reply.index("It would also help suppliers quote accurately if you can attach")
    assert "Engineering drawings (PDF)" in reply


def test_attaching_the_suggested_file_moves_on_and_the_review_does_not_repeat(db, user, tmp_path):
    refined(db, user, **KNOWN_PREFS)
    reviewer = CountingReviewer(Review([], True, False, SUBDIVISION))
    handle_message(db, user, "yes", review_fn=reviewer, require_name=False)

    drawing = tmp_path / "ab12cd34_Subdivision Drawings.pdf"
    drawing.write_bytes(b"%PDF-1.4 stub")
    summary = handle_message(
        db, user, "here are the drawings", [str(drawing)],
        model_fn=scripted(IntakeUpdate(**BASE, **KNOWN_PREFS)), require_name=False,
    )[0]
    assert "Attachments: 1 document" in summary
    assert "Confirmed" in handle_message(db, user, "yes", review_fn=reviewer, require_name=False)[0]
    assert reviewer.calls == 1


def test_reviewer_is_told_exactly_what_is_already_attached():
    text = supplier_review.describe_attachments(["/x/ab12cd34_Structural Drawings.pdf", "/x/aaaaaaaaaaaa.jpg", "/x/bbbbbbbbbbbb.jpg"])
    assert "2 photo(s)" in text and "documents: Structural Drawings.pdf" in text
    assert supplier_review.describe_attachments([]) == "none"
    assert supplier_review.describe_attachments(0) == "none" and supplier_review.describe_attachments(3) == "3 attachment(s)"
    described = supplier_review._describe({"category": "surveyor", "project_type": "residential"}, ["/x/ab12cd34_Plan.pdf"])
    assert "Already attached: documents: Plan.pdf" in described and "Project type: residential" in described


def test_review_function_receives_the_attachment_paths(db, user, tmp_path):
    seen = {}

    def spy(draft, attachments):
        seen["attachments"] = attachments
        return Review([], True)

    photo = tmp_path / "p.jpg"
    photo.write_bytes(b"x")
    handle_message(db, user, "repaint my fence", [str(photo)], model_fn=scripted(IntakeUpdate(**BASE, **KNOWN_PREFS)), require_name=False)
    handle_message(db, user, "yes", review_fn=spy, require_name=False)
    assert seen["attachments"] == [str(photo)]


def test_suggestion_lines_are_cleaned_and_capped():
    lines = supplier_review.attachment_lines({"attachment_suggestions": [
        {"item": "Drawings", "why": "layout", "kind": "document"},
        {"item": "  ", "why": "x", "kind": "photo"},
        {"item": "Photo of the site", "why": "", "kind": "photo"},
        {"item": "Title", "why": "boundaries", "kind": "document"},
        {"item": "Fifth", "why": "x", "kind": "photo"},
    ]})
    assert lines == ["Drawings: layout", "Photo of the site", "Title: boundaries"]


def test_message_layout_for_each_combination():
    full = intake.format_review_message(["Q1?", "Q2?"], ["Pref 1?", "Pref 2?"], ["Drawings: layout"])
    assert full.index("here's what a supplier would want to know") < full.index("To find the right suppliers:") < full.index("It would also help")
    assert "1. Q1?" in full and "2. Q2?" in full and "3. Pref 1?" in full and "4. Pref 2?" in full  # numbered continuously
    assert "- Drawings: layout" in full and "paperclip" in full

    only_prefs = intake.format_review_message([], ["Pref 1?"], [])
    assert only_prefs.startswith("Before I send this out, a few quick things to find the right suppliers:") and "1. Pref 1?" in only_prefs

    only_suggestions = intake.format_review_message([], [], ["Drawings: layout"])
    assert only_suggestions.startswith("Before I send this out: suppliers will quote more accurately") and "1." not in only_suggestions

    no_suggestions = intake.format_review_message(["Q1?"], [])
    assert "paperclip" not in no_suggestions and "Q1?" in no_suggestions and "To find the right suppliers" not in no_suggestions