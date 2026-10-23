"""'I don't know': the typical-value shortcut and the photo estimate (confirmed by the customer, capped, cheap)."""
import base64
import io
from types import SimpleNamespace

import pytest
from PIL import Image

import config
from db.models import IntakeSession, Job
from server.agent import photo_estimate, review_flow
from server.agent.intake import IntakeUpdate, handle_message
from server.agent.photo_estimate import Estimate
from server.agent.supplier_review import Review, pertinent_questions

BASE = dict(category="concreter", title="Replace driveway", description="Replace the driveway.", location_text="Coburg 3058")
KNOWN_PREFS = dict(distance_preference="local", max_distance_km=10, rating_preference="no_preference", timeframe="ASAP")


def Wq(label="Driveway width", question="How wide is the driveway?", options=(), estimable=True, typical="about 3 m wide (single car)"):
    return {"label": label, "question": question, "options": list(options), "multi": False, "photo_estimable": estimable, "typical": typical}


WIDTH = Wq()
PLAIN = Wq("Council crossover", "Does the job include the council crossover?", ["Yes", "No"], estimable=False, typical="")
GOOD = Estimate(True, "about 3 to 3.5 m wide", "a parked car beside it gives the scale", "medium")


class FakeEstimator:
    def __init__(self, result=GOOD):
        self.result, self.calls = result, []

    def __call__(self, question, draft, photos):
        self.calls.append(SimpleNamespace(question=question, draft=draft, photos=list(photos)))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class Reviewer:
    def __init__(self, *questions):
        self.review = Review(list(questions), True)

    def __call__(self, draft, attachments):
        return self.review


def scripted(*updates):
    queue = list(updates)
    return lambda draft, transcript, text, n: queue.pop(0)


def jpg(tmp_path, name="drive.jpg", size=(400, 300)):
    path = tmp_path / name
    Image.new("RGB", size, (120, 120, 120)).save(path, format="JPEG")
    return str(path)


def start(db, user, *questions, photos=()):
    """A chat refined to the summary, then 'yes' -> the first review question. Returns (reviewer, estimator)."""
    handle_message(db, user, "replace my driveway", list(photos), model_fn=scripted(IntakeUpdate(**BASE, **KNOWN_PREFS)), require_name=False)
    reviewer, estimator = Reviewer(*questions), FakeEstimator()
    handle_message(db, user, "yes", review_fn=reviewer, require_name=False, render_options=False, estimate_fn=estimator)
    return reviewer, estimator


def say(db, user, text, estimator, media=(), render_options=False):
    return handle_message(db, user, text, list(media), require_name=False, render_options=render_options, estimate_fn=estimator)


def session_of(db, user):
    return db.query(IntakeSession).filter_by(user_id=user.id).one()


# ---- the choices the page renders ---------------------------------------------------------------------------------
def test_choices_offer_the_typical_value_and_the_photo_estimate(db, user, tmp_path):
    start(db, user, WIDTH)
    c = review_flow.current_choices(session_of(db, user))
    assert c["typical"] == "about 3 m wide (single car)" and c["photo_estimable"] is True and c["photos_attached"] is False


def test_photos_attached_is_reported(db, user, tmp_path):
    start(db, user, WIDTH, photos=[jpg(tmp_path)])
    assert review_flow.current_choices(session_of(db, user))["photos_attached"] is True


def test_a_pdf_does_not_count_as_a_photo_for_estimating(db, user, tmp_path):
    pdf = tmp_path / "plan.pdf"
    pdf.write_bytes(b"%PDF-1.4 stub")
    start(db, user, WIDTH, photos=[str(pdf)])
    assert review_flow.current_choices(session_of(db, user))["photos_attached"] is False


def test_questions_that_a_photo_cannot_answer_do_not_offer_an_estimate(db, user):
    start(db, user, PLAIN)
    c = review_flow.current_choices(session_of(db, user))
    assert c["photo_estimable"] is False and c["typical"] is None


