from __future__ import annotations

import uuid

from db.models import Company, Job


def make_reference_code(job: Job) -> str:
    return f"{job.ref_prefix}-{uuid.uuid4().hex[:6].upper()}"


def _budget_line(job: Job) -> str:
    if job.budget_min and job.budget_max:
        return f"Indicative budget: ${job.budget_min:,.0f}-${job.budget_max:,.0f}"
    if job.budget_max:
        return f"Indicative budget: up to ${job.budget_max:,.0f}"
    return "No fixed budget set — please quote based on the scope described below."


def build_subject(job: Job, reference_code: str) -> str:
    return f"Request for Quote: {job.category} — {job.title} [Ref: {reference_code}]"


def build_body(job: Job, company: Company, reference_code: str, sender_name: str, attachment_names=()) -> str:
    project_line = f"\nProject type: {job.project_type}." if getattr(job, "project_type", None) else ""
    timeframe_line = f"\nWhen the work needs to be done: {job.timeframe}." if getattr(job, "timeframe", None) else ""
    deadline_line = f"\nWe'd appreciate a reply by {job.deadline}." if job.deadline else ""
    attached_line = ("Attached for reference: " + ", ".join(attachment_names) + "\n\n") if attachment_names else ""
    return f"""Hi {company.name} team,

I'm writing on behalf of {sender_name} to request a quote for the following job in {job.location_text}:

{job.description}

{_budget_line(job)}
{project_line}{timeframe_line}{deadline_line}

Could you please reply with:
- Your quoted price (fixed, range, or hourly rate)
- What's included in the scope
- Anything excluded or that would incur extra cost
- Estimated timeline to complete the work
- Any warranty or guarantee on the work

{attached_line}Please keep this reference code in your reply so we can match it up: {reference_code}

Thanks very much,
Quoteschmote, on behalf of {sender_name}
"""


def build_rfq(job: Job, company: Company, sender_name: str, attachment_names=()) -> tuple[str, str, str]:
    reference_code = make_reference_code(job)
    subject = build_subject(job, reference_code)
    body = build_body(job, company, reference_code, sender_name, attachment_names)
    return reference_code, subject, body
