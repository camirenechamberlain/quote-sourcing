"""The automated follow-ups: what is due and when, that it never double-sends, and that every safety limit holds.

The test database is shared between tests, so each test builds its own job with unique email addresses and only looks at its own."""
import datetime
import secrets
from pathlib import Path
from types import SimpleNamespace

import pytest

from db.models import Company, Job, Notification, Quote, ReplyEmail, RfqSend, User, Visit
from db.session import get_session
from server import supplier_response
from services import followups, outbound, timeutil
from services.notify import suppress

H = datetime.timedelta
BASE_NOW = datetime.datetime(2026, 11, 3, 23, 0)  # 10:00 on Wednesday 4 Nov in Melbourne: inside business hours
LOCAL_NOW = timeutil.utc_to_local(BASE_NOW)


def test_the_reference_time_is_inside_business_hours():
    assert LOCAL_NOW.weekday() == 2 and followups.in_business_hours(LOCAL_NOW)


@pytest.fixture()
def mail(monkeypatch):
    """Live mode with the transport replaced. Each item is (to, subject, body, extra_files)."""
    sent = []
    monkeypatch.setattr(outbound, "mode", lambda: "live")

    def fake(to, subject, body, attachment_paths=None, extra_files=None):
        sent.append((to, subject, body, extra_files or []))
        return outbound.Delivery(True, "m1", "t1", to, False)

    monkeypatch.setattr(outbound, "deliver", fake)
    return sent


@pytest.fixture()
def world():
    def make(sent_ago=H(days=4), token=True, test_mode=False, customer_email=True):
        db = get_session()
        try:
            tag = secrets.token_hex(4)
            user = User(whatsapp_number=f"web:fu{tag}", name="Irene", mobile=f"+6140{secrets.randbelow(10**7):07d}",
                        email=f"cust-{tag}@example.com" if customer_email else None)
            db.add(user)
            db.flush()
            job = Job(title=f"Driveway {tag}", description="Replace the driveway.", category="concreter", location_text="Fitzroy VIC 3065",
                      user_id=user.id, status="QUEUED")
            db.add(job)
            db.flush()
            company = Company(job_id=job.id, place_id=f"p-{tag}", name=f"Acme {tag}", discovered_email=f"acme-{tag}@example.com")
            db.add(company)
            db.flush()
            rfq = RfqSend(job_id=job.id, company_id=company.id, reference_code=f"{job.ref_prefix}-X", recipient_email=f"acme-{tag}@example.com",
                          subject=f"Request for Quote {tag}", body="b", status="sent", sent_at=BASE_NOW - sent_ago, test_mode=test_mode,
                          response_token=secrets.token_urlsafe(16) if token else None)
            db.add(rfq)
            db.commit()
            return SimpleNamespace(job=job.id, rfq=rfq.id, company=company.id, token=rfq.response_token, public=job.public_token,
                                   supplier=f"acme-{tag}@example.com", customer=user.email, tag=tag)
        finally:
            db.close()
    return make


def due(w, now=BASE_NOW):
    db = get_session()
    try:
        return [p for p in followups.plan(db, now) if p.job_id == w.job]
    finally:
        db.close()


def run(w=None, now=BASE_NOW, send=True):
    db = get_session()
    try:
        return followups.run(db, send=send, now=now)
    finally:
        db.close()


def respond(w, kind="quote", **over):
    """The supplier responds through the page, via the same code the page uses."""
    db = get_session()
    try:
        rfq, job, company = db.get(RfqSend, w.rfq), db.get(Job, w.job), db.get(Company, w.company)
        if kind == "quote":
            return supplier_response.save_response(db, rfq, job, company, kind="quote", price_type=over.get("price_type", "fixed"),
                                                   price=over.get("price", 4800.0), price_max=over.get("price_max"))
        if kind == "visit":
            start = over.get("start", LOCAL_NOW + H(days=3))
            windows = [{"start": timeutil.iso(start), "end": timeutil.iso(start + H(hours=2)), "label": timeutil.label(start, start + H(hours=2))}]
            return supplier_response.save_response(db, rfq, job, company, kind="visit", visit_windows=windows)
        return supplier_response.save_response(db, rfq, job, company, kind="decline", decline_reason="out_of_area")
    finally:
        db.close()


