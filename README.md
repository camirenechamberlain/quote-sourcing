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

A mobile-first chat page (`server/static/`) in front of the intake agent. Customers enter a mobile and email, describe the
job, attach photos, review a summary and confirm. A confirmed request becomes a `QUEUED` job (visible in the Streamlit
pages) and the customer gets a private progress link (`/j/<token>`).

Run it:

```
.venv\Scripts\python.exe -m uvicorn server.main:app --port 8000
```

Then open http://localhost:8000. Tests: `.venv\Scripts\python.exe -m pytest tests -q`.

Limits and safety (all in `.env`, see `.env.example`):
- `DAILY_QUOTE_LIMIT` quote requests per mobile per rolling 24h (default 10), enforced at start **and** at confirmation.
- `GLOBAL_DAILY_JOBS` across all customers, `IP_DAILY_SESSIONS` per IP, `MAX_MESSAGES_PER_SESSION` per chat.
- Mobile numbers are **not SMS-verified yet** (`User.mobile_verified` is always false), so the per-number limit can be
  dodged with other numbers; the IP and global caps are the real backstop until verification is added.
- Photos are re-encoded server-side (location/EXIF data is stripped) and shrunk in the browser before upload.
- Strict CSP, `Referrer-Policy: no-referrer`, hashed chat tokens, no public API docs.

Putting it on quoteschmote.com from your own PC: use a Cloudflare Tunnel pointing at `localhost:8000`, then set
`PUBLIC_BASE_URL=https://quoteschmote.com` and `TRUST_PROXY_HEADERS=1` in `.env` (only enable the latter behind the tunnel).
