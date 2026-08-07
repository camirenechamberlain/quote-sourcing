from __future__ import annotations

import streamlit as st

from branding import inject_branding, brand_header
from db.models import Company, RfqSend, ReplyEmail
from db.session import get_session

st.set_page_config(page_title="Quote Sourcing", page_icon="📋", layout="wide")
inject_branding()
brand_header("Dashboard", "Source local trade quotes: describe a job, find companies, request quotes, compare replies.")

session = get_session()
try:
    from db.models import Job

    jobs = session.query(Job).order_by(Job.created_at.desc()).all()

    if not jobs:
        st.info("No jobs yet. Use **New Job** in the sidebar to create your first job spec.")
    else:
        for job in jobs:
            n_companies = session.query(Company).filter_by(job_id=job.id).count()
            n_sent = (
                session.query(RfqSend).filter_by(job_id=job.id, status="sent").count()
            )
            n_replies = (
                session.query(ReplyEmail)
                .join(RfqSend, ReplyEmail.rfq_send_id == RfqSend.id)
                .filter(RfqSend.job_id == job.id)
                .count()
            )

            with st.container(border=True):
                cols = st.columns([3, 1, 1, 1, 1])
                cols[0].markdown(f"**{job.title}**  \n{job.category} · {job.location_text}")
                cols[1].metric("Candidates", n_companies)
                cols[2].metric("Sent", n_sent)
                cols[3].metric("Replies", n_replies)
                cols[4].markdown(f"`{job.status}`  \nRef: `{job.ref_prefix}`")
finally:
    session.close()

st.sidebar.markdown("Use the pages above to create a job, discover companies, send RFQs, check replies, and compare quotes.")
