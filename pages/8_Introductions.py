from __future__ import annotations

import pandas as pd
import streamlit as st

import config
from branding import inject_branding, brand_header
from db.models import Company, Introduction, Job
from db.session import get_session

st.set_page_config(page_title="Introductions", page_icon="🤝", layout="wide")
inject_branding()
brand_header("Introductions and fees")

STATUSES = ["free_beta", "due", "invoiced", "paid", "waived", "void"]
session = get_session()

if config.INTRODUCTION_FEE > 0:
    st.warning(f"Suppliers are being charged **${config.INTRODUCTION_FEE:g}** per introduction (a confirmed site visit, or being chosen).")
else:
    st.info(f"**Free beta.** Suppliers aren't being charged. 'List fee' is what each introduction would have earned at **${config.INTRODUCTION_FEE_LIST:g}**. "
            "Set `INTRODUCTION_FEE` in `.env` to start charging.")

rows = session.query(Introduction).order_by(Introduction.created_at.desc()).all()
live = [r for r in rows if r.status != "void"]
owed = [r for r in rows if r.status in ("due", "invoiced")]
cols = st.columns(4)
cols[0].metric("Introductions", len(live), help="Confirmed site visits and chosen suppliers, excluding voided ones")
cols[1].metric("Would-be revenue at list price", f"${sum(r.list_fee_cents for r in live) / 100:,.0f}")
cols[2].metric("Outstanding (due + invoiced)", f"${sum(r.fee_cents for r in owed) / 100:,.0f}")
cols[3].metric("Paid", f"${sum(r.fee_cents for r in rows if r.status == 'paid') / 100:,.0f}")

if not rows:
    st.caption("No introductions yet. One is recorded the first time a supplier receives a customer's details.")
    session.close()
    st.stop()

jobs = {j.id: j for j in session.query(Job).filter(Job.id.in_({r.job_id for r in rows})).all()}
companies = {c.id: c for c in session.query(Company).filter(Company.id.in_({r.company_id for r in rows})).all()}
df = pd.DataFrame([
    {
        "id": r.id,
        "When (UTC)": r.created_at,
        "Request": jobs[r.job_id].title if r.job_id in jobs else "",
        "Supplier": companies[r.company_id].name if r.company_id in companies else "",
        "Trigger": r.trigger,
        "Terms accepted": "yes" if r.accepted_at else "no",
        "Fee ($)": r.fee_cents / 100,
        "List fee ($)": r.list_fee_cents / 100,
        "Status": r.status,
        "Note": r.note or "",
    }
    for r in rows
])
edited = st.data_editor(
    df,
    column_config={
        "id": None,
        "Status": st.column_config.SelectboxColumn(options=STATUSES, required=True),
        "Fee ($)": st.column_config.NumberColumn(min_value=0, step=1, format="%.2f", help="What is actually charged. Change it to charge a beta supplier."),
    },
    disabled=["When (UTC)", "Request", "Supplier", "Trigger", "Terms accepted", "List fee ($)"],
    hide_index=True,
    use_container_width=True,
    key="introductions_editor",
)

if st.button("Save changes"):
    by_id = {r.id: r for r in rows}
    changed = 0
    for _, row in edited.iterrows():
        r = by_id.get(row["id"])
        if r is None:
            continue
        status, note, fee = row["Status"], (row["Note"] or "").strip() or None, int(round(float(row["Fee ($)"]) * 100))
        if (status, note, fee) != (r.status, r.note, r.fee_cents):
            r.status, r.note, r.fee_cents = status, note, max(0, fee)
            changed += 1
    session.commit()
    st.success(f"Saved {changed} change(s).")

st.caption("A voided introduction means the customer cancelled the visit (or ended the request) before it happened, so the fee is waived. "
           "Rebooking the same supplier reinstates it. Only one introduction is ever recorded per supplier per request.")
session.close()
