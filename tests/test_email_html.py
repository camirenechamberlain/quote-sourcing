"""The HTML version of our emails: response buttons that go straight to the right form, with the plain text always alongside."""
import email
import re
from email import policy

import pytest

import config
from services import email_html, gmail_service, outbound
from services.rfq_template import build_body

TOKEN = "AbCdEfGhIjKlMnOpQrSt12"
URL = f"https://quoteschmote.com/q/{TOKEN}"
BODY = f"""Hi Acme team,

I'm writing on behalf of Irene to request a quote.

Quickest way to respond (about a minute, no sign-up): send your price, ask to see the job first, or let us know you can't help:
{URL}

Or just reply to this email.

Thanks very much,
Quoteschmote, on behalf of Irene

Not interested in requests like this? Stop them here: {URL}/optout
"""


# ---- the HTML ---------------------------------------------------------------------------------------------------------
def test_the_response_link_becomes_three_buttons_that_go_straight_to_the_right_form():
    out = email_html.render(BODY)
    for label, path in (("Send my quote", "/quote"), ("I need to see the job first", "/visit"), ("Can&#x27;t help with this one", "/decline")):
        assert f'href="{URL}{path}"' in out and label in out
    assert out.index("Send my quote") < out.index("I need to see the job first") < out.index("Can&#x27;t help")


def test_the_text_around_the_buttons_is_kept_in_order_and_the_bare_link_is_gone():
    out = email_html.render(BODY)
    assert out.index("Hi Acme team") < out.index("Quickest way to respond") < out.index("Send my quote") < out.index("Or just reply to this email")
    assert f">{URL}</a>" not in out.split("Thanks very much")[0]  # replaced by the buttons, not shown as a raw link as well


def test_other_links_stay_ordinary_links_and_trailing_punctuation_is_not_part_of_the_link():
    out = email_html.render(f"See https://quoteschmote.com/j/abc123def456, then reply.\n\nStop here: {URL}/optout.")
    assert 'href="https://quoteschmote.com/j/abc123def456"' in out and ">https://quoteschmote.com/j/abc123def456</a>," in out
    assert f'href="{URL}/optout"' in out and "Send my quote" not in out  # an opt-out or deep link on its own is not a button block


def test_an_email_without_a_response_link_gets_no_buttons():
    out = email_html.render("Hi,\n\nYour site visit is booked.\n\nQuoteschmote")
    assert "Send my quote" not in out and "Your site visit is booked." in out and "<table" not in out


def test_everything_from_the_text_is_escaped():
    out = email_html.render('Hi <script>alert(1)</script> & "co"\n\nhttps://x.test/?a=1&b=<b>')
    assert "<script>" not in out and "&lt;script&gt;alert(1)&lt;/script&gt; &amp; &quot;co&quot;" in out and "<b>" not in out


def test_line_breaks_inside_a_paragraph_are_kept():
    out = email_html.render("Could you please reply with:\n- Your price\n- What's included")
    assert "Could you please reply with:<br>- Your price<br>- What&#x27;s included" in out


def test_the_real_rfq_email_renders_with_buttons():
    from types import SimpleNamespace

    job = SimpleNamespace(description="Fix the tile.", location_text="Reservoir", budget_min=None, budget_max=None, deadline=None, project_type=None,
                          timeframe=None, ref_prefix="AB12CD34", category="tiler", title="Tile")
    body = build_body(job, SimpleNamespace(name="Acme"), "REF-1", "Irene", ["photo-1.jpg"], URL)
    out = email_html.render(body)
    assert out.count("<a ") >= 4 and f'href="{URL}/quote"' in out and "Attached for reference: photo-1.jpg" in out


# ---- the message ------------------------------------------------------------------------------------------------------------
def parsed(msg):
    return email.message_from_bytes(msg.as_bytes(), policy=policy.default)


def test_the_message_carries_both_versions_text_first_and_html_last():
    msg = parsed(gmail_service.build_mime_message("a@example.com", "S", "plain body", html="<p>html body</p>"))
    assert msg.get_content_type() == "multipart/alternative"
    kinds = [p.get_content_type() for p in msg.iter_parts()]
    assert kinds == ["text/plain", "text/html"]
    assert "plain body" in msg.get_body(("plain",)).get_content() and "html body" in msg.get_body(("html",)).get_content()


def test_with_attachments_the_alternative_sits_inside_a_mixed_message(tmp_path):
    photo = tmp_path / "abcdef123456.jpg"
    photo.write_bytes(b"\xff\xd8\xff\xe0fake")
    msg = parsed(gmail_service.build_mime_message("a@example.com", "S", "plain", [str(photo)], [("v.ics", "text/calendar", b"BEGIN:VCALENDAR")], "<p>html</p>"))
    assert msg.get_content_type() == "multipart/mixed"
    first = next(msg.iter_parts())
    assert first.get_content_type() == "multipart/alternative"
    assert sorted(p.get_filename() for p in msg.iter_attachments()) == ["photo-1.jpg", "v.ics"]


def test_without_html_the_message_is_unchanged_plain_text():
    msg = parsed(gmail_service.build_mime_message("a@example.com", "S", "plain body"))
    assert msg.get_content_type() == "text/plain"


# ---- the mail gate -----------------------------------------------------------------------------------------------------------
@pytest.fixture()
def captured(monkeypatch):
    sent = []
    monkeypatch.setattr(gmail_service, "send_email", lambda to, subject, body, attachment_paths=None, extra_files=None, html=None:
                        sent.append({"to": to, "subject": subject, "body": body, "html": html}) or ("m1", "t1"))
    return sent


def set_mode(monkeypatch, mode, html_on=True):
    monkeypatch.setattr(config, "OUTBOUND_MODE", mode)
    monkeypatch.setattr(config, "TEST_RECIPIENT", "me@example.com")
    monkeypatch.setattr(config, "EMAIL_HTML", html_on)


def test_live_emails_get_an_html_version_with_buttons(monkeypatch, captured):
    set_mode(monkeypatch, "live")
    outbound.deliver("acme@example.com", "RFQ", BODY)
    assert captured[0]["body"] == BODY  # the text version is exactly what was reviewed
    assert f'href="{URL}/quote"' in captured[0]["html"]


def test_test_mode_html_includes_the_test_banner_so_it_cannot_be_mistaken_for_real(monkeypatch, captured):
    set_mode(monkeypatch, "test")
    outbound.deliver("acme@example.com", "RFQ", BODY)
    assert "TEST MODE" in captured[0]["body"] and "TEST MODE" in captured[0]["html"] and captured[0]["to"] == "me@example.com"


def test_html_can_be_switched_off(monkeypatch, captured):
    set_mode(monkeypatch, "live", html_on=False)
    outbound.deliver("acme@example.com", "RFQ", BODY)
    assert captured[0]["html"] is None


def test_a_dry_run_sends_nothing_whatever_the_html_setting(monkeypatch, captured):
    set_mode(monkeypatch, "dry_run")
    outbound.deliver("acme@example.com", "RFQ", BODY)
    assert captured == []