# ---- "typical" ------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("reply", ["Use typical: about 3 m wide (single car)", "typical", "Typical"])
def test_typical_is_recorded_as_unmeasured(db, user, reply):
    _, estimator = start(db, user, WIDTH)
    summary = say(db, user, reply, estimator)[0]
    assert "- Driveway width: about 3 m wide (single car) (typical for this kind of property, not measured)" in summary
    assert estimator.calls == []  # no AI call at all


def test_typical_is_just_free_text_when_the_question_has_none(db, user):
    _, estimator = start(db, user, Wq(typical=""))
    summary = say(db, user, "typical", estimator)[0]
    assert "- Driveway width: typical" in summary


# ---- estimate from a photo ------------------------------------------------------------------------------------------
def test_estimate_is_proposed_not_silently_recorded_then_confirmed(db, user, tmp_path):
    photo = jpg(tmp_path)
    _, estimator = start(db, user, WIDTH, photos=[photo])

    reply = say(db, user, "Estimate from my photo", estimator)[0]
    assert "From your photo, it looks about 3 to 3.5 m wide (a parked car beside it gives the scale)." in reply
    assert "rough estimate" in reply and "Does that look right?" in reply
    assert review_flow.current_choices(session_of(db, user)) == {
        "kind": "confirm", "label": "Driveway width", "options": ["Yes, use that", "No, I'll enter it myself"],
        "multi": False, "typical": None, "photo_estimable": False, "photos_attached": True,
    }
    assert "Driveway width" not in str(session_of(db, user).draft.get("_details"))  # nothing recorded until confirmed

    assert len(estimator.calls) == 1
    call = estimator.calls[0]
    assert call.question["label"] == "Driveway width" and call.photos == [photo]
    assert not [k for k in call.draft if k.startswith("_")]  # the estimator never sees internal state

    summary = say(db, user, "Yes, use that", estimator)[0]
    assert "- Driveway width: about 3 to 3.5 m wide (estimated from photo, to be confirmed on site)" in summary

    assert "Confirmed" in say(db, user, "yes", estimator)[0]
    job = db.query(Job).filter_by(user_id=user.id).one()
    assert "(estimated from photo, to be confirmed on site)" in job.description  # suppliers are told it is not a measurement


def test_rejecting_the_estimate_returns_to_the_original_question_without_offering_it_again(db, user, tmp_path):
    _, estimator = start(db, user, WIDTH, photos=[jpg(tmp_path)])
    say(db, user, "Estimate from my photo", estimator)
    reply = say(db, user, "No, I'll enter it myself", estimator)[0]
    assert reply.startswith("No problem.") and "How wide is the driveway?" in reply
    choices = review_flow.current_choices(session_of(db, user))
    assert choices["kind"] == "pro" and choices["photo_estimable"] is False  # one attempt per question

    assert "- Driveway width: 3.2 m" in say(db, user, "3.2 m", estimator)[0]
    assert len(estimator.calls) == 1


def test_typing_an_answer_while_an_estimate_is_waiting_overrides_it(db, user, tmp_path):
    _, estimator = start(db, user, WIDTH, photos=[jpg(tmp_path)])
    say(db, user, "Estimate from my photo", estimator)
    summary = say(db, user, "Actually it is 4 m", estimator)[0]
    assert "- Driveway width: Actually it is 4 m" in summary and "estimated" not in summary


def test_the_proposal_can_be_answered_by_number_on_whatsapp(db, user, tmp_path):
    _, estimator = start(db, user, WIDTH, photos=[jpg(tmp_path)])
    reply = say(db, user, "estimate from my photo", estimator, render_options=True)[0]
    assert "1) Yes, use that" in reply and "2) No, I'll enter it myself" in reply
    assert "(estimated from photo" in say(db, user, "1", estimator, render_options=True)[0]


def test_sending_a_photo_with_no_text_on_an_estimable_question_triggers_the_estimate(db, user, tmp_path):
    _, estimator = start(db, user, WIDTH)  # no photo yet
    photo = jpg(tmp_path)
    session = session_of(db, user)
    session.photo_paths = [photo]  # (the intake layer appends uploads before calling the flow)
    reply = say(db, user, "", estimator, media=[photo])[0]
    assert "From your photo" in reply and len(estimator.calls) == 1


