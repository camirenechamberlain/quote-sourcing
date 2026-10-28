# Quote Sourcing

Local tool to source and compare trade quotes: describe a job, find nearby companies via Google Places, scrape their contact emails, send RFQ emails from your own Gmail (with review/approval), then pull in and compare replies.

## Setup

1. **Python deps** (already installed in `.venv` if you used the same session):
   ```
   python -m venv .venv
   .venv\Scripts\pip install -r requirements.txt
   ```

2. **Copy `.env.example` to `.env`** and fill in:
   - `GOOGLE_PLACES_API_KEY` — from Google Cloud Console, with **Places API (New)** and **Geocoding API** enabled, key restricted to those two.
   - `GOOGLE_OAUTH_CLIENT_SECRET_FILE` — path to a downloaded OAuth client JSON (see below). Defaults to `data/client_secret.json`.
   - `ANTHROPIC_API_KEY` — for parsing reply emails into structured quotes.

3. **Google Cloud OAuth setup** (one-time, for Gmail send/read):
   - Create/select a project at console.cloud.google.com; enable **Gmail API**, **Places API (New)**, **Geocoding API**.
   - OAuth consent screen: External, **Testing** status, add your own Gmail as a test user.
   - Create an OAuth Client ID of type **Desktop app**, download the JSON, save it to `data/client_secret.json` (or wherever `.env` points).
   - In the app, go to **Settings** and click **Connect Gmail** — this opens your browser once for consent.
   - **Recommended: use a dedicated Gmail account for this tool rather than your main one.** The `gmail.readonly` scope technically grants read access to the whole mailbox it's connected to (a Gmail API limitation, not something this app can narrow), so a dedicated account keeps that access contained to RFQ correspondence only. Forward or check that inbox as needed — it's the OAuth grant on your main account we're avoiding, not visibility into replies.

4. **Run it**:
   ```
   .venv\Scripts\streamlit run app.py
   ```

## Workflow

