"""The customer manages their own request: close it, choose a supplier, add details and photos. Everything that follows from ending a request."""
import datetime
import secrets
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from db.models import Company, Job, Notification, Quote, ReplyEmail, RfqSend, User, Visit
from db.session import get_session
from server import customer_visits, job_manage, main, supplier_response
from services import followups, outbound, timeutil
from tests.test_web_chat import jpeg_bytes

H = datetime.timedelta


@pytest.fixture()
def client():
    return TestClient(main.app, follow_redirects=False)


@pytest.fixture(autouse=True)
def fresh_limits():
    for limiter in (job_manage.view_limiter, job_manage.action_limiter, job_manage.update_limiter, supplier_response.view_limiter,
                    supplier_response.submit_limiter_ip, supplier_response.submit_limiter_token, customer_visits.view_limiter,
                    customer_visits.action_limiter):
        limiter.hits.clear()


@pytest.fixture()
def mail(monkeypatch):
    """Live mode with the transport replaced. Each item is (to, subject, body, extra_files)."""
    sent = []
    monkeypatch.setattr(outbound, "mode", lambda: "live")
    monkeypatch.setattr(outbound, "deliver", lambda to, subject, body, attachment_paths=None, extra_files=None:
                        sent.append((to, subject, body, extra_files or [])) or outbound.Delivery(True, "m", "t", to, False))
    return sent


@pytest.fixture()
def world():
    """A live request with three suppliers: A has quoted, B asked to visit (booked), C has not answered."""
    db = get_session()
    try:
        tag = secrets.token_hex(4)
        user = User(whatsapp_number=f"web:jm{tag}", name="Irene", email=f"cust-{tag}@example.com", mobile=f"+6140{secrets.randbelow(10**7):07d}")
        db.add(user)
        db.flush()
        job = Job(title=f"Tile {tag}", description="Fix the cracked tile.", category="tiler", location_text="Reservoir VIC", user_id=user.id, status="ACTIVE")
        db.add(job)
        db.flush()
        out = SimpleNamespace(job=job.id, public=job.public_token, customer=user.email, mobile=user.mobile, tag=tag, s={})
        for key in "ABC":
            company = Company(job_id=job.id, place_id=f"p-{key}-{tag}", name=f"Supplier {key} {tag}", discovered_email=f"{key.lower()}-{tag}@example.com")
            db.add(company)
            db.flush()
            rfq = RfqSend(job_id=job.id, company_id=company.id, reference_code=f"R{key}", recipient_email=company.discovered_email, subject="Request",
                          body="b", status="sent", sent_at=datetime.datetime.utcnow(), response_token=secrets.token_urlsafe(16))
            db.add(rfq)
            db.flush()
            out.s[key] = SimpleNamespace(company=company.id, rfq=rfq.id, token=rfq.response_token, email=company.discovered_email, name=company.name, quote=None)
        db.commit()

        a, b = out.s["A"], out.s["B"]
        rfq_a, rfq_b = db.get(RfqSend, a.rfq), db.get(RfqSend, b.rfq)
        qa = supplier_response.save_response(db, rfq_a, job, db.get(Company, a.company), kind="quote", price_type="fixed", price=4800.0,
                                             included="Remove old tile\nNew tile", timeline="2 days", warranty="1 year")
        start = timeutil.local_now() + H(days=5)
        qb = supplier_response.save_response(db, rfq_b, job, db.get(Company, b.company), kind="visit",
                                             visit_windows=[{"start": timeutil.iso(start), "end": timeutil.iso(start + H(hours=2)), "label": "Visit"}])
        db.add(Visit(job_id=job.id, rfq_send_id=rfq_b.id, company_id=b.company, quote_id=qb.id, start_local=timeutil.iso(start),
                     end_local=timeutil.iso(start + H(hours=2)), label="Visit", status="confirmed"))
        db.commit()
        a.quote, b.quote = qa.id, qb.id
        return out
    finally:
        db.close()


def job_of(w):
    db = get_session()
    try:
        return db.get(Job, w.job)
    finally:
        db.close()


def to_supplier(mail, s):
    return [m for m in mail if m[0] == s.email]


def kinds_sent(w):
    db = get_session()
    try:
        return sorted((n.kind, n.to_email) for n in db.query(Notification).filter_by(job_id=w.job).all())
    finally:
        db.close()


