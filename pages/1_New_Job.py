from __future__ import annotations

import streamlit as st
from pydantic import BaseModel, ValidationError, field_validator

from branding import inject_branding, brand_header
from db.models import Job
from db.session import get_session
from services import attachments as attachments_service

st.set_page_config(page_title="New Job", page_icon="🆕", layout="wide")
inject_branding()
brand_header("New Job Spec")


class JobSpecInput(BaseModel):
    title: str
    description: str
    category: str
    location_text: str
    radius_km: float
    budget_min: float | None = None
    budget_max: float | None = None
    deadline: str | None = None

    @field_validator("title", "description", "category", "location_text")
    @classmethod
    def not_blank(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("This field cannot be blank")
        return v.strip()

    @field_validator("radius_km")
    @classmethod
    def positive_radius(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("Radius must be greater than 0")
        return v


with st.form("new_job_form"):
    title = st.text_input("Job title", placeholder="e.g. Rewire kitchen and add 3 power points")
    category = st.text_input("Trade / category", placeholder="e.g. electrician")
    description = st.text_area(
        "Job description",
        placeholder="Describe the work in as much detail as possible: scope, access, materials, constraints...",
        height=160,
    )
    location_text = st.text_input("Location (address, suburb, or postcode)")
    radius_km = st.slider("Search radius (km)", min_value=1, max_value=100, value=15)

    col1, col2 = st.columns(2)
    budget_min = col1.number_input("Budget min (optional)", min_value=0.0, value=0.0, step=50.0)
    budget_max = col2.number_input("Budget max (optional)", min_value=0.0, value=0.0, step=50.0)

    deadline = st.text_input("Deadline for quotes (optional)", placeholder="e.g. 2026-08-21")

    photos = st.file_uploader(
        "Photos of the job (optional) — attached to every RFQ email you send for this job",
        type=["png", "jpg", "jpeg", "gif", "webp", "pdf"],
        accept_multiple_files=True,
    )

    submitted = st.form_submit_button("Create job", type="primary")

if submitted:
    try:
        spec = JobSpecInput(
            title=title,
            description=description,
            category=category,
            location_text=location_text,
            radius_km=radius_km,
            budget_min=budget_min or None,
            budget_max=budget_max or None,
            deadline=deadline or None,
        )
    except ValidationError as e:
        for err in e.errors():
            st.error(f"{err['loc'][0]}: {err['msg']}")
    else:
        session = get_session()
        try:
            job = Job(
                title=spec.title,
                description=spec.description,
                category=spec.category,
                location_text=spec.location_text,
                radius_m=int(spec.radius_km * 1000),
                budget_min=spec.budget_min,
                budget_max=spec.budget_max,
                deadline=spec.deadline,
                status="DRAFT",
            )
            session.add(job)
            session.commit()

            if photos:
                saved_paths = attachments_service.save_uploaded_files(job.id, photos)
                job.attachment_paths = saved_paths
                session.commit()

            photo_note = f" with {len(photos)} photo(s) attached" if photos else ""
            st.success(f"Job '{job.title}' created (ref {job.ref_prefix}){photo_note}. Go to **Discover Companies** next.")
        finally:
            session.close()
