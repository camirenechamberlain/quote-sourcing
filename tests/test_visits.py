"""The customer's visit-time picker, the consent it needs, the emails and calendar invites it sends, and the calendar builder itself."""
import datetime
import secrets

import pytest
from fastapi.testclient import TestClient

from db.models import Company, Job, Notification, Quote, ReplyEmail, RfqSend, User, Visit
from db.session import get_session
from server import customer_visits, main
from services import outbound, timeutil
from services.ics import build_ics
from tests.test_supplier_response import day, fresh_limits, rfq, stored, visit_data  # noqa: F401  (fixtures and helpers)

__all__ = ["fresh_limits", "rfq"]  # fixtures imported for pytest

# Unique to this run: the database is shared, and other tests count jobs per mobile number for the daily limit.
MOBILE = f"+6140{secrets.randbelow(10**7):07d}"


@pytest.fixture()
def client():
    return TestClient(main.app, follow_redirects=False)


@pytest.fixture(autouse=True)
def reset_customer_limits():
    for limiter in (customer_visits.view_limiter, customer_visits.action_limiter):
        limiter.hits.clear()


@pytest.fixture()
def mail(monkeypatch):
    """Capture what would be emailed (live mode, with the transport replaced). Each item is (to, subject, body, extra_files)."""
    sent = []
    monkeypatch.setattr(outbound, "mode", lambda: "live")

    def fake(to, subject, body, attachment_paths=None, extra_files=None):
        sent.append((to, subject, body, extra_files or []))
        return outbound.Delivery(True, "m1", "t1", to, False)

    monkeypatch.setattr(outbound, "deliver", fake)
    return sent


def visits(rfq):
    """This test's visits only (the test database is shared between tests)."""
    db = get_session()
    try:
        return db.query(Visit).filter_by(job_id=rfq[2]["job"]).order_by(Visit.created_at).all()
    finally:
        db.close()


def notes(rfq):
    db = get_session()
    try:
        return db.query(Notification).filter_by(job_id=rfq[2]["job"]).all()
    finally:
        db.close()


def offered(client, rfq, **over):
    """The supplier offers times; returns (supplier token, progress token, quote id, customer-facing visit page base)."""
    token, job_token, ids = rfq
    db = get_session()
    customer = db.get(User, db.get(Job, ids["job"]).user_id)
    customer.mobile, customer.email = MOBILE, "irene@example.com"
    db.commit()
    db.close()
    assert client.post(f"/q/{token}/visit", data=visit_data(**over)).status_code == 303
    quote = stored(ids["rfq"])[1][0]
    return token, job_token, quote.id, f"/j/{job_token}/visit/{quote.id}"


# ---- the picker -----------------------------------------------------------------------------------------------
def test_the_picker_lists_the_offered_times_and_spells_out_what_is_shared(client, rfq):
    _, _, _, base = offered(client, rfq)
    page = client.get(base).text
    assert page.count('name="slot"') == 2 and "10am to 12pm" in page and "2pm to 4:30pm" in page
    assert "Acme Concreting" in page
    assert "Irene" in page and MOBILE in page and "Fitzroy VIC 3065" in page  # exactly what will be shared
    assert "name='consent'" in page and "None of these suit" in page


def test_nothing_is_confirmed_without_a_time_and_the_consent_tick(client, rfq):
    _, _, _, base = offered(client, rfq)
    assert "choose one of the times" in client.post(base, data={"consent": "1"}).text
    assert "tick the box" in client.post(base, data={"slot": "0"}).text
    assert visits(rfq) == []


def test_confirming_books_the_visit_and_emails_both_sides_with_a_calendar_invite(client, rfq, mail):
    token, job_token, quote_id, base = offered(client, rfq)
    r = client.post(base, data={"slot": "1", "consent": "1"})
    assert r.status_code == 303 and r.headers["location"] == base

    (visit,) = visits(rfq)
    assert visit.status == "confirmed" and visit.start_local == f"{day(8)}T14:00" and visit.end_local == f"{day(8)}T16:30"

    supplier_mail = next(m for m in mail if m[0] == "acme@example.com")
    customer_mail = next(m for m in mail if m[0] == "irene@example.com")
    assert "Site visit confirmed" in supplier_mail[1] and "2pm to 4:30pm" in supplier_mail[1]
    assert MOBILE in supplier_mail[2] and "irene@example.com" in supplier_mail[2] and "Fitzroy VIC 3065" in supplier_mail[2]
    assert f"/q/{token}" in supplier_mail[2]  # their own link, to follow up
    assert "Your site visit is booked" in customer_mail[1] and "acme@example.com" not in customer_mail[2]  # supplier details stay private
    for item in (supplier_mail, customer_mail):
        name, mime, data = item[3][0]
        assert name == "site-visit.ics" and mime == "text/calendar" and b"BEGIN:VEVENT" in data
        assert f"DTSTART:{day(8).replace('-', '')}T140000".encode() in data and b"METHOD:PUBLISH" in data

    kinds = {n.kind: n for n in notes(rfq)}
    assert set(kinds) == {"visit_confirmed", "visit_confirmed_customer"} and kinds["visit_confirmed"].audience == "supplier"


