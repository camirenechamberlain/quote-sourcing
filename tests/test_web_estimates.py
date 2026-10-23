"""'Estimate from my photo' and 'Typical' through the website API."""
from db.models import ChatSession, Job
from db.session import get_session
from server import web_chat
from server.agent import photo_estimate, supplier_review
from server.agent.photo_estimate import Estimate
from server.agent.supplier_review import Review
from tests.test_web_chat import (  # reuse the website helpers and fixtures
    client, contact, fake_model, fresh_limits, jpeg_bytes, refined_chat, say, start,
)

__all__ = ["client", "fake_model", "fresh_limits"]  # fixtures imported for pytest

WIDTH = {"label": "Driveway width", "question": "How wide is the driveway?", "options": [], "multi": False,
         "photo_estimable": True, "typical": "about 3 m wide (single car)"}
GOOD = Estimate(True, "about 3 to 3.5 m wide", "a parked car beside it gives the scale", "medium")
PHOTO = ("files", ("drive.jpg", jpeg_bytes(size=(60, 40)), "image/jpeg"))


def setup(monkeypatch, estimate=GOOD):
    monkeypatch.setattr(supplier_review, "review_request", lambda draft, attachments: Review([WIDTH], True))
    calls = []

    def fake_estimate(question, draft, photos):
        calls.append(list(photos))
        return estimate

    monkeypatch.setattr(photo_estimate, "estimate_from_photos", fake_estimate)
    return calls


def job_for(token):
    db = get_session()
    try:
        chat = db.query(ChatSession).filter_by(token_hash=web_chat.hash_token(token)).one()
        return db.query(Job).filter_by(user_id=chat.user_id).one()
    finally:
        db.close()


def test_choices_tell_the_page_what_to_offer(client, monkeypatch):
    setup(monkeypatch)
    token = refined_chat(client)  # no photo attached
    choices = say(client, token, "yes").json()["choices"]
    assert choices["typical"] == "about 3 m wide (single car)"
    assert choices["photo_estimable"] is True and choices["photos_attached"] is False  # page shows "Attach a photo to estimate"


def test_estimate_with_an_attached_photo_is_proposed_confirmed_and_reaches_the_job(client, monkeypatch):
    calls = setup(monkeypatch)
    token = refined_chat(client, files=[PHOTO])
    choices = say(client, token, "yes").json()["choices"]
    assert choices["photos_attached"] is True  # page shows "Estimate from my photo"

    proposal = say(client, token, "Estimate from my photo").json()
    assert "From your photo, it looks about 3 to 3.5 m wide" in proposal["replies"][0]
    assert proposal["choices"]["kind"] == "confirm" and proposal["choices"]["options"] == ["Yes, use that", "No, I'll enter it myself"]
    assert len(calls) == 1 and len(calls[0]) == 1  # one call, with the one uploaded (and sanitised) photo

    summary = say(client, token, "Yes, use that").json()
    assert "- Driveway width: about 3 to 3.5 m wide (estimated from photo, to be confirmed on site)" in summary["replies"][0]

    assert say(client, token, "yes").json()["state"] == "need_details"
    assert contact(client, token).json()["state"] == "confirmed"
    assert "(estimated from photo, to be confirmed on site)" in job_for(token).description


def test_attaching_a_photo_with_no_text_triggers_the_estimate(client, monkeypatch):
    calls = setup(monkeypatch)
    token = refined_chat(client)  # no photo yet
    say(client, token, "yes")
    r = say(client, token, "", files=[PHOTO]).json()  # the page sends just the photo after "Attach a photo to estimate"
    assert "From your photo" in r["replies"][0] and r["choices"]["kind"] == "confirm"
    assert len(calls) == 1


def test_typical_over_the_api_needs_no_ai_call(client, monkeypatch):
    calls = setup(monkeypatch)
    token = refined_chat(client)
    say(client, token, "yes")
    summary = say(client, token, "Use typical: about 3 m wide (single car)").json()["replies"][0]
    assert "(typical for this kind of property, not measured)" in summary
    assert calls == []


def test_an_estimate_that_fails_over_the_api_is_handled_politely(client, monkeypatch):
    setup(monkeypatch, estimate=Estimate(False, "", "", "low"))
    token = refined_chat(client, files=[PHOTO])
    say(client, token, "yes")
    r = say(client, token, "Estimate from my photo").json()
    assert "couldn't work that out reliably" in r["replies"][0]
    assert r["choices"]["kind"] == "pro" and r["choices"]["photo_estimable"] is False
