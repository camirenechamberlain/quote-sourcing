"""Photos first: as soon as the job is described, the customer is asked for photos (or the ones sent are read at once), and
the reviewer looks at them itself so it only asks about what they can't answer."""
import pytest
from PIL import Image

import config
from server.agent import review_flow, supplier_review
from server.agent.intake import IntakeUpdate, handle_message
from server.agent.supplier_review import Review
from server.agent.supplier_review import review_request as real_review_request  # the autouse stub replaces the module attribute
from tests.test_supplier_review import BASE, KNOWN_PREFS, CountingReviewer, Q, say, scripted, session_of

PHOTO = "/tmp/uploads/drive.jpg"
SEEN = (("Driveway surface", "exposed aggregate"), ("Driveway width", "about 3 to 3.5 m"))


@pytest.fixture(autouse=True)
def photos_first_on(monkeypatch):
    monkeypatch.setattr(config, "PHOTOS_FIRST", True)


def described(db, user, reviewer, media_paths=None, **extra):
    """The customer's first message, once it contains everything intake needs (the job and where)."""
    return handle_message(
        db, user, "repaint my fence in brunswick", media_paths=media_paths, model_fn=scripted(IntakeUpdate(**BASE, **extra)),
        review_fn=reviewer,
    )


def test_a_described_job_without_photos_asks_for_photos_before_anything_else(db, user):
    reviewer = CountingReviewer(Review([Q("Surface", "What surface?", ["Concrete", "Pavers"])], True))
    first = described(db, user, reviewer)

    assert "Before any questions" in first[0] and "paperclip" in first[0]
    assert "Reply YES" not in first[0]  # no summary yet
    assert reviewer.calls == 0  # no AI call is spent until the photos are in (or declined)
    assert session_of(db, user).state == "collecting" and session_of(db, user).job_id is None
    choices = review_flow.current_choices(session_of(db, user))
    assert choices["kind"] == "attach" and choices["label"] == "Photos" and choices["options"] == ["Continue without photos"]


def test_photos_sent_with_the_job_are_read_straight_away_and_only_the_rest_is_asked(db, user):
    reviewer = CountingReviewer(Review([Q("Access", "Is there side access for a machine?", ["Yes", "No"])], True, False, (), SEEN))
    replies = described(db, user, reviewer, media_paths=[PHOTO], **KNOWN_PREFS)  # no YES needed first

    assert reviewer.calls == 1
    assert "I've looked at your photos" in replies[0]
    assert "- Driveway surface: exposed aggregate" in replies[0] and "- Driveway width: about 3 to 3.5 m" in replies[0]
    assert replies[1] == "Is there side access for a machine?"  # only what the photos could not answer
    summary = say(db, user, "Yes", reviewer)[0]
    assert "Driveway surface: exposed aggregate (seen in photos, to be confirmed on site)" in summary
    assert "- Access: Yes" in summary and "Reply YES to start" in summary


def test_photos_sent_after_the_prompt_are_read_then(db, user):
    reviewer = CountingReviewer(Review([Q("Access", "Is there side access?", ["Yes", "No"])], True, False, (), SEEN))
    described(db, user, reviewer, **KNOWN_PREFS)
    replies = say(db, user, "", reviewer, media_paths=[PHOTO])
    assert reviewer.calls == 1 and "I've looked at your photos" in replies[0] and replies[1] == "Is there side access?"


def test_photos_that_answer_everything_go_straight_to_the_summary_with_the_findings(db, user):
    reviewer = CountingReviewer(Review([], True, False, (), SEEN))
    replies = described(db, user, reviewer, media_paths=[PHOTO], **KNOWN_PREFS)

    assert "Reply YES to start" in replies[0]
    assert "Driveway width: about 3 to 3.5 m (seen in photos, to be confirmed on site)" in replies[0]
    assert session_of(db, user).state == "awaiting_confirm"


@pytest.mark.parametrize("skip", ["Continue without photos", "no photos", "yes"])
def test_declining_photos_runs_the_normal_review(db, user, skip):
    reviewer = CountingReviewer(Review([Q("Surface", "What surface?", ["Concrete", "Pavers"])], True))
    described(db, user, reviewer, **KNOWN_PREFS)
    replies = say(db, user, skip, reviewer)

    assert reviewer.calls == 1
    assert "What surface?" in replies[0] and "looked at your photos" not in replies[0]


