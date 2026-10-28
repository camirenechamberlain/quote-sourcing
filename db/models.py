from __future__ import annotations

import datetime
import secrets
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
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    # Sourcing preferences gathered during the supplier review. Proximity is stored in radius_m.
    timeframe: Mapped[str | None] = mapped_column(String, nullable=True)  # when the WORK needs doing ("ASAP", "within 2 weeks")
    min_rating: Mapped[float | None] = mapped_column(Float, nullable=True)  # e.g. 4.5 when only top-rated suppliers wanted
    project_type: Mapped[str | None] = mapped_column(String, nullable=True)  # residential | commercial (shapes the supplier search)
    # Unguessable token for the customer-facing progress page (/j/<token>). Never the job id.
    public_token: Mapped[str | None] = mapped_column(String, unique=True, default=lambda: secrets.token_urlsafe(12))
    # How the request ended (set by the customer from their progress page). status: DRAFT | QUEUED | ACTIVE | AWARDED | CLOSED
    chosen_company_id: Mapped[str | None] = mapped_column(String, nullable=True)
    final_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    closed_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    closed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    # When the customer approved the supplier shortlist (nothing is sent before this for customer-owned jobs)
    shortlist_approved_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)

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
    customer_removed: Mapped[bool] = mapped_column(Boolean, default=False)  # the customer dropped this supplier from their shortlist

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
    test_mode: Mapped[bool] = mapped_column(Boolean, default=False)  # sent to the TEST_RECIPIENT, not the supplier
    # Unguessable key for the supplier's no-login response page (/q/<token>); the link in the RFQ email carries it.
    response_token: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
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
    source: Mapped[str] = mapped_column(String, default="email")  # email | web (the supplier used the response page)

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
    kind: Mapped[str] = mapped_column(String, default="quote")  # quote | visit | decline | question
    decline_reason: Mapped[str | None] = mapped_column(String, nullable=True)  # out_of_area | too_busy | wrong_trade | other
    # Times the supplier offered for a site visit: [{start, end, label}] (naive local ISO). Older rows may hold plain strings.
    visit_windows: Mapped[list] = mapped_column(JSON, default=list)
    windows_declined: Mapped[bool] = mapped_column(Boolean, default=False)  # the customer can't do any of the offered times
    notified_customer_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)  # when we last told the customer
    extracted_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_now)

    reply_email: Mapped["ReplyEmail"] = relationship(back_populates="quote")


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(String, nullable=False)


class User(Base):
    """A website visitor: one row per chat session, no login. `whatsapp_number` is a legacy column name (kept so existing
    databases need no migration); it now holds a unique "web:<hex>" key."""

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    whatsapp_number: Mapped[str] = mapped_column(String, unique=True, nullable=False)  # legacy name; holds "web:<hex>"
    name: Mapped[str | None] = mapped_column(String, nullable=True)
    email: Mapped[str | None] = mapped_column(String, nullable=True)
    sheet_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # Website chats: one User row per chat session (so conversations can't leak between sessions), carrying the
    # mobile number the customer typed. `mobile_verified` flips to True once SMS verification exists.
    mobile: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    mobile_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_now)


class WaMessage(Base):
    """Every chat message in or out. The table and `twilio_sid` column keep their original names (unused now) to avoid a migration."""

    __tablename__ = "wa_messages"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    direction: Mapped[str] = mapped_column(String, nullable=False)  # in | out
    body: Mapped[str] = mapped_column(Text, default="")
    media_paths: Mapped[list] = mapped_column(JSON, default=list)
    twilio_sid: Mapped[str | None] = mapped_column(String, unique=True, nullable=True)
    delivery: Mapped[str | None] = mapped_column(String, nullable=True)  # sent | dry_run | failed (out only)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_now)


class ChatSession(Base):
    """A browser chat on the website. The bearer token is only stored hashed."""

    __tablename__ = "chat_sessions"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    token_hash: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    ip: Mapped[str | None] = mapped_column(String, nullable=True)
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_now)


class IntakeSession(Base):
    """One in-progress job request being collected in the chat."""

    __tablename__ = "intake_sessions"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    state: Mapped[str] = mapped_column(String, default="collecting")  # collecting|awaiting_confirm|confirmed|cancelled
    draft: Mapped[dict] = mapped_column(JSON, default=dict)
    photo_paths: Mapped[list] = mapped_column(JSON, default=list)
    questions_asked: Mapped[int] = mapped_column(Integer, default=0)
    job_id: Mapped[str | None] = mapped_column(ForeignKey("jobs.id"), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class Visit(Base):
    """A site visit the customer has confirmed with one supplier. Times are naive local ISO strings (see services/timeutil)."""

    __tablename__ = "visits"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), nullable=False, index=True)
    rfq_send_id: Mapped[str] = mapped_column(ForeignKey("rfq_sends.id"), nullable=False, index=True)
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id"), nullable=False)
    quote_id: Mapped[str | None] = mapped_column(ForeignKey("quotes.id"), nullable=True)
    start_local: Mapped[str] = mapped_column(String, nullable=False)  # "2026-11-03T10:00"
    end_local: Mapped[str] = mapped_column(String, nullable=False)
    label: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, default="confirmed")  # confirmed | cancelled
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_now)


class Notification(Base):
    """Every email we send about a job (follow-ups, visit confirmations, updates), for dedupe and an audit trail.
    `status` is sent | test (redirected to TEST_RECIPIENT) | dry_run (recorded only) | failed."""

    __tablename__ = "notifications"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    job_id: Mapped[str | None] = mapped_column(ForeignKey("jobs.id"), nullable=True, index=True)
    rfq_send_id: Mapped[str | None] = mapped_column(ForeignKey("rfq_sends.id"), nullable=True, index=True)
    audience: Mapped[str] = mapped_column(String, default="supplier")  # supplier | customer
    kind: Mapped[str] = mapped_column(String, nullable=False, index=True)  # e.g. chase_1, visit_reminder, customer_digest
    to_email: Mapped[str] = mapped_column(String, nullable=False)
    subject: Mapped[str] = mapped_column(String, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String, default="sent")
    error_message: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_now)


class Suppression(Base):
    """Supplier addresses that have opted out of further requests. Honoured before any supplier email is sent."""

    __tablename__ = "suppressions"

    email: Mapped[str] = mapped_column(String, primary_key=True)  # lower-case
    reason: Mapped[str] = mapped_column(String, default="opted_out")
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_now)


class AccountLoginToken(Base):
    """A one-time sign-in link emailed to a customer. Only the hash is stored; it expires quickly and works once."""

    __tablename__ = "account_login_tokens"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    token_hash: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    email: Mapped[str] = mapped_column(String, nullable=False, index=True)  # lower-case
    expires_at: Mapped[datetime.datetime] = mapped_column(DateTime, nullable=False)
    used_at: Mapped[datetime.datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_now)


class AccountSession(Base):
    """A signed-in customer (a browser cookie). Only the hash of the cookie value is stored."""

    __tablename__ = "account_sessions"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    token_hash: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    email: Mapped[str] = mapped_column(String, nullable=False, index=True)
    expires_at: Mapped[datetime.datetime] = mapped_column(DateTime, nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, default=_now)
