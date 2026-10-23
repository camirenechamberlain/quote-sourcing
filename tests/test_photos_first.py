"""Photos first: the customer is asked for photos before any questions, and the reviewer looks at them itself so it only
asks about what they can't answer."""
import pytest
from PIL import Image

import config
from server.agent import review_flow, supplier_review
from server.agent.supplier_review import Review
from server.agent.supplier_review import review_request as real_review_request  # the autouse stub replaces the module attribute
from tests.test_supplier_review import KNOWN_PREFS, CountingReviewer, Q, refined, say, session_of

PHOTO = "/tmp/uploads/drive.jpg"
SEEN = (("Driveway surface", "exposed aggregate"), ("Driveway width", "about 3 to 3.5 m"))


@pytest.fixture(autouse=True)
def photos_first_on(monkeypatch):
    monkeypatch.setattr(config, "PHOTOS_FIRST", True)


def test_yes_without_photos_asks_for_photos_before_any_question(db, user):
    refined(db, user)
    reviewer = CountingReviewer(Review([Q("Surface", "What surface?", ["Concrete", "Pavers"])], True))
    first = say(db, user, "yes", reviewer)

    assert "Before any questions" in first[0] and "paperclip" in first[0]
    assert reviewer.calls == 0  # nothing is asked, and no AI call is spent, until the photos are in (or declined)
    assert session_of(db, user).state == "collecting" and session_of(db, user).job_id is None
    choices = review_flow.current_choices(session_of(db, user))
    assert choices["kind"] == "attach" and choices["label"] == "Photos" and choices["options"] == ["Continue without photos"]


def test_photos_arrive_the_reviewer_sees_them_and_only_asks_what_is_left(db, user):
    refined(db, user, **KNOWN_PREFS)
    reviewer = CountingReviewer(Review([Q("Access", "Is there side access for a machine?", ["Yes", "No"])], True, False, (), SEEN))
    say(db, user, "yes", reviewer)
    replies = say(db, user, "", reviewer, media_paths=[PHOTO])

    assert reviewer.calls == 1
    assert "I've looked at your photos" in replies[0]
    assert "- Driveway surface: exposed aggregate" in replies[0] and "- Driveway width: about 3 to 3.5 m" in replies[0]
    assert replies[1] == "Is there side access for a machine?"  # only what the photos could not answer
    summary = say(db, user, "Yes", reviewer)[0]
    assert "Driveway surface: exposed aggregate (seen in photos, to be confirmed on site)" in summary
    assert "- Access: Yes" in summary


def test_photos_that_answer_everything_go_straight_to_the_summary(db, user):
    refined(db, user, **KNOWN_PREFS)
    reviewer = CountingReviewer(Review([], True, False, (), SEEN))
    say(db, user, "yes", reviewer)
    replies = say(db, user, "", reviewer, media_paths=[PHOTO])

    assert "Reply YES to start" in replies[0]
    assert "Driveway width: about 3 to 3.5 m (seen in photos, to be confirmed on site)" in replies[0]
    assert session_of(db, user).state == "awaiting_confirm"


@pytest.mark.parametrize("skip", ["Continue without photos", "no photos", "yes"])
def test_declining_photos_runs_the_normal_review(db, user, skip):
    refined(db, user, **KNOWN_PREFS)
    reviewer = CountingReviewer(Review([Q("Surface", "What surface?", ["Concrete", "Pavers"])], True))
    say(db, user, "yes", reviewer)
    replies = say(db, user, skip, reviewer)

    assert reviewer.calls == 1
    assert "What surface?" in replies[0] and "looked at your photos" not in replies[0]


def test_photos_already_attached_skip_the_ask_and_get_reviewed_at_once(db, user):
    refined(db, user, **KNOWN_PREFS)
    session_of(db, user).photo_paths = [PHOTO]
    reviewer = CountingReviewer(Review([Q("Surface", "What surface?", ["Concrete", "Pavers"])], True, False, (), SEEN))
    replies = say(db, user, "yes", reviewer)

    assert reviewer.calls == 1 and "Before any questions" not in replies[0]
    assert "I've looked at your photos" in replies[0]


def test_a_pdf_alone_is_not_a_photo_so_photos_are_still_requested(db, user):
    refined(db, user)
    session_of(db, user).photo_paths = ["/tmp/uploads/plans.pdf"]
    assert "Before any questions" in say(db, user, "yes", CountingReviewer(Review([], True)))[0]


def test_a_reviewer_failure_after_photos_never_blocks_the_customer(db, user):
    refined(db, user, **KNOWN_PREFS)
    reviewer = CountingReviewer(RuntimeError("boom"))
    say(db, user, "yes", reviewer)
    replies = say(db, user, "", reviewer, media_paths=[PHOTO])
    assert "Reply YES to start" in replies[0]


def test_the_review_still_runs_only_once(db, user):
    refined(db, user, **KNOWN_PREFS)
    reviewer = CountingReviewer(Review([], True))
    say(db, user, "yes", reviewer)
    say(db, user, "Continue without photos", reviewer)
    say(db, user, "yes", reviewer)  # confirm the summary
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