def test_the_supplier_sees_the_customers_details_only_after_confirmation(client, rfq, mail):
    token, job_token, quote_id, base = offered(client, rfq)
    before = client.get(f"/q/{token}").text
    assert MOBILE not in before and "irene@example.com" not in before and "Site visit confirmed" not in before
    client.post(base, data={"slot": "0", "consent": "1"})
    after = client.get(f"/q/{token}").text
    assert "Site visit confirmed" in after and MOBILE in after and "irene@example.com" in after and f"/q/{token}/visit.ics" in after


def test_the_progress_page_shows_the_booking_and_a_link_to_manage_it(client, rfq, mail):
    _, job_token, _, base = offered(client, rfq)
    assert "Choose a time" in client.get(f"/j/{job_token}").text
    client.post(base, data={"slot": "0", "consent": "1"})
    page = client.get(f"/j/{job_token}").text
    assert "Visit booked" in page and "Details and calendar" in page and "10am to 12pm" in page


def test_confirming_a_different_time_replaces_the_booking(client, rfq, mail):
    _, _, _, base = offered(client, rfq)
    client.post(base, data={"slot": "0", "consent": "1"})
    client.post(base, data={"slot": "1", "consent": "1"})  # the page still lets them choose another time
    statuses = sorted((v.start_local[-5:], v.status) for v in visits(rfq))
    assert statuses == [("10:00", "cancelled"), ("14:00", "confirmed")]


def test_a_time_that_has_passed_cannot_be_booked(client, rfq, mail):
    _, _, quote_id, base = offered(client, rfq)
    db = get_session()
    quote = db.get(Quote, quote_id)
    past = timeutil.local_now() - datetime.timedelta(hours=3)
    quote.visit_windows = [{"start": timeutil.iso(past), "end": timeutil.iso(past + datetime.timedelta(hours=1)), "label": "Earlier today"},
                           quote.visit_windows[1]]
    db.commit()
    db.close()
    assert "choose one of the times" in client.post(base, data={"slot": "0", "consent": "1"}).text
    assert visits(rfq) == []
    assert "Earlier today" not in client.get(base).text  # not offered any more


# ---- other times and cancellation -----------------------------------------------------------------------------
def test_none_of_these_suit_asks_the_supplier_once_and_the_supplier_sees_why(client, rfq, mail):
    token, _, quote_id, base = offered(client, rfq)
    assert client.post(f"{base}/other").status_code == 303
    client.post(f"{base}/other")  # a double tap must not email twice
    assert [m for m in mail if "other visit times" in m[1]] and len([m for m in mail if "other visit times" in m[1]]) == 1
    assert "We've asked Acme Concreting for other times" in client.get(base).text
    assert "can't make those times" in client.get(f"/q/{token}").text
    # the supplier offering fresh times clears it
    client.post(f"/q/{token}/visit", data=visit_data(date1=day(9), from1="09:00", to1="10:00"))
    assert "Choose a time for Acme Concreting" in client.get(base).text


def test_cancelling_sends_the_supplier_a_cancellation_and_asks_for_new_times(client, rfq, mail):
    token, _, _, base = offered(client, rfq)
    client.post(base, data={"slot": "0", "consent": "1"})
    mail.clear()
    assert client.post(f"{base}/cancel").status_code == 303
    to, subject, body, files = mail[0]
    assert to == "acme@example.com" and "cancelled" in subject.lower()
    assert b"METHOD:CANCEL" in files[0][2] and b"STATUS:CANCELLED" in files[0][2]
    assert [v.status for v in visits(rfq)] == ["cancelled"]
    assert "can't make those times" in client.get(f"/q/{token}").text and "Site visit confirmed" not in client.get(f"/q/{token}").text


# ---- calendar downloads ---------------------------------------------------------------------------------------
def test_both_sides_can_download_the_calendar_file_only_when_a_visit_is_confirmed(client, rfq, mail):
    token, _, _, base = offered(client, rfq)
    assert client.get(f"{base}/visit.ics").status_code == 404 and client.get(f"/q/{token}/visit.ics").status_code == 404
    client.post(base, data={"slot": "0", "consent": "1"})
    for url in (f"{base}/visit.ics", f"/q/{token}/visit.ics"):
        r = client.get(url)
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/calendar")
        assert b"BEGIN:VCALENDAR" in r.content and b"LOCATION:Fitzroy VIC 3065" in r.content
    assert MOBILE.encode() not in client.get(f"{base}/visit.ics").content  # the customer's own calendar has no phone number to leak


