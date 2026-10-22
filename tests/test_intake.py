from pathlib import Path

from db.models import IntakeSession, Job, WaMessage
from server.agent import intake
from server.agent.intake import FALLBACK_QUESTIONS, IntakeUpdate, handle_message


def scripted(*updates: IntakeUpdate):
    """A fake model that returns the given updates in order."""
    queue = list(updates)

    def fake(draft, transcript, user_text, n_photos):
        return queue.pop(0)

    return fake


def record_out(db, user, replies):
    """Mimic the webhook: outbound replies are logged (the name-question shortcut relies on this)."""
    for r in replies:
        db.add(WaMessage(user_id=user.id, direction="out", body=r, delivery="dry_run"))
    db.commit()


FULL = dict(
    is_job_request=True,
    category="plumber",
    title="Fix leaking bathroom tap",
    description="Bathroom vanity tap dripping constantly, needs washer or cartridge.",
    location_text="Carlton VIC 3053",
    budget_max=300,
)


def test_greeting_gets_a_question_not_a_job(db, user):
    fake = scripted(IntakeUpdate(is_job_request=False, next_question="Hi! What do you need done, and where?"))
    replies = handle_message(db, user, "hi", model_fn=fake)
    assert replies == ["Hi! What do you need done, and where?"]
    assert db.query(Job).filter_by(user_id=user.id).count() == 0


def test_missing_location_is_always_asked_even_if_model_forgets(db, user):
    fake = scripted(IntakeUpdate(category="plumber", description="Leaking tap in the bathroom", next_question=None))
    replies = handle_message(db, user, "plumber for a leaking tap", model_fn=fake)
    assert replies == [FALLBACK_QUESTIONS["location_text"]]
    assert db.query(IntakeSession).filter_by(user_id=user.id).one().state == "collecting"


def test_complete_request_asks_for_name_then_confirms_then_creates_job(db, user, tmp_path):
    photo = tmp_path / "tap.jpg"
    photo.write_bytes(b"fake-jpeg")

    fake = scripted(IntakeUpdate(**FULL, next_question=None))
    replies = handle_message(db, user, "plumber carlton leaking tap max $300", [str(photo)], model_fn=fake)
    assert replies == [FALLBACK_QUESTIONS["user_name"]]
    record_out(db, user, replies)

    # Name answered: deterministic path, the model is not called again.
    replies = handle_message(db, user, "Irene", model_fn=scripted())
    assert user.name == "Irene"
    assert "Reply YES to start" in replies[0]
    assert "Photos: 1" in replies[0]
    assert "up to $300" in replies[0]
    record_out(db, user, replies)

    replies = handle_message(db, user, "yes!", model_fn=scripted())
    assert "Confirmed" in replies[0]

    job = db.query(Job).filter_by(user_id=user.id).one()
    assert job.status == "QUEUED"
    assert job.category == "plumber"
    assert job.budget_max == 300
    assert len(job.attachment_paths) == 1 and Path(job.attachment_paths[0]).exists()
    assert not photo.exists()  # moved into the job's folder, not copied
    assert replies[0].count(job.ref_prefix) == 1


def test_cancel_drops_the_session(db, user):
    fake = scripted(IntakeUpdate(**FULL, user_name="Irene", next_question=None))
    handle_message(db, user, "plumber carlton leaking tap", model_fn=fake)
    replies = handle_message(db, user, "CANCEL", model_fn=scripted())
    assert "dropped" in replies[0]
    assert db.query(Job).filter_by(user_id=user.id).count() == 0
    assert db.query(IntakeSession).filter_by(user_id=user.id).one().state == "cancelled"


def test_edit_while_awaiting_confirmation_reruns_the_model(db, user):
    handle_message(db, user, "x", model_fn=scripted(IntakeUpdate(**FULL, user_name="Irene", next_question=None)))
    edited = {**FULL, "location_text": "Fitzroy VIC 3065"}
    replies = handle_message(db, user, "actually it's in Fitzroy", model_fn=scripted(IntakeUpdate(**edited, next_question=None)))
    assert "Fitzroy VIC 3065" in replies[0]
    assert "Carlton" not in replies[0]
    assert db.query(Job).filter_by(user_id=user.id).count() == 0  # still not confirmed


def test_no_means_what_would_you_change_not_cancel(db, user):
    handle_message(db, user, "x", model_fn=scripted(IntakeUpdate(**FULL, user_name="Irene", next_question=None)))
    replies = handle_message(db, user, "no", model_fn=scripted())
    assert "change" in replies[0].lower()
    assert db.query(IntakeSession).filter_by(user_id=user.id).one().state == "collecting"


def test_clarifying_questions_are_capped(db, user):
    """A chatty model must not interrogate the user forever once the required fields exist."""
    always_asks = IntakeUpdate(**FULL, user_name="Irene", next_question="Any access issues?")
    results = []
    for _ in range(intake.MAX_CLARIFYING_QUESTIONS + 2):
        results.append(handle_message(db, user, "more info", model_fn=scripted(always_asks))[0])
    questions = [r for r in results if r == "Any access issues?"]
    assert len(questions) == intake.MAX_CLARIFYING_QUESTIONS
    assert "Reply YES to start" in results[intake.MAX_CLARIFYING_QUESTIONS]


def test_photo_only_message_is_passed_to_the_model_with_photo_count(db, user, tmp_path):
    photo = tmp_path / "a.jpg"
    photo.write_bytes(b"x")
    seen = {}

    def fake(draft, transcript, user_text, n_photos):
        seen.update(user_text=user_text, n_photos=n_photos)
        return IntakeUpdate(is_job_request=False, next_question="Thanks for the photo! What needs doing?")

    replies = handle_message(db, user, "", [str(photo)], model_fn=fake)
    assert seen == {"user_text": "(sent photo only)", "n_photos": 1}
    assert replies == ["Thanks for the photo! What needs doing?"]


def test_empty_message_without_media_gets_a_nudge(db, user):
    replies = handle_message(db, user, "   ", model_fn=scripted())
    assert "Tell me what you need" in replies[0]


def test_model_failure_does_not_corrupt_state(db, user):
    def boom(*a, **k):
        raise RuntimeError("api down")

    replies = handle_message(db, user, "plumber please", model_fn=boom)
    assert "snag" in replies[0]
    assert (db.query(IntakeSession).filter_by(user_id=user.id).one().draft or {}) == {}


def test_yes_without_an_open_request_does_not_create_a_job(db, user):
    fake = scripted(IntakeUpdate(is_job_request=False, next_question="What do you need done?"))
    handle_message(db, user, "yes", model_fn=fake)
    assert db.query(Job).filter_by(user_id=user.id).count() == 0