def test_a_pdf_alone_is_not_a_photo_so_photos_are_still_requested(db, user):
    reviewer = CountingReviewer(Review([], True))
    assert "Before any questions" in described(db, user, reviewer, media_paths=["/tmp/uploads/plans.pdf"])[0]
    assert reviewer.calls == 0


def test_a_reviewer_failure_never_blocks_the_customer(db, user):
    replies = described(db, user, CountingReviewer(RuntimeError("boom")), media_paths=[PHOTO], **KNOWN_PREFS)
    assert "Reply YES to start" in replies[0]


def test_the_review_runs_only_once_even_after_edits_and_confirmation(db, user):
    reviewer = CountingReviewer(Review([], True))
    described(db, user, reviewer, **KNOWN_PREFS)
    say(db, user, "Continue without photos", reviewer)  # review runs here and finishes: the summary is shown
    assert session_of(db, user).state == "awaiting_confirm"
    say(db, user, "yes", reviewer)
    assert reviewer.calls == 1


# ---- the real reviewer call (with the API stubbed) ------------------------------------------------------------------
class FakeClient:
    def __init__(self, tool_input):
        self.sent, self.messages = None, self
        self._tool_input = tool_input

    def create(self, **kwargs):
        self.sent = kwargs
        block = type("Block", (), {"type": "tool_use", "input": self._tool_input})()
        return type("Response", (), {"content": [block]})()


def photo_file(tmp_path, name):
    path = tmp_path / name
    Image.new("RGB", (60, 40), (120, 120, 120)).save(path, "JPEG")
    return str(path)


TOOL_INPUT = {
    "can_give_ballpark": True, "needs_local_supplier": True, "project_type_matters": False, "attachment_suggestions": [],
    "photo_findings": [{"label": "Surface", "finding": "exposed aggregate"}, {"label": "Junk", "finding": "  "}],
    "gaps": [],
}


def test_the_reviewer_is_shown_the_photos_and_its_findings_come_back(tmp_path, monkeypatch):
    client = FakeClient(TOOL_INPUT)
    monkeypatch.setattr(supplier_review, "_client", lambda: client)
    photos = [photo_file(tmp_path, f"p{i}.jpg") for i in range(6)]
    review = real_review_request({"category": "concreter", "description": "x", "location_text": "y"}, photos + [str(tmp_path / "plans.pdf")])

    content = client.sent["messages"][0]["content"]
    assert [b["type"] for b in content].count("image") == config.MAX_REVIEW_PHOTOS  # capped: vision is the costly part
    assert content[-1]["type"] == "text" and "6 photo(s)" in content[-1]["text"]
    assert review.photo_findings == (("Surface", "exposed aggregate"),)  # blank findings dropped


def test_findings_are_ignored_when_no_photo_was_actually_shown(tmp_path, monkeypatch):
    client = FakeClient(TOOL_INPUT)  # a reviewer that "sees" things with no photos is hallucinating
    monkeypatch.setattr(supplier_review, "_client", lambda: client)
    review = real_review_request({"category": "concreter"}, [])
    assert review.photo_findings == () and all(b["type"] == "text" for b in client.sent["messages"][0]["content"])


# ---- through the website API (the scenario: a photo sent with the first message) ---------------------------------------
from tests.test_web_chat import client, fake_model, fresh_limits, jpeg_bytes, say as web_say, start  # noqa: E402

__all__ = ["client", "fake_model", "fresh_limits"]  # fixtures imported for pytest


def test_website_a_photo_sent_with_the_job_is_read_and_questioned_at_once(client, monkeypatch):
    monkeypatch.setattr(supplier_review, "review_request", lambda draft, attachments: Review(
        [Q("Access", "Is there side access?", ["Yes", "No"])], True, False, (), SEEN))
    token = start(client).json()["token"]
    r = web_say(client, token, "plumber for a leaking tap in carlton", files=[("files", ("a.jpg", jpeg_bytes(), "image/jpeg"))]).json()

    assert "I've looked at your photos" in r["replies"][0] and "- Driveway width: about 3 to 3.5 m" in r["replies"][0]
    assert r["replies"][1] == "Is there side access?"
    assert r["choices"]["kind"] == "pro" and r["choices"]["options"] == ["Yes", "No"]
    assert r["state"] == "collecting" and r["review_pending"] is True


def test_website_a_job_without_photos_gets_the_photo_prompt_with_buttons(client):
    token = start(client).json()["token"]
    r = web_say(client, token, "plumber for a leaking tap in carlton").json()
    assert "Before any questions" in r["replies"][0]
    assert r["choices"]["kind"] == "attach" and r["choices"]["options"] == ["Continue without photos"]
