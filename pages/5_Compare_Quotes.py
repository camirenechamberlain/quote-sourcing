from __future__ import annotations

import streamlit as st

from branding import inject_branding, brand_header
from db.models import Job, Quote, ReplyEmail, RfqSend
from db.session import get_session
from services.llm_extract import extract_quote

st.set_page_config(page_title="Compare Quotes", page_icon="📊", layout="wide")
inject_branding()
brand_header("Compare Quotes")

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
quotes = session.query(Quote).filter_by(job_id=job.id).order_by(Quote.extracted_at).all()

replied_rfq_ids = {q.reply_email.rfq_send_id for q in quotes}
awaiting = [rs for rs in sent_rows if rs.id not in replied_rfq_ids]

c1, c2, c3 = st.columns(3)
c1.metric("Sent", len(sent_rows))
c2.metric("Replied", len(replied_rfq_ids))
c3.metric("Quotes extracted", sum(1 for q in quotes if q.is_quote))

if awaiting:
    with st.expander(f"Awaiting reply from {len(awaiting)} compan{'y' if len(awaiting)==1 else 'ies'}"):
        for rs in awaiting:
            st.markdown(f"- {rs.company.name} (`{rs.recipient_email}`)")

st.divider()

if not quotes:
    st.info("No quotes yet. Use **Check Replies** to pull in responses.")
    session.close()
    st.stop()


def price_display(q: Quote) -> str:
    if q.price_amount is None:
        return "—"
    currency = q.currency or ""
    if q.price_type == "range" and q.price_max:
        return f"{currency} {q.price_amount:,.0f}–{q.price_max:,.0f}"
    if q.price_type == "hourly":
        return f"{currency} {q.price_amount:,.0f}/hr"
    return f"{currency} {q.price_amount:,.0f}"


sort_choice = st.radio("Sort by", ["Price (low to high)", "Response time"], horizontal=True)

def sort_key(q: Quote):
    if sort_choice == "Price (low to high)":
        return (q.price_amount is None, q.price_amount or 0)
    rfq_send = q.reply_email.rfq_send
    if rfq_send.sent_at and q.reply_email.received_at:
        return (q.reply_email.received_at - rfq_send.sent_at)
    return 999999

quotes_sorted = sorted(quotes, key=sort_key)

cols = st.columns(min(len(quotes_sorted), 3) or 1)
for i, q in enumerate(quotes_sorted):
    rfq_send = q.reply_email.rfq_send
    with cols[i % len(cols)]:
        with st.container(border=True):
            title = f"⭐ {rfq_send.company.name}" if q.is_winner else rfq_send.company.name
            st.markdown(f"### {title}")
            if rfq_send.company.rating:
                st.caption(f"Google rating: {rfq_send.company.rating}")

            if not q.is_quote:
                st.warning("Needs your review — doesn't look like a firm quote")
            elif q.confidence == "low":
                st.warning("Low-confidence extraction — check the original email")

            st.markdown(f"## {price_display(q)}")
            st.caption(f"Type: {q.price_type}")

            if q.summary:
                st.write(q.summary)

            if q.scope_included:
                st.markdown("**Included:**")
                for item in q.scope_included:
                    st.markdown(f"- {item}")

            if q.exclusions:
                st.markdown("**:orange[Excluded / extra cost:]**")
                for item in q.exclusions:
                    st.markdown(f":orange[- {item}]")

            if q.timeline_text:
                st.markdown(f"**Timeline:** {q.timeline_text}")
            if q.warranty_notes:
                st.markdown(f"**Warranty:** {q.warranty_notes}")

            if rfq_send.sent_at and q.reply_email.received_at:
                delta = q.reply_email.received_at - rfq_send.sent_at
                st.caption(f"Response time: {delta.days}d {delta.seconds // 3600}h")

            with st.expander("Original email"):
                st.text(q.reply_email.raw_body_text)

            btn_cols = st.columns(2)
            if btn_cols[0].button("Mark as winner", key=f"winner_{q.id}"):
                for other in quotes:
                    other.is_winner = other.id == q.id
                session.commit()
                st.rerun()
            if btn_cols[1].button("Re-extract", key=f"reextract_{q.id}"):
                job_budget = (
                    f"${job.budget_min:,.0f}-${job.budget_max:,.0f}"
                    if job.budget_min and job.budget_max
                    else "not specified"
                )
                try:
                    result = extract_quote(job.description, job_budget, q.reply_email.raw_body_text)
                    q.is_quote = result.parsed.is_quote
                    q.price_amount = result.parsed.price_amount
                    q.price_max = result.parsed.price_max
                    q.currency = result.parsed.currency
                    q.price_type = result.parsed.price_type
                    q.scope_included = result.parsed.scope_included
                    q.exclusions = result.parsed.exclusions
                    q.timeline_text = result.parsed.timeline_text
                    q.warranty_notes = result.parsed.warranty_notes
                    q.confidence = result.parsed.confidence
                    q.summary = result.parsed.summary
                    q.llm_raw_response = result.raw_response
                    session.commit()
                    st.rerun()
                except Exception as e:  # noqa: BLE001
                    st.error(f"Re-extraction failed: {e}")

session.close()
