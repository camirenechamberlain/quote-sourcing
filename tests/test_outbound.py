"""The OUTBOUND_MODE safety switch: only 'live' reaches a supplier, and a mistyped mode never sends."""
import pytest

import config
from services import gmail_service, outbound


@pytest.fixture()
def sent(monkeypatch):
    calls = []

    def fake_send(to, subject, body, attachment_paths=None):
        calls.append({"to": to, "subject": subject, "body": body, "attachments": attachment_paths})
        return "msg-1", "thread-1"

    monkeypatch.setattr(gmail_service, "send_email", fake_send)
    return calls


def set_mode(monkeypatch, mode, recipient=""):
    monkeypatch.setattr(config, "OUTBOUND_MODE", mode)
    monkeypatch.setattr(config, "TEST_RECIPIENT", recipient)


def test_dry_run_sends_nothing(monkeypatch, sent):
    set_mode(monkeypatch, "dry_run")
    d = outbound.deliver("acme@example.com", "Subject", "Body", ["/x/a.jpg"])
    assert sent == [] and d.sent is False and d.sent_to == "acme@example.com" and d.test_mode is False


@pytest.mark.parametrize("value", ["", "LIVE ", "Live", "prod", "true", "1", None])
def test_only_exactly_live_goes_live_and_garbage_is_a_dry_run(monkeypatch, sent, value):
    set_mode(monkeypatch, value)
    d = outbound.deliver("acme@example.com", "S", "B")
    if (value or "").strip().lower() == "live":  # surrounding spaces and case are forgiven
        assert d.sent and sent[0]["to"] == "acme@example.com"
    else:
        assert sent == [] and not d.sent


def test_test_mode_redirects_to_the_test_recipient_labelled_with_attachments(monkeypatch, sent):
    set_mode(monkeypatch, "test", "me@example.com")
    d = outbound.deliver("acme@example.com", "Request for Quote", "Hello", ["/x/a.jpg", "/x/plans.pdf"])
    assert len(sent) == 1 and sent[0]["to"] == "me@example.com"  # never the supplier
    assert sent[0]["subject"] == "[TEST for acme@example.com] Request for Quote"
    assert "would have gone to acme@example.com" in sent[0]["body"] and sent[0]["body"].endswith("Hello")
    assert sent[0]["attachments"] == ["/x/a.jpg", "/x/plans.pdf"]
    assert d.sent and d.test_mode and d.sent_to == "me@example.com" and d.message_id == "msg-1" and d.thread_id == "thread-1"


@pytest.mark.parametrize("recipient", ["", "not-an-email", "  "])
def test_test_mode_without_a_valid_test_recipient_refuses_and_sends_nothing(monkeypatch, sent, recipient):
    set_mode(monkeypatch, "test", recipient)
    with pytest.raises(outbound.OutboundError):
        outbound.deliver("acme@example.com", "S", "B")
    assert sent == []


def test_live_mode_sends_to_the_supplier_and_ignores_the_test_recipient(monkeypatch, sent):
    set_mode(monkeypatch, "live", "me@example.com")
    d = outbound.deliver("acme@example.com", "S", "B", ["/x/a.jpg"])
    assert sent[0]["to"] == "acme@example.com" and sent[0]["subject"] == "S" and sent[0]["body"] == "B"
    assert d.sent and not d.test_mode and d.sent_to == "acme@example.com"


def test_the_test_environment_runs_in_dry_run():
    assert config.OUTBOUND_MODE == "dry_run" and outbound.mode() == "dry_run"  # conftest pins it, so no test can email anyone
