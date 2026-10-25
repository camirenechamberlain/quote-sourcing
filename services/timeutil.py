"""Local-time helpers. Site visits are arranged in the job's local time (a Tuesday 10am means 10am where the job is), so visit times
are stored as naive local ISO strings ("2026-11-03T10:00") and only converted when we need "now" or a calendar invite.
TODO: derive the time zone from the job's country once the platform is country-aware (it is one setting for now)."""
from __future__ import annotations

import datetime
from zoneinfo import ZoneInfo

import config


def tz() -> datetime.tzinfo:
    try:
        return ZoneInfo(config.TIMEZONE)
    except Exception:  # noqa: BLE001 - a mistyped zone must never break the site; fall back to UTC
        return datetime.timezone.utc


def local_now() -> datetime.datetime:
    """Now, as a naive datetime in the configured local time zone."""
    return datetime.datetime.now(tz()).replace(tzinfo=None, microsecond=0)


def utc_to_local(moment: datetime.datetime) -> datetime.datetime:
    """A naive UTC datetime (how the database stores times) as naive local time."""
    return moment.replace(tzinfo=datetime.timezone.utc).astimezone(tz()).replace(tzinfo=None)


def parse_local(value: str) -> datetime.datetime:
    return datetime.datetime.strptime(value, "%Y-%m-%dT%H:%M")


def iso(moment: datetime.datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M")


def clock(moment: datetime.datetime) -> str:
    """'10am', '2:30pm'."""
    hour = moment.hour % 12 or 12
    suffix = "am" if moment.hour < 12 else "pm"
    return f"{hour}{suffix}" if moment.minute == 0 else f"{hour}:{moment.minute:02d}{suffix}"


def label(start: datetime.datetime, end: datetime.datetime) -> str:
    """'Tue 3 Nov, 10am to 12pm'."""
    return f"{start:%a} {start.day} {start:%b}, {clock(start)} to {clock(end)}"