# ---- the progress page ---------------------------------------------------------------------------------------------
def test_the_progress_page_offers_choose_close_and_add_details_while_the_request_is_open(client, world):
    page = client.get(f"/j/{world.public}").text
    assert f"/j/{world.public}/choose/{world.s['A'].quote}" in page and "Choose this supplier" in page
    assert f"/j/{world.public}/close" in page and f"/j/{world.public}/update" in page and 'href="/account"' in page
    assert f"/j/{world.public}/choose/{world.s['B'].quote}" not in page  # only real quotes can be chosen, not visit requests
    assert world.s["A"].email not in page and world.s["B"].email not in page  # supplier contact details never shown


# ---- closing ---------------------------------------------------------------------------------------------------------
def test_closing_asks_why_and_then_ends_the_request(client, world, mail):
    page = client.get(f"/j/{world.public}/close").text
    assert "Close this request?" in page and "I found someone else" in page and "I no longer need it" in page
    r = client.post(f"/j/{world.public}/close", data={"reason": "found_elsewhere"})
    assert r.status_code == 303 and r.headers["location"] == f"/j/{world.public}"
    job = job_of(world)
    assert job.status == "CLOSED" and job.closed_reason == "found_elsewhere" and job.closed_at is not None


def test_an_unknown_reason_is_stored_as_other(client, world, mail):
    client.post(f"/j/{world.public}/close", data={"reason": "<script>"})
    assert job_of(world).closed_reason == "other"


def test_closing_tells_the_suppliers_who_have_not_declined_and_cancels_booked_visits(client, world, mail):
    db = get_session()
    supplier_response.save_response(db, db.get(RfqSend, world.s["C"].rfq), db.get(Job, world.job), db.get(Company, world.s["C"].company),
                                    kind="decline", decline_reason="too_busy")  # C already said no
    db.close()
    client.post(f"/j/{world.public}/close", data={"reason": "not_proceeding"})
    assert len(to_supplier(mail, world.s["A"])) == 1 and "closed" in to_supplier(mail, world.s["A"])[0][1].lower() + to_supplier(mail, world.s["A"])[0][2]
    assert to_supplier(mail, world.s["C"]) == []  # no need to email someone who declined
    b = to_supplier(mail, world.s["B"])
    assert len(b) == 1 and b[0][3] and b"METHOD:CANCEL" in b[0][3][0][2]  # the booked visit is cancelled in their calendar
    db = get_session()
    assert db.query(Visit).filter_by(job_id=world.job).one().status == "cancelled"
    assert "not_proceeding" not in b[0][2]  # the customer's reason stays private


def test_a_second_close_does_nothing_and_never_emails_twice(client, world, mail):
    client.post(f"/j/{world.public}/close", data={"reason": "other"})
    first = len(mail)
    again = client.post(f"/j/{world.public}/close", data={"reason": "other"})
    assert "already been closed or awarded" in again.text and len(mail) == first


def test_after_closing_suppliers_see_it_is_closed_and_cannot_respond_but_can_opt_out(client, world, mail):
    client.post(f"/j/{world.public}/close", data={"reason": "other"})
    token = world.s["C"].token
    page = client.get(f"/q/{token}").text
    assert "closed this request" in page and f"/q/{token}/quote" not in page and "optout" in page
    for path, data in (("quote", {"price": "100"}), ("visit", {"date1": "2030-01-01"}), ("decline", {"reason": "other"})):
        assert "This request has ended" in client.post(f"/q/{token}/{path}", data=data).text
    assert get_session().query(Quote).filter_by(job_id=world.job, company_id=world.s["C"].company).count() == 0
    assert "You're off the list" in client.post(f"/q/{token}/optout").text  # opting out is always possible


def test_after_closing_the_customer_cannot_change_visits_and_follow_ups_stop(client, world, mail):
    client.post(f"/j/{world.public}/close", data={"reason": "other"})
    assert "This request has ended" in client.get(f"/j/{world.public}/visit/{world.s['B'].quote}").text
    assert "This request has ended" in client.post(f"/j/{world.public}/visit/{world.s['B'].quote}/other").text
    db = get_session()
    planned = [p for p in followups.plan(db, datetime.datetime.utcnow() + H(days=5)) if p.job_id == world.job]
    assert planned == []  # no chases, digests or reminders for a finished request
    page = client.get(f"/j/{world.public}").text
    assert "This request is closed" in page and "Close this request" not in page and "Choose this supplier" not in page


