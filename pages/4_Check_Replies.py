from __future__ import annotations

import streamlit as st
from sqlalchemy.exc import IntegrityError

from branding import inject_branding, brand_header
from db.models import Job, Quote, ReplyEmail, RfqSend
from db.session import get_session
from services import gmail_service
from services.gmail_service import GmailNotConnected
from services.llm_extract import extract_quote

st.set_page_config(page_title="Check Replies", page_icon="📬", layout="wide")
inject_branding()
brand_header("Check Replies")

session = get_session()
jobs = session.query(Job).order_by(Job.created_at.desc()).all()

if not jobs:
    st.info("No jobs yet.")
    session.close()
    st.stop()

job_labels = {f"{j.title} ({j.category}, {j.location_text})": j.id for j in jobs}
selected_label = st.selectbox("Job", list(job_labels.keys()))
job_id = job_labels[selected_label]
job = session.get(Job, job_id)

sent_rows = session.query(RfqSend).filter_by(job_id=job.id, status="sent").all()

if not sent_rows:
    st.info("No RFQs sent yet for this job.")
    session.close()
    st.stop()


def ingest_message(rfq_send: RfqSend, message: dict) -> bool:
    """Returns True if a new ReplyEmail row was inserted."""
    if message["id"] == rfq_send.gmail_message_id:
        return False  # skip the outgoing message itself
    reply_data = gmail_service.message_to_reply_dict(message)
    reply = ReplyEmail(rfq_send_id=rfq_send.id, **reply_data)
    session.add(reply)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        return False  # already ingested (duplicate gmail_message_id)

    try:
        job_budget = (
            f"${job.budget_min:,.0f}-${job.budget_max:,.0f}"
            if job.budget_min and job.budget_max
            else "not specified"
        )
        result = extract_quote(job.description, job_budget, reply.raw_body_text)
        session.add(
            Quote(
                reply_email_id=reply.id,
                job_id=rfq_send.job_id,
                company_id=rfq_send.company_id,
                is_quote=result.parsed.is_quote,
                price_amount=result.parsed.price_amount,
                price_max=result.parsed.price_max,
                currency=result.parsed.currency,
                price_type=result.parsed.price_type,
                scope_included=result.parsed.scope_included,
                exclusions=result.parsed.exclusions,
                timeline_text=result.parsed.timeline_text,
                warranty_notes=result.parsed.warranty_notes,
                confidence=result.parsed.confidence,
                summary=result.parsed.summary,
                llm_raw_response=result.raw_response,
            )
        )
        session.commit()
    except Exception as e:  # noqa: BLE001 - extraction failure shouldn't block ingestion
        session.add(
            Quote(
                reply_email_id=reply.id,
                job_id=rfq_send.job_id,
                company_id=rfq_send.company_id,
                is_quote=False,
                confidence="low",
                summary=f"Extraction failed: {e}"[:200],
            )
        )
        session.commit()
    return True


if st.button("Check for replies", type="primary"):
    new_count = 0
    errors = []
    with st.spinner(f"Checking {len(sent_rows)} thread(s)..."):
        for rfq_send in sent_rows:
            try:
                if rfq_send.gmail_thread_id:
                    for message in gmail_service.get_thread_messages(rfq_send.gmail_thread_id):
                        if ingest_message(rfq_send, message):
                            new_count += 1
                # Fallback: catch replies that broke threading, matched by reference code
                for message in gmail_service.search_messages(f'"{rfq_send.reference_code}"'):
                    if ingest_message(rfq_send, message):
                        new_count += 1
            except GmailNotConnected as e:
                errors.append(str(e))
                break
            except Exception as e:  # noqa: BLE001
                errors.append(f"{rfq_send.recipient_email}: {e}")

    for e in errors:
        st.error(e)
    st.success(f"Ingested {new_count} new reply email(s).")

st.divider()
st.subheader("Status")

for rfq_send in sent_rows:
    replies = session.query(ReplyEmail).filter_by(rfq_send_id=rfq_send.id).all()
    with st.container(border=True):
        cols = st.columns([2, 1, 2])
        cols[0].markdown(f"**{rfq_send.company.name}** — `{rfq_send.recipient_email}`")
        cols[1].markdown(f"Sent: {rfq_send.sent_at.strftime('%Y-%m-%d %H:%M') if rfq_send.sent_at else '-'}")
        if replies:
            cols[2].success(f"{len(replies)} repl{'y' if len(replies) == 1 else 'ies'} received")
        else:
            cols[2].caption("No reply yet")

st.caption("Once replies come in, go to **Compare Quotes** to see them parsed side by side.")

session.close()
