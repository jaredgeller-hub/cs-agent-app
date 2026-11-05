# CS Agent

CS Agent is an AI colleague for Customer Success Managers, built by Alwaison. It watches the tools a CSM already uses (Gmail, Google Calendar, Google Drive, and the CRM) and surfaces judgment before the CSM has to ask: a morning digest, pre-call prep, post-call recaps with drafted follow-ups, priorities, and churn-risk alerts. It never acts without the CSM's approval. Every draft is surfaced first, and the CSM edits and approves before anything is sent or written.

- License: PolyForm Small Business License 1.0.0 (see [License](#license))
- Company: https://alwaison.com

This README explains how to run your own copy of CS Agent: what you need, how to configure it, and what to review before real users sign in.

---

## What CS Agent is

CS Agent is a FastAPI backend (`main.py` plus the `product/` package) and a React frontend (`frontend/`, built with Vite). CSMs sign in with Clerk at `/app`, connect Google and their CRM, and get a daily briefing, account context, alerts and drafted actions. Claude (Anthropic) does the reasoning. OAuth tokens are encrypted before they are stored in Postgres.

Principles the code holds to:

- Nothing is sent or written without the CSM approving a draft first.
- Email bodies are not stored. CS Agent keeps relationship signals, not communications.
- Security and billing checks fail closed.

## Requirements

- **Python 3.11** (the exact version is in `runtime.txt`).
- **Node 22** (`.nvmrc`), used to build the frontend.
- **Postgres**, required. Tables are created automatically on startup. The SQL files in `migrations/` are one-off fixes for an older database and are not needed on a fresh install.
- **Redis plus the background worker**, optional. Without Redis, background jobs run inside the web process.
- Accounts with **Anthropic**, **Clerk** and **Google Cloud**. HubSpot, Gainsight, Slack, Zoom and Stripe are optional.

## Quickstart (local)

```bash
# 1. Backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env                       # fill in the required variables below

# 2. Org config
cp org_config.example.json org_config.json # then edit it, see "Config files"

# 3. Frontend (Vite reads frontend/.env, not the root .env)
cp frontend/.env.example frontend/.env     # set VITE_CLERK_PUBLISHABLE_KEY
cd frontend && npm ci && npm run build && cd ..

# 4. Run
python3 -m uvicorn main:app --reload --host 0.0.0.0 --port 8000
./worker.sh                                # optional, needs REDIS_URL
```

Open `http://localhost:8000/app` and sign in.

Generate an encryption key for `TOKEN_ENCRYPTION_KEY` with:

```bash
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Keep the production key somewhere safe and separate from your local one. If you lose it, every stored OAuth token becomes unreadable and every user has to reconnect.

If you skip the frontend build, `/app` serves an older page that sends you to a sign-in route that does not exist when `DEMO_MODE` is off. Build the frontend first.

For frontend work you can also run `cd frontend && npm run dev`, which proxies API calls to port 8000.

## Deploying

Any host that runs a Python web process, a background process, Postgres and Redis works. On Render, for example:

| Service | Build command | Start command |
|---|---|---|
| Web | `./build.sh` | `uvicorn main:app --host 0.0.0.0 --port $PORT` |
| Background worker (optional) | `pip install -r requirements.txt` | `./worker.sh` |
| Postgres | | provides `DATABASE_URL` |
| Redis (optional) | | provides `REDIS_URL` |

Things to know:

- **Run exactly one web instance.** The scheduler (absence checks, trial emails, post-call checks, connection checks) runs inside the web process, so a second instance would run every scheduled job twice.
- **Local disk is wiped on every deploy** on hosts like Render. Digests and some per-user files are also written to `product/data/`. Postgres is the record that survives; treat the disk copies as a cache.
- **Give the worker the same environment variables as the web service**, including `TOKEN_ENCRYPTION_KEY`, `DATABASE_URL` and `REDIS_URL`.
- **Set the `VITE_*` variables on the web service.** They are read when `./build.sh` runs and baked into the frontend, so changing one needs a rebuild.
- **Set `PUBLIC_BASE_URL`** to your public origin (for example `https://cs.example.com`). Links, emails and every OAuth callback are built from it. The default is `http://localhost:8000`.

## Environment variables

`.env.example` lists every variable the backend reads, with a comment on each. `frontend/.env.example` lists the build-time variables. Summary:

**Required**

| Variable | What it is |
|---|---|
| `TOKEN_ENCRYPTION_KEY` | Fernet key that encrypts OAuth tokens. The app refuses to start without it. |
| `DATABASE_URL` | Postgres connection string. |
| `ANTHROPIC_API_KEY` | Claude API key. |
| `CLERK_SECRET_KEY` | Clerk backend key. |
| `CLERK_FRONTEND_API` | Clerk frontend API host, without `https://`. If unset, every signed-in request gets 401. |
| `PUBLIC_BASE_URL` | Public origin of your deployment. Required anywhere other than localhost. |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | Google OAuth client for Gmail and Calendar, the core signals. |

**Build-time (in `frontend/.env`, or on the web service when hosted)**

| Variable | What it is |
|---|---|
| `VITE_CLERK_PUBLISHABLE_KEY` | Required. Clerk publishable key. |
| `VITE_API_URL` | Optional. API base URL. Default: same origin. |
| `VITE_COMPANY_NAME` | Optional. Company name in the app's tagline. |

**Optional**

| Variable | Default and purpose |
|---|---|
| `ANTHROPIC_MODEL` | `claude-sonnet-4-6`. Model for every LLM call. |
| `REDIS_URL` | Unset: jobs run in the web process. |
| `ADMIN_SECRET` | Unset: `/admin/*` routes are refused. Send it as the `X-Admin-Key` header. |
| `CORS_ALLOWED_ORIGINS` | `PUBLIC_BASE_URL` plus local dev ports. A `*` is rejected. |
| `COMPANY_NAME`, `COMPANY_LEGAL_NAME`, `COMPANY_DOMAIN` | Your company, shown on pages and in emails. |
| `SUPPORT_EMAIL`, `FROM_EMAIL`, `SENDER_NAME`, `SENDER_TITLE` | Contact and sender details. |
| `SYSTEM_EMAIL_SMTP_USER`, `SYSTEM_EMAIL_PASSWORD`, `SYSTEM_EMAIL_ADDRESS` | Notification emails. The sender uses Gmail SMTP (`smtp.gmail.com`), so use a Google account with an app password. Unset: no emails are sent. |
| `FOUNDER_EMAIL` | Receives pipeline failure alerts and feedback. Default: `SUPPORT_EMAIL`. |
| `WAITLIST_FORM_URL` | Target of the landing page buttons. Default: `mailto:SUPPORT_EMAIL`. |
| `DIGEST_GENERATOR_ENABLED`, `CHAT_TOOL_USE_ENABLED`, `CHAT_TOOL_TIMEOUT_S` | Feature flags, off by default. |
| `SLACK_WEBHOOK_URL` | Internal Slack notifications for operators. |
| `DEMO_MODE` and the `DEMO_*` / `CSM_*` variables | Leave unset. They enable a scripted demo, not the product. |

**Per connector** (each one only matters if you offer that connector)

| Connector | Variables |
|---|---|
| HubSpot | `HUBSPOT_CLIENT_ID`, `HUBSPOT_CLIENT_SECRET` |
| Gainsight | `GAINSIGHT_CLIENT_ID`, `GAINSIGHT_CLIENT_SECRET`, `GAINSIGHT_DOMAIN` |
| Slack | `SLACK_CLIENT_ID`, `SLACK_CLIENT_SECRET`, `SLACK_SIGNALS_ENABLED` |
| Zoom | `ZOOM_CLIENT_ID`, `ZOOM_CLIENT_SECRET`, `ZOOM_WEBHOOK_SECRET_TOKEN`, `ZOOM_SIGNALS_ENABLED` |

Each connector also accepts a `*_REDIRECT_URI` override. You should not need it if `PUBLIC_BASE_URL` is right.

**Billing (off by default)**

`BILLING_ENABLED` is off unless you set it to `true`. While it is off, the app never calls Stripe and never limits access by subscription. Turn it on only if you want to charge your own users, and then set `STRIPE_SECRET_KEY`, `STRIPE_PRICE_ID` and `STRIPE_WEBHOOK_SECRET`.

## Clerk setup

Clerk is the only way users sign in.

1. Create an application in Clerk and choose your sign-in methods (email, Google, or both).
2. Copy the publishable key to `VITE_CLERK_PUBLISHABLE_KEY`, the secret key to `CLERK_SECRET_KEY`, and the frontend API host to `CLERK_FRONTEND_API`.
3. **Restrict sign-ups.** Anyone who can create an account in your Clerk application can sign in to your CS Agent and is set up automatically. In Clerk, switch sign-ups to an allowlist of your email domain or addresses, or to invite-only.
4. For production, follow Clerk's steps for a production instance on your own domain.

Every signed-in user gets an identity equal to their **Clerk user ID** (it starts with `user_`). Their connected accounts and data are stored under that ID.

## Config files

`org_config.json` holds your organization settings and, optionally, per-CSM settings.

```bash
cp org_config.example.json org_config.json
```

Then edit it:

- Set `org_name`, `crm_type` (`hubspot` or `gainsight`) and `timezone`.
- **Delete the two example CSMs** under `csms`. The scheduled jobs loop over every listed CSM.
- To give a CSM their own settings (name, timezone, account list, notifications), add an entry **keyed by their Clerk user ID**, with `email` set to the address they sign in with. Do not key entries by a name such as `jane`: the CSM's connected accounts are saved under their Clerk ID, so a different key would make them look disconnected.
- CSMs you do not list can still sign in. They use their Clerk user ID and default settings, and pick their accounts during onboarding.

The smallest valid file is:

```json
{"org_name": "Your Org", "crm_type": "hubspot", "timezone": "America/New_York", "csms": {}}
```

Do not copy `pilot_config.example.json`. It is an older single-CSM format, only read when `org_config.json` is missing.

If neither file exists, signed-in users still get in with default settings. If `org_config.json` exists but is invalid JSON or is missing a required field, sign-ins are refused until you fix it.

## Connector apps

Register each OAuth app with the redirect URI shown, built from your `PUBLIC_BASE_URL` (written below as `https://YOUR_HOST`).

| Connector | Redirect URI | Scopes and notes |
|---|---|---|
| Google (Gmail, Calendar, Drive, Docs) | `https://YOUR_HOST/auth/google/callback` | Enable the Gmail, Google Calendar, Google Drive and Google Docs APIs. Scopes requested at connect: `gmail.readonly`, `calendar.readonly`, `drive.meet.readonly`, `documents.readonly`, `userinfo.email`, `openid`. Turning on Google actions in Settings also requests `gmail.send`. |
| HubSpot | `https://YOUR_HOST/auth/hubspot/callback` | Uses HubSpot's remote MCP server with OAuth and PKCE, so create an app that HubSpot allows to use its MCP server. Scopes are set on the HubSpot app. |
| Gainsight | `https://YOUR_HOST/auth/gainsight/callback` | Set `GAINSIGHT_DOMAIN` to your tenant domain. Scope `read_write`. |
| Slack | `https://YOUR_HOST/auth/slack/callback` | User-token scopes: `search:read`, `chat:write`, `users:read`, `channels:read`, `users:read.email`. |
| Zoom | `https://YOUR_HOST/auth/zoom/callback` | Scopes: `meeting:read:search`, `meeting:read:assets`, `cloud_recording:read:list_user_recordings`, `cloud_recording:read:content`. Webhooks: `https://YOUR_HOST/webhooks/zoom` and `https://YOUR_HOST/webhooks/zoom/deauthorize`. |
| Stripe (billing only) | Webhook: `https://YOUR_HOST/billing/webhook` | Only if `BILLING_ENABLED=true`. |
| Anthropic | none | API key only. |

**Google: Gmail read is a restricted scope.** A public Google app that requests it needs Google's verification and a security assessment, and until then shows a warning and caps users. If your company uses Google Workspace and only your own staff will connect, set the OAuth consent screen's user type to **Internal**. Internal apps skip verification.

## Pages to review before going live

These pages ship with the code. Placeholders such as `{{COMPANY_NAME}}` are filled from your environment variables, but some text makes claims about the original deployment. Review each one and change what is not true for you.

| Page | File | What to check |
|---|---|---|
| `/privacy` | `product/static/privacy.html` | Placeholder. Replace it with your own privacy policy. |
| `/terms` | `product/static/terms.html` | Placeholder. Replace it with your own terms. |
| `/accessibility` | `product/static/accessibility.html` | Says testing last happened on October 14, 2026, lists "the cs-agent.app" home, privacy, terms and sign-in pages as tested with no WCAG 2.1 AA issues found, says the waitlist form is hosted by Google Forms, and links to cs-agent.app in the footer. |
| `/support` | `product/static/support.html` | Promises a reply within one business day, points to "cs-agent.app/docs", and links to cs-agent.app in the footer. |
| `/docs` | `product/static/docs.html` | Says "Go to cs-agent.app and click Sign up" and "Your 14-day free trial starts immediately. No credit card required." That is not true with billing off. |
| `/security/*` | `docs/policies/*.md` | Effective date 2026-05-18. Says HTTP to cs-agent.app is redirected to HTTPS, data is stored in PostgreSQL on Render, and production access is limited to "the founder". Names specific cloud providers. Sets response deadlines (1, 3 and 5 business days). Defines scope as cs-agent.app and its subdomains, and links to a changelog on cs-agent.app. |
| `/` | `index.html` | Neutral landing page. Replace it with your own if you want one. |

One string in the app itself also names the original domain: an error message in `frontend/src/components/settings/SettingsModal.jsx` ("for cs-agent.app and try again").

## Demo-only files

These files are for a scripted demo and are not part of running the product. Leave `DEMO_MODE` off and you can ignore them.

- `ui_prototype_2.html`: the scripted demo, served at `/demo` only when `DEMO_MODE` is on.
- `scripts/seed_csm_data.py`, `scripts/seed_demo_day.py`: seed a test Google account with demo data. Tests import them.
- `gmail_auth_setup.py`: an older desktop OAuth flow, used by `scripts/onboard_csm.py`. Real users connect Google from the app instead.
- `generate_calendar.py`: writes sample calendar data for the demo.

## Running tests

Run the tests from a **git clone**, not from a downloaded zip: some tests call `git ls-files`.

```bash
python3 -m pytest tests/ --timeout=30      # backend, no secrets needed
cd frontend && npm ci && npm test          # frontend
```

The backend tests set their own test encryption key and read only the `*.example.json` config files. They need no database, no Redis and no API keys.

## Roadmap

CS Agent is a work in progress. No dates, on purpose.

### Now: a self-hostable work in progress

- Daily digest and chat across Gmail, Calendar, Drive, HubSpot, Gainsight, Slack and Zoom.
- Runs on your own infrastructure under your own Clerk, Google and Anthropic accounts.

### Next: an MCP plug-in

- Connect CS Agent to the tools you already use through MCP, instead of waiting for a custom connector for each one.

### Later

- Microsoft 365: Outlook mail and calendar.
- Easier deploys, such as a one-click Render setup or Docker.
- A full accessibility audit of the signed-in app.

### Have a request?

Open a GitHub issue. Connectors beyond MCP get built when a team needs one.

## License

CS Agent is licensed under the [PolyForm Small Business License 1.0.0](LICENSE).

- **Free** for individuals and small businesses as the license defines them: use for the benefit of a company (including a sole proprietorship) with fewer than 100 employees and independent contractors and less than 1,000,000 USD (2019, inflation-adjusted) total revenue in the prior tax year.
- **Larger companies** need a commercial license.
- **Commercial licensing:** CONTACT_PLACEHOLDER

See [LICENSE](LICENSE) for the full terms.