1. **New Job** — describe the work, budget, location, radius, category.
2. **Discover Companies** — finds nearby companies via Places, then scrapes their websites for a contact email (or add one manually if scraping doesn't find one).
3. **Review and Send** — attach reference photos (or PDFs) for the job, edit the auto-drafted RFQ per recipient, then approve and send. Attachments are sent with every RFQ for that job. Nothing sends without this step.
4. **Check Replies** — polls your Gmail for replies matching the job's reference code; runs LLM extraction automatically on new replies.
5. **Compare Quotes** — side-by-side price/scope/exclusions/timeline comparison, mark a winner.

## Notes

- All data is local, in `data/app.db` (SQLite). `data/`, `.env`, and `.venv/` are gitignored.
- Sending unsolicited email to scraped business addresses may be subject to anti-spam laws depending on jurisdiction (CAN-SPAM, Spam Act, CASL, etc). This is a one-off B2B RFQ, not bulk marketing, and the template identifies you by name — but use your own judgment, especially at volume or across jurisdictions you're unfamiliar with.
- The double-send guard (one RFQ per company per job) and reply-dedupe are enforced at the database level, verified during development.
- Attachments are capped at a soft 15MB total per job (Gmail's raw-message limit is ~25MB, attachments inflate ~37% once encoded) — the Review and Send page warns if you're over that.
- Branding (colors, font, header/sidebar styling) is pulled from propertypie.com.au — see [branding.py](branding.py) if the palette changes.

## Quoteschmote website chat

A mobile-first chat page (`server/static/`) in front of the intake agent. The landing page is just the chat: the customer
describes the job and attaches photos, the agent refines it, shows a summary, and only then asks for first name, mobile,
email and consent. A confirmed request becomes a `QUEUED` job (visible in the Streamlit pages) and the customer gets a
private progress link (`/j/<token>`).

Run it:

```
.venv\Scripts\python.exe -m uvicorn server.main:app --port 8000
```

Then open http://localhost:8000. Tests: `.venv\Scripts\python.exe -m pytest tests -q`.

Limits and safety (all in `.env`, see `.env.example`):
- `DAILY_QUOTE_LIMIT` quote requests per mobile per rolling 24h (default 10), enforced at the contact-details step,
  i.e. the moment a request would become a job.
- `GLOBAL_DAILY_MESSAGES` caps total website chat messages per 24h (default 1500). Chats start before we know who the
  customer is, so this bounds AI spend from anonymous use.
- `GLOBAL_DAILY_JOBS` across all customers, `IP_DAILY_SESSIONS` per IP, `MAX_MESSAGES_PER_SESSION` per chat.
- Mobile numbers are **not SMS-verified yet** (`User.mobile_verified` is always false), so the per-number limit can be
  dodged with other numbers; the IP and global caps are the real backstop until verification is added.
- Photos are re-encoded server-side (location/EXIF data is stripped) and shrunk in the browser before upload.
- Strict CSP, `Referrer-Policy: no-referrer`, hashed chat tokens, no public API docs.

Putting it on quoteschmote.com from your own PC: use a Cloudflare Tunnel pointing at `localhost:8000`, then set
`PUBLIC_BASE_URL=https://quoteschmote.com` and `TRUST_PROXY_HEADERS=1` in `.env` (only enable the latter behind the tunnel).

### Supplier review and sourcing preferences

When a customer taps "Yes, request quotes", a second agent (`server/agent/supplier_review.py`) acts as a **senior professional in
the relevant field** (a land surveyor for a subdivision, an electrician for a rewire, and so on) and asks the pertinent questions
that trade always asks before it can quote completely: scope, size, existing condition, access and hazards, compliance and
permits, who supplies materials, occupancy, urgency. Up to **five** questions, most important first; fewer when the request is
already complete, and a single tile gets two. Each question carries an importance (essential / important / minor) and minor ones
are dropped unless `REVIEW_THOROUGH=1`.

The same step also works out, asking only for what the customer hasn't already said:

- **Residential or commercial**, where it matters for the trade. It leads the Places search query ("commercial electrician")
  and appears in the supplier email.
- **Proximity**: skipped for work that can be done remotely. "within N km" sets the radius (default 15 km, "anywhere" = 50 km,
  the Places cap).
- **Reviews**: "4.5 stars or higher" sets `Job.min_rating`; Discover Companies excludes lower-rated and unrated suppliers before
  scraping them (`services/filters.py`).
- **Timeframe**: when the work needs doing; it appears in the supplier email.
- **Suggested attachments**: which photos or documents would help suppliers quote (for subdivisions it always leads with
  engineering/design drawings, then the title and any survey or permit). Anything already attached is not suggested again.

**Photos first.** The first intake step only needs the trade, the job and the location (no size or scope questions). When the
job is described, the review starts straight away (no confirm tap first): photos sent with the job are read immediately, and
otherwise the customer is asked to attach photos *before* any question. The reviewer studies up to
`MAX_REVIEW_PHOTOS` (4) downscaled photos like a tradesperson on a site visit: whatever it can plainly see (existing material,
condition, rough size where a car or door gives scale) is recorded in the brief as "seen in photos, to be confirmed on site" and
shown to the customer, and it asks only about what the photos can't show. If the photos answer everything it asks nothing.
"Continue without photos" falls back to the normal questions. Cost: roughly 1,000 tokens per photo on the review call (about 1
cent for four). `PHOTOS_FIRST=0` restores the older order (summary and confirm first, questions after). Checked live on a synthetic driveway scene: 5 questions without a photo, 0 with.

Questions are asked **one at a time** ("2 of 4: ..."). On the website each is a row of tappable options (multi-select where
several can apply, with a Done button); the customer can always type instead, tap "Not sure", or "Send as is" to skip the rest.
The review runs once per request, never loops, and fails open (a
reviewer error never blocks a customer). Settings: `REVIEW_MODEL`, `REVIEW_THOROUGH`.

#### When the customer doesn't know

For a physical size or quantity the customer may not know (driveway width, house footprint, roof area), the question gets up
to two shortcuts:

- **Typical**: a sensible typical value ("about 3 m wide (single car)"). It comes free with the review, with no extra AI call,
  and is recorded as "typical for this kind of property, not measured". Only offered for a number with a unit.
- **Estimate from my photo**: one cheap vision call (`ESTIMATE_MODEL`, Haiku, at most 3 photos at 1024 px, about 0.03 cents).
  The estimate must use something of known size in the photo (a car, a door) and is given as a range; if there is nothing to
  judge scale from it says so instead of guessing. The customer confirms before it is used, and the supplier brief says
  "estimated from photo, to be confirmed on site". At most `MAX_ESTIMATES_PER_REQUEST` (3) per request.

Estimates were checked on synthetic scenes only; expect real angled photos to be rougher, which is why the customer confirms.

### Supplier response page

Each RFQ email carries a one-tap link (`/q/<token>`, set `PUBLIC_BASE_URL` so it can be built) to a no-login page where the supplier
sends a quote (fixed, range or hourly, with inclusions, exclusions, timeline and warranty), asks to see the job first (offering up to three
visit times), or declines with a reason (for example outside their area). Replying by email still works. A response is stored like an
emailed reply (a `ReplyEmail` plus a structured `Quote`), so Compare Quotes and the customer's progress page show it with no extra work, and
submitting again from the same link revises it. The page is plain server-rendered HTML (no JavaScript), indexed by no one, rate limited,
and escapes everything it shows. The customer's progress page lists each supplier's status (quote with price, wants a site visit, declined)
and never shows supplier contact details.

### Site visits

Many trades can't price without seeing the job, so suppliers can offer up to three real date-and-time windows instead of a price. The customer
chooses one from their progress page (`/j/<token>/visit/<id>`) after a consent step that names exactly what is shared (first name, mobile and
the job address, with that one supplier). "None of these suit" asks the supplier for new times and cancelling is one tap. Confirming emails both
sides with a calendar invite (`.ics`, floating local time); the supplier page shows the customer's details only after confirmation. Times use the
`TIMEZONE` setting (default `Australia/Melbourne`).

### Automated follow-ups

`services/followups.py` keeps requests moving without anyone chasing by hand: a supplier who hasn't responded is nudged at 3 and 7 days (never
more, never after 14 days); the customer gets one digest (at most every 4 hours) of new quotes, visit offers and declines, and reminders to pick
a visit time at 24 and 72 hours; both sides get a reminder within 24 hours of a visit; a supplier who visited but hasn't quoted is nudged at 2 and
6 days after; and a customer with quotes waiting gets one nudge after 3 days. It only sends 08:00 to 18:00 local, Monday to Saturday, at most 40
emails per run and 200 per day, and every email is recorded in the `Notification` table, which also stops anything sending twice.

- Preview what is due: `.venv\Scripts\python.exe -m services.followups` (add `--send` to really send). The **Outbox** page in the admin app shows
  the same, the recent emails and who has opted out.
- Run it automatically: set `FOLLOWUPS_ENABLED=1` in `.env` and restart the site (`FOLLOWUP_INTERVAL_MINUTES`, default 15). It is off by default.
- It obeys `OUTBOUND_MODE` (dry_run only logs; test redirects to `TEST_RECIPIENT`; live sends) and never emails an address that has opted out
  (the "Don't contact me" link on every supplier page and email). Customer emails that depend on a link are held back until `PUBLIC_BASE_URL`
  is set.

### Customer accounts and managing a request

Customers sign in at `/account` with an email link (no passwords): the link is single-use, lasts 15 minutes, and opening it only shows a
Sign in button (so email scanners can't use it up). The page is always answered the same way, and the email is only sent to an address that
has requests. "My requests" lists every request made with that email with a plain-English status.

From a request's progress page (`/j/<token>`) the customer can:

- **Approve the supplier shortlist.** Suppliers we found (with a usable email) are listed with their rating; the customer unticks anyone they
  don't want and approves. Removed suppliers are never contacted. The admin's Review and Send page shows whether the customer has approved and
  waits for it (the operator can override), and the Discover page won't let anyone re-include a removed supplier.
- **Choose a supplier.** Records the agreed price, asks consent before sharing contact details, tells the winner (with details) and everyone
  else (without), and cancels other visits.
- **Close the request** (with an optional private reason), **add details or photos** (emailed to suppliers still in play; at most 5 updates).

An ended request (closed or awarded) is finished everywhere: supplier pages say how it ended and refuse new responses (opting out always
works), the visit picker refuses changes, and the automated follow-ups skip it. The customer is also emailed once when their shortlist is
ready to review (`shortlist_ready` follow-up).

### Photos and PDF attachments

Once the chat is under way, choosing photos sends them straight away (on the landing page they wait for the job description, and
they wait if the customer has typed a caption). The summary lists every attached file by name, exactly as suppliers will see it
(`photo-1.jpg`, `Site Plan Rev B.pdf`). Customers can attach photos and PDFs (for example engineering drawings): up to 6 files per message, 12 per request, 4 PDFs,
10 MB per PDF, 15 MB in total. File type is decided by content, never by name. Photos are re-encoded (GPS/EXIF stripped).
PDFs are rebuilt page by page (`services/documents.py`), which drops JavaScript, embedded files, form scripts, annotation
actions and document metadata while keeping the pages vector-sharp; encrypted, oversized or unreadable PDFs are rejected with a
clear message. Suppliers receive readable file names (`photo-1.jpg`, `Proposed Lot Layout - Rev B.pdf`).

### Caching

Asset URLs carry a version token (`/static/chat.css?v=...`, see `server/assets.py`) and are cacheable forever, while the
page itself is never cached. After changing Python code, restart `start_site.bat`; static file changes show immediately.