def set_quote(quote_id, **fields):
    db = get_session()
    q = db.get(Quote, quote_id)
    for k, v in fields.items():
        setattr(q, k, v)
    db.commit()
    db.close()


def book(w, quote_id, start, status="confirmed"):
    db = get_session()
    db.add(Visit(job_id=w.job, rfq_send_id=w.rfq, company_id=w.company, quote_id=quote_id, start_local=timeutil.iso(start),
                 end_local=timeutil.iso(start + H(hours=2)), label=timeutil.label(start, start + H(hours=2)), status=status))
    db.commit()
    db.close()


def kinds(w, **kw):
    return sorted(p.kind for p in due(w, **kw))


# ---- supplier chases ----------------------------------------------------------------------------------------------
def test_a_silent_supplier_is_chased_after_three_days_not_before(world):
    assert kinds(world(sent_ago=H(days=2, hours=23))) == []
    assert kinds(world(sent_ago=H(days=3, minutes=1))) == ["chase_1"]


def test_the_chase_email_carries_their_link_an_opt_out_and_the_customers_name(world, mail):
    w = world(sent_ago=H(days=4))
    assert run().sent >= 1
    to, subject, body, _ = next(m for m in mail if m[0] == w.supplier)
    assert subject.startswith("Following up: Request for Quote")
    assert f"https://example.test/q/{w.token}" in body and f"https://example.test/q/{w.token}/optout" in body
    assert "Irene is still looking" in body and body.rstrip().splitlines()[-3:-1] != [] and "Quoteschmote, on behalf of Irene" in body
    note = get_session().query(Notification).filter_by(rfq_send_id=w.rfq).one()
    assert note.kind == "chase_1" and note.status == "sent" and note.audience == "supplier" and note.job_id == w.job


def test_it_never_sends_the_same_chase_twice(world, mail):
    w = world(sent_ago=H(days=4))
    run()
    run()
    run()
    assert len([m for m in mail if m[0] == w.supplier]) == 1


def test_the_second_chase_waits_for_seven_days_and_two_days_after_the_first(world, mail):
    w = world(sent_ago=H(days=4))
    run()  # chase_1 now
    assert kinds(w, now=BASE_NOW + H(days=2)) == []  # 6 days old: too early
    assert kinds(w, now=BASE_NOW + H(days=3)) == ["chase_2"]  # 7 days old and 3 days after the first
    run(now=BASE_NOW + H(days=3))
    assert kinds(w, now=BASE_NOW + H(days=4)) == []  # never a third


def test_a_chase_is_skipped_when_the_first_was_late_the_gap_still_applies(world, mail):
    w = world(sent_ago=H(days=7, hours=1))  # nothing chased yet and already a week old
    assert kinds(w) == ["chase_1"]
    run()
    assert kinds(w, now=BASE_NOW + H(hours=47)) == []  # chase_2 must be at least 2 days after chase_1
    assert kinds(w, now=BASE_NOW + H(hours=49)) == ["chase_2"]


def test_stale_requests_are_left_alone(world):
    assert kinds(world(sent_ago=H(days=15))) == []


@pytest.mark.parametrize("how", ["email_reply", "quote", "visit", "decline"])
def test_a_supplier_who_has_responded_in_any_way_is_not_chased(world, how):
    w = world(sent_ago=H(days=5))
    if how == "email_reply":
        db = get_session()
        db.add(ReplyEmail(rfq_send_id=w.rfq, gmail_message_id=f"g-{w.tag}", raw_body_text="Sounds good, will quote"))
        db.commit()
        db.close()
    else:
        respond(w, how)
    assert [k for k in kinds(w) if k.startswith("chase")] == []