def test_a_photo_on_a_question_a_photo_cannot_answer_is_just_acknowledged(db, user, tmp_path):
    _, estimator = start(db, user, PLAIN)
    photo = jpg(tmp_path)
    reply = say(db, user, "", estimator, media=[photo])[0]
    assert reply.startswith("Got it, thanks.") and "council crossover" in reply
    assert estimator.calls == []


def test_asking_to_estimate_with_no_photo_says_so_and_costs_nothing(db, user):
    _, estimator = start(db, user, WIDTH)
    reply = say(db, user, "Estimate from my photo", estimator)[0]
    assert "I need a photo first" in reply and "How wide is the driveway?" in reply
    assert estimator.calls == []
    assert review_flow.current_choices(session_of(db, user))["photo_estimable"] is True  # still available once they attach one


def test_asking_to_estimate_something_a_photo_cannot_show_is_refused(db, user, tmp_path):
    _, estimator = start(db, user, PLAIN, photos=[jpg(tmp_path)])
    assert "can't estimate that one from a photo" in say(db, user, "Estimate from my photo", estimator)[0]
    assert estimator.calls == []


def test_an_estimate_that_cannot_be_made_is_handled_kindly_and_not_retried(db, user, tmp_path):
    _, estimator = start(db, user, WIDTH, photos=[jpg(tmp_path)])
    estimator.result = Estimate(False, "", "", "low")
    reply = say(db, user, "Estimate from my photo", estimator)[0]
    assert "couldn't work that out reliably" in reply and "How wide is the driveway?" in reply
    assert review_flow.current_choices(session_of(db, user))["photo_estimable"] is False
    assert "can't estimate that one from a photo" in say(db, user, "Estimate from my photo", estimator)[0]
    assert len(estimator.calls) == 1


def test_an_estimator_that_raises_never_breaks_the_chat(db, user, tmp_path):
    _, estimator = start(db, user, WIDTH, photos=[jpg(tmp_path)])
    estimator.result = RuntimeError("api down")
    assert "couldn't work that out reliably" in say(db, user, "Estimate from my photo", estimator)[0]
    assert "- Driveway width: 3 m" in say(db, user, "3 m", estimator)[0]