# ---- access control ------------------------------------------------------------------------------------------
def test_a_visit_page_only_works_for_the_right_job_and_a_real_visit_request(client, rfq, mail):
    token, job_token, quote_id, base = offered(client, rfq)
    other_job = get_session()  # another customer's progress token must not reach this quote
    foreign = Job(title="Other", description="x", category="plumber", location_text="Carlton", status="QUEUED")
    other_job.add(foreign)
    other_job.commit()
    foreign_token = foreign.public_token
    other_job.close()
    assert client.get(f"/j/{foreign_token}/visit/{quote_id}").status_code == 404
    assert client.post(f"/j/{foreign_token}/visit/{quote_id}", data={"slot": "0", "consent": "1"}).status_code == 404
    assert client.get(f"/j/{job_token}/visit/not-a-quote").status_code == 404
    client.post(f"/q/{token}/quote", data={"price_type": "fixed", "price": "100"})  # now it is a quote, not a visit request
    assert client.get(base).status_code == 404


def test_visit_actions_are_rate_limited(client, rfq):
    _, _, _, base = offered(client, rfq)
    codes = [client.post(f"{base}/other").status_code for _ in range(customer_visits.action_limiter.limit + 3)]
    assert codes[0] == 303 and codes[-1] == 429


# ---- notifications: dry run and opt-outs ---------------------------------------------------------------------------
def test_in_dry_run_the_confirmation_is_recorded_but_nothing_is_sent(client, rfq):
    _, _, _, base = offered(client, rfq)
    client.post(base, data={"slot": "0", "consent": "1"})
    recorded = notes(rfq)
    assert {n.status for n in recorded} == {"dry_run"} and len(recorded) == 2  # visible in the outbox, never delivered


def test_a_supplier_who_opted_out_is_not_emailed(client, rfq, mail):
    from db.models import Suppression
    from services.notify import suppress

    _, _, _, base = offered(client, rfq)
    db = get_session()
    suppress(db, "ACME@example.com")
    db.commit()
    db.close()
    try:
        client.post(base, data={"slot": "0", "consent": "1"})
        assert [m[0] for m in mail] == ["irene@example.com"]  # the customer still gets their confirmation
    finally:  # the database is shared between tests, so don't leave a global opt-out behind
        db = get_session()
        db.query(Suppression).filter_by(email="acme@example.com").delete()
        db.commit()
        db.close()


# ---- the calendar builder --------------------------------------------------------------------------------------
def test_ics_is_crlf_floating_time_and_escaped():
    ics = build_ics("abc", datetime.datetime(2026, 11, 3, 10), datetime.datetime(2026, 11, 3, 12), "Visit; with, commas",
                    "Line one\nLine two", "12 Brunswick St, Fitzroy")
    text = ics.decode()
    assert text.startswith("BEGIN:VCALENDAR\r\n") and text.endswith("END:VCALENDAR\r\n") and "\n" not in text.replace("\r\n", "")
    assert "DTSTART:20261103T100000" in text and "DTEND:20261103T120000" in text and "Z\r\nDTEND" not in text  # floating, no time zone
    assert "SUMMARY:Visit\\; with\\, commas" in text and "DESCRIPTION:Line one\\nLine two" in text and "LOCATION:12 Brunswick St\\, Fitzroy" in text


def test_ics_lines_are_folded_at_75_octets_without_splitting_a_character():
    long_text = "café " * 60
    ics = build_ics("abc", datetime.datetime(2026, 11, 3, 10), datetime.datetime(2026, 11, 3, 11), long_text)
    lines = ics.split(b"\r\n")
    assert all(len(line) <= 75 for line in lines)
    ics.decode("utf-8")  # still valid UTF-8, so no character was cut in half
    unfolded = ics.decode().replace("\r\n ", "")
    assert "SUMMARY:" + long_text.replace("é", "é") in unfolded


def test_time_helpers():
    start, end = datetime.datetime(2026, 11, 3, 14, 30), datetime.datetime(2026, 11, 3, 16)
    assert timeutil.label(start, end) == "Tue 3 Nov, 2:30pm to 4pm"
    assert timeutil.clock(datetime.datetime(2026, 1, 1, 0, 0)) == "12am" and timeutil.clock(datetime.datetime(2026, 1, 1, 12, 5)) == "12:05pm"
    assert timeutil.parse_local(timeutil.iso(start)) == start