def test_without_a_response_link_the_chase_says_to_reply_by_email(world, mail):
    w = world(sent_ago=H(days=4), token=False)
    run()
    body = next(m[2] for m in mail if m[0] == w.supplier)
    assert "/q/" not in body and "reply to this email" in body


def test_an_opted_out_supplier_is_never_chased(world, mail):
    w = world(sent_ago=H(days=4))
    db = get_session()
    suppress(db, w.supplier.upper())
    db.commit()
    db.close()
    assert [k for k in kinds(w)] == []
    run()
    assert not [m for m in mail if m[0] == w.supplier]


def test_test_sends_are_not_chased_for_real_but_are_in_test_mode(world, monkeypatch, mail):
    w = world(sent_ago=H(days=4), test_mode=True)
    assert kinds(w) == []  # live mode: this supplier never actually received the request
    monkeypatch.setattr(outbound, "mode", lambda: "test")
    assert kinds(w) == ["chase_1"]


# ---- safety limits ------------------------------------------------------------------------------------------------
def test_nothing_is_sent_outside_business_hours_or_on_sundays(world, mail):
    w = world(sent_ago=H(days=4))
    night = datetime.datetime(2026, 11, 4, 11, 0)  # 22:00 local
    sunday = datetime.datetime(2026, 11, 7, 23, 0)  # 10:00 Sunday 8 Nov local
    assert timeutil.utc_to_local(sunday).weekday() == 6
    for when in (night, sunday):
        report = run(now=when)
        assert report.sent == 0 and "business hours" in report.note
    assert not [m for m in mail if m[0] == w.supplier]
    assert due(w, now=night)  # still due: it will go out when the hours open


def test_in_dry_run_nothing_is_sent_or_recorded(world, monkeypatch):
    w = world(sent_ago=H(days=4))
    monkeypatch.setattr(outbound, "mode", lambda: "dry_run")
    delivered = []
    monkeypatch.setattr(outbound, "deliver", lambda *a, **k: delivered.append(a))
    report = run()
    assert report.sent == 0 and "dry_run" in report.note and delivered == []
    assert get_session().query(Notification).filter_by(rfq_send_id=w.rfq).count() == 0
    assert due(w)  # and it is still due for when sending is switched on


def test_preview_mode_never_sends(world, mail):
    w = world(sent_ago=H(days=4))
    report = run(send=False)
    assert report.sent == 0 and report.note == "preview only" and not [m for m in mail if m[0] == w.supplier]
    assert any(p.job_id == w.job for p in report.planned)


def test_a_run_sends_at_most_max_per_run_and_the_rest_wait(world, mail, monkeypatch):
    for _ in range(3):
        world(sent_ago=H(days=4))
    monkeypatch.setattr(followups, "MAX_PER_RUN", 2)
    report = run()
    assert report.sent == 2 and "more are due" in report.note


def test_the_daily_cap_stops_sending(world, mail, monkeypatch):
    world(sent_ago=H(days=4))
    db = get_session()
    already = db.query(Notification).filter(Notification.status.in_(("sent", "test")), Notification.created_at >= BASE_NOW - H(days=1)).count()
    db.close()
    monkeypatch.setattr(followups, "DAILY_CAP", already)  # the day's allowance is used up
    report = run()
    assert report.sent == 0 and "cap" in report.note


def test_a_failing_mail_transport_is_recorded_and_does_not_stop_the_run(world, monkeypatch):
    w = world(sent_ago=H(days=4))
    monkeypatch.setattr(outbound, "mode", lambda: "live")

    def boom(*a, **k):
        raise RuntimeError("gmail is down")

    monkeypatch.setattr(outbound, "deliver", boom)
    report = run()
    assert report.sent == 0
    note = get_session().query(Notification).filter_by(rfq_send_id=w.rfq).one()
    assert note.status == "failed" and "gmail is down" in note.error_message
    assert due(w)  # a failure doesn't count as sent, so it is retried on the next run


