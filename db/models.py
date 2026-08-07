from __future__ import annotations

import datetime
import uuid

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _uuid() -> str:
    return uuid.uuid4().hex


def _now() -> datetime.datetime:
    return datetime.datetime.utcnow()


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    title: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String, nullable=False)
    budget_min: Mapped[float | None] = mapped_column(Float, nullable=True)
    budget_max: Mapped[float | None] = mapped_column(Float, nullable=True)
    location_text: Mapped[str] = mapped_column(String, nullable=False)
    lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    lng: Mapped[float | None] = mapped_column(Float, nullable=True)
    radius_m: Mapped[int] = mapped_column(Integer, default=15000)
    deadline: Mapped[str | None] = mapped_column(String, nullable=True)
    ref_prefix: Mapped[str] = mapped_column(String, unique=True, default=lambda: uuid.uuid4().hex[:8].upper())
    status: Mapped[str] = mapped_column(String, default="DRAFT")
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_now)
    attachment_paths: Mapped[list] = mapped_column(JSON, default=list)

    companies: Mapped[list["Company"]] = relationship(back_populates="job", cascade="all, delete-orphan")


class Company(Base):
    __tablename__ = "companies"
    __table_args__ = (UniqueConstraint("job_id", "place_id", name="uq_company_job_place"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), nullable=False)
    place_id: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    address: Mapped[str | None] = mapped_column(String, nullable=True)
    phone: Mapped[str | None] = mapped_column(String, nullable=True)
    website: Mapped[str | None] = mapped_column(String, nullable=True)
    rating: Mapped[float | None] = mapped_column(Float, nullable=True)
    discovered_email: Mapped[str | None] = mapped_column(String, nullable=True)
    email_source: Mapped[str] = mapped_column(String, default="none")  # scraped_mailto|scraped_regex|manual|none
    scrape_status: Mapped[str] = mapped_column(String, default="pending")
    scrape_notes: Mapped[str | None] = mapped_column(String, nullable=True)
    included: Mapped[bool] = mapped_column(Boolean, default=True)

    job: Mapped["Job"] = relationship(back_populates="companies")
    rfq_sends: Mapped[list["RfqSend"]] = relationship(back_populates="company", cascade="all, delete-orphan")


class RfqSend(Base):
    __tablename__ = "rfq_sends"
    __table_args__ = (UniqueConstraint("job_id", "company_id", name="uq_rfq_job_company"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), nullable=False)
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id"), nullable=False)
    reference_code: Mapped[str] = mapped_column(String, nullable=False)
    recipient_email: Mapped[str] = mapped_column(String, nullable=False)
    subject: Mapped[str] = mapped_column(String, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    gmail_message_id: Mapped[str | None] = mapped_column(String, nullable=True)
    gmail_thread_id: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="draft")  # draft|sent|failed
    sent_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    error_message: Mapped[str | None] = mapped_column(String, nullable=True)

    company: Mapped["Company"] = relationship(back_populates="rfq_sends")
    replies: Mapped[list["ReplyEmail"]] = relationship(back_populates="rfq_send", cascade="all, delete-orphan")


class ReplyEmail(Base):
    __tablename__ = "reply_emails"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    rfq_send_id: Mapped[str] = mapped_column(ForeignKey("rfq_sends.id"), nullable=False)
    gmail_message_id: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    gmail_thread_id: Mapped[str | None] = mapped_column(String, nullable=True)
    from_email: Mapped[str | None] = mapped_column(String, nullable=True)
    received_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    subject: Mapped[str | None] = mapped_column(String, nullable=True)
    raw_body_text: Mapped[str] = mapped_column(Text, nullable=False)
    ingested_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_now)

    rfq_send: Mapped["RfqSend"] = relationship(back_populates="replies")
    quote: Mapped["Quote"] = relationship(back_populates="reply_email", uselist=False, cascade="all, delete-orphan")


class Quote(Base):
    __tablename__ = "quotes"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    reply_email_id: Mapped[str] = mapped_column(ForeignKey("reply_emails.id"), unique=True, nullable=False)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), nullable=False)
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id"), nullable=False)
    is_quote: Mapped[bool] = mapped_column(Boolean, default=True)
    price_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_max: Mapped[float | None] = mapped_column(Float, nullable=True)
    currency: Mapped[str | None] = mapped_column(String, nullable=True)
    price_type: Mapped[str] = mapped_column(String, default="unknown")  # fixed|range|hourly|unknown
    scope_included: Mapped[list] = mapped_column(JSON, default=list)
    exclusions: Mapped[list] = mapped_column(JSON, default=list)
    timeline_text: Mapped[str | None] = mapped_column(String, nullable=True)
    warranty_notes: Mapped[str | None] = mapped_column(String, nullable=True)
    confidence: Mapped[str] = mapped_column(String, default="low")  # high|medium|low
    summary: Mapped[str | None] = mapped_column(String, nullable=True)
    llm_raw_response: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_winner: Mapped[bool] = mapped_column(Boolean, default=False)
    extracted_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_now)

    reply_email: Mapped["ReplyEmail"] = relationship(back_populates="quote")


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(String, nullable=False)