def test_estimates_per_request_are_capped(db, user, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MAX_ESTIMATES_PER_REQUEST", 1)
    second = Wq("Block width", "How wide is the block?", typical="")
    _, estimator = start(db, user, WIDTH, second, photos=[jpg(tmp_path)])
    say(db, user, "Estimate from my photo", estimator)
    say(db, user, "Yes, use that", estimator)  # moves on to the second question
    reply = say(db, user, "Estimate from my photo", estimator)[0]
    assert "used up my photo estimates" in reply and "How wide is the block?" in reply
    assert len(estimator.calls) == 1


# ---- WhatsApp hints ----------------------------------------------------------------------------------------------------
def test_whatsapp_text_mentions_the_typical_shortcut_and_photo_estimate(db, user):
    handle_message(db, user, "replace my driveway", model_fn=scripted(IntakeUpdate(**BASE, **KNOWN_PREFS)), require_name=False)
    first = handle_message(db, user, "yes", review_fn=Reviewer(WIDTH), require_name=False, render_options=True)[0]
    assert 'Reply "typical" to use: about 3 m wide (single car).' in first
    assert "send a photo and I'll estimate it" in first


def test_website_text_has_no_hints_because_they_are_buttons(db, user):
    handle_message(db, user, "replace my driveway", model_fn=scripted(IntakeUpdate(**BASE, **KNOWN_PREFS)), require_name=False)
    first = handle_message(db, user, "yes", review_fn=Reviewer(WIDTH), require_name=False, render_options=False)[0]
    assert "typical" not in first and "estimate it" not in first


# ---- the reviewer flags these per question -----------------------------------------------------------------------------
def test_reviewer_questions_carry_the_photo_and_typical_flags():
    gap = {"label": "Driveway width", "question": "How wide is the driveway?", "options": [], "multi": False, "importance": "essential",
           "photo_estimable": True, "typical_answer": " about 3 m wide (single car) ", "why": "x"}
    q = pertinent_questions({"gaps": [gap]})[0]
    assert q["photo_estimable"] is True and q["typical"] == "about 3 m wide (single car)"
    bare = pertinent_questions({"gaps": [{"question": "Q?", "importance": "essential"}]})[0]
    assert bare["photo_estimable"] is False and bare["typical"] == ""


# ---- the estimator: cheap by construction, and never raises ---------------------------------------------------------------
def test_image_paths_keeps_only_photos():
    assert photo_estimate.image_paths(["/a/x.jpg", "/a/y.PDF", "/a/z.png", "/a/w.webp", "/a/v.gif"]) == ["/a/x.jpg", "/a/z.png", "/a/w.webp"]
    assert photo_estimate.image_paths(None) == []


def test_photos_are_downscaled_before_being_sent(tmp_path):
    big = tmp_path / "big.jpg"
    Image.new("RGB", (4000, 3000), (10, 20, 30)).save(big, format="JPEG")
    block = photo_estimate.encode_photo(str(big))
    assert block["type"] == "image" and block["source"]["media_type"] == "image/jpeg"
    sent = Image.open(io.BytesIO(base64.b64decode(block["source"]["data"])))
    assert max(sent.size) <= photo_estimate.MAX_EDGE_PX  # ~1,000px: roughly a thousand tokens, not several thousand


def test_an_unreadable_photo_is_skipped_not_fatal(tmp_path):
    bad = tmp_path / "bad.jpg"
    bad.write_bytes(b"not an image")
    assert photo_estimate.encode_photo(str(bad)) is None
    assert photo_estimate.encode_photo(str(tmp_path / "missing.jpg")) is None


class StubClient:
    def __init__(self, tool_input=None, raises=None):
        self.tool_input, self.raises, self.kwargs = tool_input, raises, None
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.kwargs = kwargs
        if self.raises:
            raise self.raises
        content = [SimpleNamespace(type="tool_use", input=self.tool_input)] if self.tool_input is not None else []
        return SimpleNamespace(content=content)


def test_estimator_uses_the_cheap_model_at_most_three_photos_and_a_forced_tool(tmp_path, monkeypatch):
    stub = StubClient({"can_estimate": True, "answer": "about 3 m wide", "basis": "a car for scale", "confidence": "medium"})
    monkeypatch.setattr(photo_estimate, "_client", lambda: stub)
    photos = [jpg(tmp_path, f"p{i}.jpg") for i in range(5)]
    est = photo_estimate.estimate_from_photos({"question": "How wide?"}, {"category": "concreter", "description": "driveway"}, photos)
    assert est == Estimate(True, "about 3 m wide", "a car for scale", "medium")
    assert stub.kwargs["model"] == config.ESTIMATE_MODEL == "claude-haiku-5-5"
    content = stub.kwargs["messages"][0]["content"]
    assert [b["type"] for b in content].count("image") == photo_estimate.MAX_PHOTOS == 3
    assert stub.kwargs["tool_choice"] == {"type": "tool", "name": "report_estimate"} and stub.kwargs["max_tokens"] <= 300


def test_estimator_with_no_usable_photo_makes_no_api_call(tmp_path, monkeypatch):
    def boom():
        raise AssertionError("must not call the API without a photo")

    monkeypatch.setattr(photo_estimate, "_client", boom)
    pdf = tmp_path / "plan.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    assert photo_estimate.estimate_from_photos({"question": "q"}, {}, [str(pdf)]).ok is False
    assert photo_estimate.estimate_from_photos({"question": "q"}, {}, []).ok is False


@pytest.mark.parametrize("stub", [
    StubClient(raises=RuntimeError("api down")),
    StubClient(tool_input=None),
    StubClient({"can_estimate": False, "answer": "", "basis": "", "confidence": "low"}),
    StubClient({"can_estimate": True, "answer": "   ", "basis": "", "confidence": "low"}),
])
def test_estimator_failures_become_a_polite_not_ok(tmp_path, monkeypatch, stub):
    monkeypatch.setattr(photo_estimate, "_client", lambda: stub)
    assert photo_estimate.estimate_from_photos({"question": "q"}, {}, [jpg(tmp_path)]).ok is False