# ---- choosing a supplier ----------------------------------------------------------------------------------------------
def test_the_choose_page_shows_the_quote_and_the_consent_it_needs(client, world):
    page = client.get(f"/j/{world.public}/choose/{world.s['A'].quote}").text
    assert f"Go with {world.s['A'].name}?" in page and "$4,800" in page and "Remove old tile" in page and "2 days" in page and "1 year" in page
    assert "value='4800'" in page and "name='consent'" in page and world.mobile in page  # exactly what will be shared


def test_choosing_without_the_consent_tick_changes_nothing(client, world, mail):
    r = client.post(f"/j/{world.public}/choose/{world.s['A'].quote}", data={"price": "4800"})
    assert r.status_code == 303 and "note=" in r.headers["location"] and "tick" in r.headers["location"]
    assert job_of(world).status == "ACTIVE" and mail == []


def test_choosing_awards_the_job_records_the_price_and_tells_everyone_the_right_thing(client, world, mail):
    r = client.post(f"/j/{world.public}/choose/{world.s['A'].quote}", data={"price": "$4,500", "consent": "1"})
    assert r.status_code == 303
    job = job_of(world)
    assert job.status == "AWARDED" and job.chosen_company_id == world.s["A"].company and job.final_price == 4500 and job.closed_at

    won = to_supplier(mail, world.s["A"])
    assert len(won) == 1 and "chosen" in won[0][1].lower() and "$4,500" in won[0][2]
    assert world.mobile in won[0][2] and world.customer in won[0][2] and "Reservoir VIC" in won[0][2]  # the chosen supplier gets the contact details
    for loser in ("B", "C"):
        lost = to_supplier(mail, world.s[loser])
        assert len(lost) == 1 and "another supplier" in lost[0][2]
        assert world.mobile not in lost[0][2] and world.customer not in lost[0][2]  # nobody else does
    assert b"METHOD:CANCEL" in to_supplier(mail, world.s["B"])[0][3][0][2]  # the other supplier's visit is cancelled

    db = get_session()
    winners = [q.company_id for q in db.query(Quote).filter_by(job_id=world.job, is_winner=True).all()]
    assert winners == [world.s["A"].company]


def test_a_blank_price_defaults_to_the_quote_and_a_bad_one_is_refused(client, world, mail):
    bad = client.post(f"/j/{world.public}/choose/{world.s['A'].quote}", data={"price": "lots", "consent": "1"})
    assert bad.status_code == 303 and "note=" in bad.headers["location"] and job_of(world).status == "ACTIVE"
    client.post(f"/j/{world.public}/choose/{world.s['A'].quote}", data={"price": "", "consent": "1"})
    assert job_of(world).final_price == 4800


def test_only_a_real_quote_on_this_request_can_be_chosen(client, world):
    assert client.get(f"/j/{world.public}/choose/{world.s['B'].quote}").status_code == 404  # a visit request is not a quote
    assert client.get(f"/j/{world.public}/choose/nonsense").status_code == 404
    other = get_session().query(Job).filter(Job.id != world.job).first()
    if other is not None:
        assert client.get(f"/j/{other.public_token}/choose/{world.s['A'].quote}").status_code == 404  # another customer's job


def test_after_awarding_the_pages_say_so_for_everyone(client, world, mail):
    client.post(f"/j/{world.public}/choose/{world.s['A'].quote}", data={"price": "4500", "consent": "1"})
    progress = client.get(f"/j/{world.public}").text
    assert "Awarded to" in progress and "Chosen" in progress and "Agreed price $4,500" in progress and "Choose this supplier" not in progress
    chosen = client.get(f"/q/{world.s['A'].token}").text
    assert "You've been chosen" in chosen and world.mobile in chosen and "$4,500" in chosen
    other = client.get(f"/q/{world.s['C'].token}").text
    assert "chosen another supplier" in other and world.mobile not in other
    assert "This request has ended" in client.post(f"/j/{world.public}/choose/{world.s['A'].quote}", data={"consent": "1"}).text  # can't award twice


