"""A minimal iCalendar (.ics) event, so a confirmed site visit can be added to any calendar with one tap.

Times are written as "floating" local times (no time zone): the visit happens at that wall-clock time wherever the calendar is,
which is what a tradesperson and a customer in the same place both want.
"""
from __future__ import annotations

import datetime


def _escape(text: str) -> str:
    return (text or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\r\n", "\n").replace("\n", "\\n")


def _fold(line: str) -> list[str]:
    """RFC 5545: lines are at most 75 octets; continuation lines start with a space."""
    raw, out = line.encode("utf-8"), []
    while len(raw) > 75:
        cut = 75
        while cut > 0 and (raw[cut] & 0xC0) == 0x80:  # never split inside a multi-byte character
            cut -= 1
        out.append(raw[:cut].decode("utf-8"))
        raw = b" " + raw[cut:]
    out.append(raw.decode("utf-8"))
    return out


def build_ics(uid: str, start: datetime.datetime, end: datetime.datetime, summary: str, description: str = "", location: str = "",
              sequence: int = 0, cancelled: bool = False) -> bytes:
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    rows = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Quoteschmote//Site visits//EN", "CALSCALE:GREGORIAN",
        f"METHOD:{'CANCEL' if cancelled else 'PUBLISH'}",
        "BEGIN:VEVENT", f"UID:{_escape(uid)}@quoteschmote", f"DTSTAMP:{stamp}", f"SEQUENCE:{sequence}",
        f"DTSTART:{start:%Y%m%dT%H%M%S}", f"DTEND:{end:%Y%m%dT%H%M%S}", f"SUMMARY:{_escape(summary)}",
    ]
    if description:
        rows.append(f"DESCRIPTION:{_escape(description)}")
    if location:
        rows.append(f"LOCATION:{_escape(location)}")
    rows += [f"STATUS:{'CANCELLED' if cancelled else 'CONFIRMED'}", "END:VEVENT", "END:VCALENDAR"]
    folded = [piece for row in rows for piece in _fold(row)]
    return ("\r\n".join(folded) + "\r\n").encode("utf-8")