# ---- customer digest ------------------------------------------------------------------------------------------------
def test_a_new_quote_produces_one_digest_to_the_customer_and_is_then_marked_told(world, mail):
    w = world(sent_ago=H(hours=5))
    respond(w, "quote", price_type="range", price=4800.0, price_max=5600.0)
    run()
    to, subject, body, _ = next(m for m in mail if m[0] == w.customer)
    assert "1 quote(s)" in subject and f"Acme {w.tag}" in body and "$4,800 to $5,600" in body
    assert f"https://example.test/j/{w.public}" in body and w.supplier not in body  # no supplier contact details
    run()
    assert len([m for m in mail if m[0] == w.customer]) == 1  # told once
    db = get_session()
    assert db.query(Quote).filter_by(job_id=w.job).one().notified_customer_at is not None


def test_a_visit_offer_in_the_digest_links_to_the_picker(world, mail):
    w = world(sent_ago=H(hours=5))
    quote = respond(w, "visit")
    run()
    body = next(m[2] for m in mail if m[0] == w.customer)
    assert "offered:" in body and f"https://example.test/j/{w.public}/visit/{quote.id}" in body


def test_declines_alone_do_not_trigger_a_digest_but_ride_along_with_one(world, mail):
    w = world(sent_ago=H(hours=5))
    respond(w, "decline")
    assert kinds(w) == []
    other = SimpleNamespace(**{**vars(w)})  # a second supplier on the same job answers with a quote
    db = get_session()
    company = Company(job_id=w.job, place_id=f"p2-{w.tag}", name=f"Beta {w.tag}", discovered_email=f"beta-{w.tag}@example.com")
    db.add(company)
    db.flush()
    rfq2 = RfqSend(job_id=w.job, company_id=company.id, reference_code="R2", recipient_email=company.discovered_email, subject="s", body="b",
                   status="sent", sent_at=BASE_NOW - H(hours=5), response_token=secrets.token_urlsafe(16))
    db.add(rfq2)
    db.commit()
    other.rfq, other.company = rfq2.id, company.id
    db.close()
    respond(other, "quote")
    run()
    body = next(m[2] for m in mail if m[0] == w.customer)
    assert "Can't help this time: 1 supplier(s)" in body and f"Beta {w.tag}" in body


def test_digests_are_throttled_to_one_every_four_hours(world, mail):
    w = world(sent_ago=H(hours=5))
    respond(w, "quote")
    run()
    other = world()  # unrelated job, only here so the shared run has something else to do
    quote = respond(w, "visit")  # replaces the earlier response for the same supplier: a fresh, un-notified one
    set_quote(quote.id, notified_customer_at=None)
    assert kinds(w, now=BASE_NOW + H(hours=3)) == []  # told about this job an hour or two ago
    assert "customer_digest" in kinds(w, now=BASE_NOW + H(hours=5))


def test_a_customer_with_no_email_gets_no_digest(world):
    w = world(sent_ago=H(hours=5), customer_email=False)
    respond(w, "quote")
    assert kinds(w) == []


def test_old_rows_without_a_kind_still_count_by_whether_they_were_a_quote():
    q = lambda **k: SimpleNamespace(kind=None, is_quote=True, **k)  # noqa: E731
    assert followups.quote_kind(q()) == "quote"
    assert followups.quote_kind(SimpleNamespace(kind=None, is_quote=False)) == "reply"
    assert followups.quote_kind(SimpleNamespace(kind="quote", is_quote=False)) == "reply"  # an emailed reply that wasn't a quote
    assert followups.quote_kind(SimpleNamespace(kind="visit", is_quote=False)) == "visit"


# ---- choosing a visit time ------------------------------------------------------------------------------------------
def told(w, quote, hours_ago):
    set_quote(quote.id, notified_customer_at=BASE_NOW - H(hours=hours_ago))