# ---- add details and photos ---------------------------------------------------------------------------------------------
def test_adding_a_note_updates_the_brief_and_emails_the_suppliers_who_have_not_declined(client, world, mail):
    db = get_session()
    supplier_response.save_response(db, db.get(RfqSend, world.s["C"].rfq), db.get(Job, world.job), db.get(Company, world.s["C"].company),
                                    kind="decline", decline_reason="too_busy")
    db.close()
    r = client.post(f"/j/{world.public}/update", data={"note": "The tiles are 600mm square."})
    assert r.status_code == 303 and r.headers["location"] == f"/j/{world.public}?updated=1"
    assert "Update " in job_of(world).description and "The tiles are 600mm square." in job_of(world).description
    for key in "AB":
        sent = to_supplier(mail, world.s[key])
        assert len(sent) == 1 and "600mm square" in sent[0][2] and f"/q/{world.s[key].token}" in sent[0][2]
    assert to_supplier(mail, world.s["C"]) == []  # they declined
    assert "Added to your request" in client.get(r.headers["location"]).text


def test_adding_a_photo_saves_a_cleaned_copy_and_sends_it_to_suppliers(client, world, mail):
    before = list(job_of(world).attachment_paths or [])
    r = client.post(f"/j/{world.public}/update", data={"note": ""}, files=[("files", ("crack.jpg", jpeg_bytes(exif=True), "image/jpeg"))])
    assert r.status_code == 303
    after = job_of(world).attachment_paths
    assert len(after) == len(before) + 1 and Path(after[-1]).exists()
    assert b"SecretCamMaker" not in Path(after[-1]).read_bytes()  # camera details are stripped, as for photos in the chat
    email = to_supplier(mail, world.s["A"])[0]
    assert [f[0] for f in email[3]] == ["update-photo-1.jpg"] and "update-photo-1.jpg" in email[2]


@pytest.mark.parametrize("data, files, message", [
    ({"note": ""}, [], "write a note or choose a file"),
    ({"note": ""}, [("files", ("x.jpg", b"definitely not an image", "image/jpeg"))], ""),
])
def test_empty_or_unreadable_updates_change_nothing(client, world, mail, data, files, message):
    before = job_of(world).description
    r = client.post(f"/j/{world.public}/update", data=data, files=files)
    assert r.status_code == 303 and "update?note=" in r.headers["location"]
    assert job_of(world).description == before and mail == []
    assert message in client.get(r.headers["location"]).text.replace("&#x27;", "'") or not message


def test_updates_are_limited_in_number_and_attachments(client, world, mail):
    db = get_session()
    job = db.get(Job, world.job)
    job.attachment_paths = [f"/x/p{i}.jpg" for i in range(12)]
    db.commit()
    db.close()
    r = client.post(f"/j/{world.public}/update", data={"note": "x"}, files=[("files", ("a.jpg", jpeg_bytes(), "image/jpeg"))])
    assert "update?note=" in r.headers["location"] and "attachments" in client.get(r.headers["location"]).text

    db = get_session()
    job = db.get(Job, world.job)
    job.attachment_paths = []
    job.description += "".join(f"\n\nUpdate 1 Nov 2026: note {i}" for i in range(job_manage.MAX_UPDATES_PER_JOB))
    db.commit()
    db.close()
    r = client.post(f"/j/{world.public}/update", data={"note": "one more"})
    assert "limit of updates" in client.get(r.headers["location"]).text


def test_an_ended_request_cannot_be_updated(client, world, mail):
    client.post(f"/j/{world.public}/close", data={"reason": "other"})
    mail.clear()
    assert "This request has ended" in client.get(f"/j/{world.public}/update").text
    assert "This request has ended" in client.post(f"/j/{world.public}/update", data={"note": "hello"}).text and mail == []


def test_the_notes_text_is_escaped_where_it_is_shown_and_capped(client, world, mail):
    client.post(f"/j/{world.public}/update", data={"note": "<script>alert(1)</script>" + "x" * 3000})
    desc = job_of(world).description
    assert len(desc) < 1500 + len("Fix the cracked tile.") + 100
    page = client.get(f"/q/{world.s['A'].token}").text
    assert "<script>alert(1)" not in page and "&lt;script&gt;" in page


# ---- access control -------------------------------------------------------------------------------------------------------
def test_management_pages_need_a_valid_progress_token_and_are_rate_limited(client, world):
    for path in ("close", "update", f"choose/{world.s['A'].quote}"):
        assert client.get(f"/j/not-a-real-token/{path}").status_code == 404
    codes = [client.post(f"/j/{world.public}/update", data={"note": ""}).status_code for _ in range(job_manage.action_limiter.limit + 3)]
    assert codes[-1] == 429
