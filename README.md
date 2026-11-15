# Email automation

A small standalone app that sends personal emails to your contacts from your
Gmail account. No CRM and no LinkedIn: contacts come from a CSV upload (or are
typed in) and live in your local PostgreSQL database (or a SQLite file if you
don't configure one).

- **Contacts:** import a CSV (needs an `email` column; name, company and title are
  picked up automatically, any other column becomes a `{{column}}` field) and
  organise contacts into lists. Unsubscribed contacts are never emailed.
- **Campaigns:** one email per contact of a list, written from your template
  (`Hi {{first_name}}, ...`) or by the AI (Gemini) for each person. Optional
  files and a button (e.g. "Book a call"). Live preview while you write.
- **Review:** by default every email waits for you to approve, edit or skip it.
- **Sending:** a worker sends one email at a time through the Gmail API,
  `SEND_INTERVAL_SECONDS` apart, never more than your daily limit.

## Setup

Needs Python 3.10–3.13.

```bash
python -m venv .venv
.venv\Scripts\activate            # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
copy .env.example .env            # macOS/Linux: cp .env.example .env
```

Fill in `.env` (below), create the database, then:

```bash
psql -U postgres -c "CREATE DATABASE email_automation;"
python manage.py migrate
python manage.py createsuperuser  # your login for the app
```

| Setting | Where to get it |
|---|---|
| `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_HOST`, `DB_PORT` | Your local PostgreSQL (defaults: `email_automation`, `postgres`, localhost, 5432). Leave `DB_NAME` empty to use a SQLite file instead. |
| `DJANGO_SECRET_KEY` | Any long random string. |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | See "Connect Gmail" below. |
| `GEMINI_API_KEY` | [Google AI Studio](https://aistudio.google.com/apikey). Only needed for AI-written emails. |

### Connect Gmail (one time)

1. In [Google Cloud Console](https://console.cloud.google.com/) create a project and
   enable the **Gmail API**.
2. **OAuth consent screen:** choose *External*, fill in an app name, and add the
   Gmail address you will send from under **Test users**.
3. **Credentials > Create credentials > OAuth client ID**, type **Web application**,
   with the authorised redirect URI `http://localhost:5000/gmail/callback/`.
   Open the app at `http://localhost:5000` (not `127.0.0.1`) when you connect, so the
   sign-in returns to the same address.
4. Copy the client ID and secret into `.env`.
5. Start the app, open **Settings > Connect Gmail**, and sign in with that address.

The app asks only for permission to *send* email (plus your address); it
cannot read your inbox. Disconnecting in Settings also revokes the access at Google.

## Run

### Always on (Windows, recommended)

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install_autostart.ps1
```

Registers two scheduled tasks, `EmailAutomation-web` and `EmailAutomation-worker`,
that start hidden when you log in and restart if they stop. Logs go to `logs\web.log`
and `logs\worker.log`. Remove them with `scripts\uninstall_autostart.ps1`.

### By hand

Two terminals, both with the virtual environment active:

```bash
python manage.py runserver        # the app: http://localhost:5000
python manage.py run_worker       # does all the automatic work
```

Only one worker can run at a time (a database lock): a second one waits on standby.

## What runs automatically

The worker, every few seconds:

1. **Writes** first emails and follow-ups with the AI.
2. **Checks** each email (right name, no placeholders or links, sensible length). Passing
   emails are sent on their own unless the campaign asks to review everything;
   the rest wait under "To review" with the reason.
3. **Follows up** in the same Gmail thread after N days without a reply (up to 3, set per
   campaign). Any real reply, bounce or unsubscribe stops them; out-of-office does not.
4. **Sends** one email at a time, only in business hours (default Mon–Fri 10:00–18:00),
   within today's limit, `SEND_INTERVAL_SECONDS` apart.
5. **Reads replies** every couple of minutes and the AI sorts them: Interested, Asked for
   info, Referral, Not now, Not interested, Out of office. You get an email for the
   promising ones; "not interested" unsubscribes the contact.
6. **Protects the account**: warm-up, limit cuts on bounces, pauses on Gmail limits.

`run_worker --once` does a single pass.

## Tests

```bash
python manage.py test mailer
```

## Good to know

- **Limits:** a free Gmail account can send about 500 emails a day; new accounts
  should start much lower (30–50) and keep the 45-second spacing, or Gmail may
  flag the account.
- **Cold email rules:** only email people who can reasonably expect it, and honour
  unsubscribe requests (mark the contact *Unsubscribed* on the Contacts page).
- **Your data** (contacts, Gmail tokens) stays in your local database. Keep `.env`
  (and `db.sqlite3`, if you use it) private; both are git-ignored.