def test_the_customer_is_reminded_to_pick_a_time_after_a_day_and_again_after_three(world, mail):
    w = world(sent_ago=H(days=1))
    quote = respond(w, "visit", start=LOCAL_NOW + H(days=6))
    told(w, quote, 23)
    assert kinds(w) == []
    told(w, quote, 25)
    assert kinds(w) == ["pick_reminder_1"]
    run()
    assert kinds(w) == []
    told(w, quote, 73)
    assert kinds(w) == ["pick_reminder_2"]
    run()
    assert kinds(w) == []  # two reminders, then they're left alone


@pytest.mark.parametrize("state", ["booked", "other_times", "all_past"])
def test_no_reminder_once_a_time_is_booked_other_times_asked_or_the_offer_has_passed(world, state):
    w = world(sent_ago=H(days=1))
    quote = respond(w, "visit", start=LOCAL_NOW + H(days=6))
    told(w, quote, 30)
    if state == "booked":
        book(w, quote.id, LOCAL_NOW + H(days=6))
    elif state == "other_times":
        set_quote(quote.id, windows_declined=True)
    else:
        past = LOCAL_NOW - H(days=1)
        set_quote(quote.id, visit_windows=[{"start": timeutil.iso(past), "end": timeutil.iso(past + H(hours=2)), "label": "Yesterday"}])
    assert not [k for k in kinds(w) if k.startswith("pick_reminder")]


# ---- visit reminders and post-visit nudges -----------------------------------------------------------------------------
def test_both_sides_get_one_reminder_within_a_day_of_the_visit(world, mail):
    w = world(sent_ago=H(days=1))
    quote = respond(w, "visit")
    set_quote(quote.id, notified_customer_at=BASE_NOW)
    book(w, quote.id, LOCAL_NOW + H(hours=20))
    assert kinds(w) == ["visit_reminder_customer", "visit_reminder_supplier"]
    run()
    run()
    mine = [m for m in mail if m[0] in (w.supplier, w.customer)]
    assert len(mine) == 2
    assert next(m for m in mine if m[0] == w.supplier)[2].count("Fitzroy VIC 3065") == 1
    assert "Fitzroy VIC 3065" in next(m for m in mine if m[0] == w.customer)[2]


@pytest.mark.parametrize("start, status", [(H(hours=30), "confirmed"), (H(hours=-2), "confirmed"), (H(hours=20), "cancelled")])
def test_no_reminder_for_a_visit_that_is_too_far_off_already_started_or_cancelled(world, start, status):
    w = world(sent_ago=H(days=1))
    quote = respond(w, "visit")
    set_quote(quote.id, notified_customer_at=BASE_NOW)
    book(w, quote.id, LOCAL_NOW + start, status)
    assert not [k for k in kinds(w) if k.startswith("visit_reminder")]


def test_a_supplier_who_visited_but_has_not_quoted_is_nudged_at_two_and_six_days(world, mail):
    w = world(sent_ago=H(days=10))
    quote = respond(w, "visit")
    set_quote(quote.id, notified_customer_at=BASE_NOW - H(days=9))
    book(w, quote.id, LOCAL_NOW - H(days=2, hours=3))  # ended 2 days and an hour ago
    assert [k for k in kinds(w) if k.startswith("post_visit")] == ["post_visit_1"]
    run()
    assert [k for k in kinds(w) if k.startswith("post_visit")] == []
    assert [k for k in kinds(w, now=BASE_NOW + H(days=4)) if k.startswith("post_visit")] == ["post_visit_2"]
    body = next(m[2] for m in mail if m[0] == w.supplier and "Your quote for the site visit" in m[1])
    assert f"https://example.test/q/{w.token}/quote" in body


def test_no_post_visit_nudge_once_they_have_quoted_or_after_two_weeks(world):
    sent = world(sent_ago=H(days=10))
    q1 = respond(sent, "visit")
    book(sent, q1.id, LOCAL_NOW - H(days=3))
    respond(sent, "quote")  # sent their quote after the visit
    assert not [k for k in kinds(sent) if k.startswith("post_visit")]

    stale = world(sent_ago=H(days=20))
    q2 = respond(stale, "visit")
    book(stale, q2.id, LOCAL_NOW - H(days=14))
    assert not [k for k in kinds(stale) if k.startswith("post_visit")]


