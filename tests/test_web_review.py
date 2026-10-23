"""The supplier review on the website: questions arrive one at a time with tappable choices."""
from db.models import ChatSession, Job
from db.session import get_session
from server import web_chat
from server.agent import supplier_review
from server.agent.supplier_review import Review
from tests.test_web_chat import (  # reuse the website helpers and fixtures
    client, contact, fake_model, fresh_limits, refined_chat, say, start,
)

__all__ = ["client", "fake_model", "fresh_limits"]  # fixtures imported for pytest

CONDITION = {"label": "Existing paint", "question": "What condition is the existing paint in?",
             "options": ["Peeling or flaking", "Just faded", "Bare timber"], "multi": False}
TRIM = {"label": "Trim", "question": "Which trim is included?", "options": ["Eaves", "Fascia", "Gutters"], "multi": True}
DRAWINGS = ("Engineering drawings (PDF): they show the proposed layout",)


def reviewer_returning(monkeypatch, review):
    calls = []

    def fake(draft, attachments):
        calls.append(draft)
        return review

    monkeypatch.setattr(supplier_review, "review_request", fake)
    return calls


def job_for(token):
    db = get_session()
    try:
        chat = db.query(ChatSession).filter_by(token_hash=web_chat.hash_token(token)).one()
        return db.query(Job).filter_by(user_id=chat.user_id).one()
    finally:
        db.close()


def test_yes_returns_the_first_question_with_tappable_choices(client, monkeypatch):
    reviewer_returning(monkeypatch, Review([CONDITION, TRIM], True))
    token = refined_chat(client)
    r = say(client, token, "yes").json()

    assert "1 of 2: What condition is the existing paint in?" in r["replies"][0]
    assert "Which trim" not in r["replies"][0]  # asked in turn, not all at once
    assert "1)" not in r["replies"][0]  # the website shows buttons, not a numbered list
    assert r["choices"] == {"kind": "pro", "label": "Existing paint", "options": CONDITION["options"], "multi": False,
                            "typical": None, "photo_estimable": False, "photos_attached": False}
    assert r["state"] == "collecting" and r["review_pending"] is True and r["needs_details"] is True


def test_answering_by_tapping_an_option_moves_through_to_contact_details_and_the_job(client, monkeypatch):
    calls = reviewer_returning(monkeypatch, Review([CONDITION, TRIM], True))
    token = refined_chat(client)
    say(client, token, "yes")

    second = say(client, token, "Peeling or flaking").json()
    assert second["replies"][0].startswith("2 of 2: Which trim is included?")
    assert second["choices"]["multi"] is True and second["choices"]["options"] == TRIM["options"]

    summary = say(client, token, "Eaves, Gutters").json()
    assert summary["state"] == "awaiting_confirm" and summary["choices"] is None
    assert "- Existing paint: Peeling or flaking" in summary["replies"][0] and "- Trim: Eaves, Gutters" in summary["replies"][0]

    assert say(client, token, "yes").json()["state"] == "need_details"
    assert contact(client, token).json()["state"] == "confirmed"
    description = job_for(token).description
    assert "- Existing paint: Peeling or flaking" in description and "- Trim: Eaves, Gutters" in description
    assert len(calls) == 1  # the review ran once


def test_a_refreshed_page_gets_the_open_question_back(client, monkeypatch):
    reviewer_returning(monkeypatch, Review([CONDITION, TRIM], True))
    token = refined_chat(client)
    say(client, token, "yes")
    say(client, token, "Just faded")

    state = client.get("/api/chat/state", params={"token": token}).json()
    assert state["choices"]["label"] == "Trim" and state["state"] == "collecting" and state["review_pending"] is True


def test_no_choices_when_no_question_is_open(client, monkeypatch):
    reviewer_returning(monkeypatch, Review([CONDITION], True))
    token = refined_chat(client)
    assert client.get("/api/chat/state", params={"token": token}).json()["choices"] is None  # at the summary
    say(client, token, "yes")
    assert client.get("/api/chat/state", params={"token": token}).json()["choices"] is not None
    say(client, token, "Just faded")
    assert client.get("/api/chat/state", params={"token": token}).json()["choices"] is None  # all answered


def test_the_attachment_step_is_flagged_so_the_page_can_offer_attach_files(client, monkeypatch):
    reviewer_returning(monkeypatch, Review([], True, False, DRAWINGS))
    token = refined_chat(client)
    r = say(client, token, "yes").json()
    assert r["choices"]["kind"] == "attach" and r["choices"]["options"] == ["Continue without attaching"]
    assert "Engineering drawings (PDF)" in r["replies"][0] and "paperclip" in r["replies"][0]

    done = say(client, token, "Continue without attaching").json()
    assert done["state"] == "awaiting_confirm"


def test_send_as_is_mid_review_keeps_the_answers_so_far(client, monkeypatch):
    reviewer_returning(monkeypatch, Review([CONDITION, TRIM], True))
    token = refined_chat(client)
    say(client, token, "yes")
    say(client, token, "Bare timber")

    r = say(client, token, "Send as is").json()
    assert r["state"] == "need_details" and r["choices"] is None and r["review_pending"] is False
    assert contact(client, token).json()["state"] == "confirmed"
    description = job_for(token).description
    assert "- Existing paint: Bare timber" in description and "Trim" not in description


def test_not_sure_skips_a_question_without_recording_anything(client, monkeypatch):
    reviewer_returning(monkeypatch, Review([CONDITION, TRIM], True))
    token = refined_chat(client)
    say(client, token, "yes")
    say(client, token, "Not sure")
    summary = say(client, token, "Fascia").json()["replies"][0]
    assert "Existing paint" not in summary and "- Trim: Fascia" in summary