# ---- quotes waiting -------------------------------------------------------------------------------------------------
def test_the_customer_gets_one_nudge_when_quotes_have_waited_three_days(world, mail):
    w = world(sent_ago=H(days=5))
    quote = respond(w, "quote")
    set_quote(quote.id, notified_customer_at=BASE_NOW - H(days=2))
    assert "quotes_waiting" not in kinds(w)
    set_quote(quote.id, notified_customer_at=BASE_NOW - H(days=4))
    assert "quotes_waiting" in kinds(w)
    run()
    assert "quotes_waiting" not in kinds(w)
    assert any("waiting for you" in m[1] for m in mail if m[0] == w.customer)


# ---- scheduler, command line and admin page ---------------------------------------------------------------------------
def test_the_scheduler_pass_never_raises_even_when_a_run_fails(monkeypatch):
    from server import main

    monkeypatch.setattr(followups, "run", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    main.run_followups_once()  # logged, not raised: a bad pass must not kill the timer


def test_the_website_only_runs_follow_ups_when_switched_on(monkeypatch):
    import threading

    import config
    from fastapi.testclient import TestClient
    from server import main

    ran = threading.Event()
    monkeypatch.setattr(main, "run_followups_once", ran.set)
    monkeypatch.setattr(config, "FOLLOWUPS_ENABLED", False)
    with TestClient(main.app):
        assert not ran.wait(0.5)  # off by default
    monkeypatch.setattr(config, "FOLLOWUPS_ENABLED", True)
    with TestClient(main.app):
        assert ran.wait(3)


def age_to_real_clock(w, days=4):
    """The CLI and the admin page use the real clock, so re-date this test's request relative to now."""
    db = get_session()
    db.get(RfqSend, w.rfq).sent_at = datetime.datetime.utcnow() - H(days=days)
    db.commit()
    db.close()


def test_the_command_line_previews_by_default_and_never_sends(world, mail, monkeypatch, capsys):
    w = world(sent_ago=H(days=4))
    age_to_real_clock(w)
    monkeypatch.setattr("sys.argv", ["followups"])
    followups.main()
    out = capsys.readouterr().out
    assert "mode=live" in out and "sent=0" in out and "preview only" in out and w.supplier in out
    assert not [m for m in mail if m[0] == w.supplier]


def test_the_outbox_page_renders_the_mode_what_is_due_and_the_recent_emails(world):
    from streamlit.testing.v1 import AppTest

    w = world(sent_ago=H(days=4))
    age_to_real_clock(w)
    page = Path(__file__).resolve().parent.parent / "pages" / "7_Outbox.py"
    at = AppTest.from_file(str(page), default_timeout=60).run()
    assert not at.exception
    assert any("Dry run" in i.value for i in at.info)  # the test environment is pinned to dry_run
    assert {m.label for m in at.metric} >= {"Automatic follow-ups", "Runs every", "Sending hours"}
    assert any(w.supplier in str(df.value.to_dict()) for df in at.dataframe)  # what's due includes this supplier's chase


# ---- emails that depend on a link are held back without PUBLIC_BASE_URL ----------------------------------------------------
def test_customer_emails_that_need_a_link_are_held_back_when_the_site_address_is_not_set(world, monkeypatch):
    import config

    w = world(sent_ago=H(hours=5))
    respond(w, "visit")
    assert "customer_digest" in kinds(w)
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "")
    assert "customer_digest" not in kinds(w)
    assert any("PUBLIC_BASE_URL" in message for message in followups.config_warnings())


def test_a_supplier_chase_still_goes_without_a_site_address_because_replying_by_email_works(world, monkeypatch):
    import config

    w = world(sent_ago=H(days=4), token=False)
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "")
    assert kinds(w) == ["chase_1"]
