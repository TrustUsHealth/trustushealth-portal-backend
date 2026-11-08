# TrustUs Health Portal Backend

FastAPI backend for the TrustUs Health operations portal. It provides authentication, role-based access control,
configuration and dashboard APIs, and the initial Access Portal. The product scope is described in the workspace
[`PRD.md`](../PRD.md).

## Build status

| # | Build | Status |
|---|---|---|
| 0 | **Foundation** (this repo's part) | **Done:** config loader + validation, email-OTP login, RBAC, module auto-discovery, Access Portal API, dashboard tiles API, audit writing, Docker Compose, CI/CD |
| 0 | Foundation (frontend repo, Render/Cloudflare setup) | Pending: separate repo / manual hosting setup |
| 1 | `patient_registry` | Not started |
| 2-6 | `bill_generator`, `reports`, `delivery`, `logs`, `analytics` | Not started |

Modules listed in `config/modules.yaml` but not yet built are skipped at startup and get no dashboard tile. When a
module's folder lands, it mounts and its tile appears with no other change.

## What works today

Implemented API functionality:

- Email one-time-password (OTP) sign-in for invited Admin, Staff, and Engineer accounts.
- OTPs are hashed in storage, expire after five minutes, are single-use, and have per-email and per-IP request limits;
  the response does not disclose whether an address is registered.
- Short-lived bearer access tokens and rotating refresh tokens in an HttpOnly cookie; logout revokes the session.
- Bootstrap of one Admin account using `BOOTSTRAP_ADMIN_EMAIL`.
- Admin APIs for users/invitations, module visibility, access requests and grants, plus an access matrix.
- Dashboard tiles, public app/brand configuration, and access-controlled lab/partner configuration.
- Audit events for authentication and access changes, YAML validation, OpenAPI generation, and `/healthz`.

The backend folders for `patient_registry`, `bill_generator`, `reports`, `delivery`, `logs`, and `analytics` do not
exist yet. Their YAML registry entries are placeholders: they do not mean those APIs or workflows are implemented.
Gmail intake, WhatsApp delivery, PDF generation/sending workflows, bill/report processing, and scheduled jobs are also
not available yet. The email service already accepts PDF attachments for future use, but no current feature generates
or sends them. Sentry can be configured, but monitoring is optional.

## Requirements

- Python 3.12 or Docker Desktop with Docker Compose.
- For real integration testing: a MongoDB/Atlas database and a Gmail account with 2-Step Verification enabled.

## Option A: run the local demo stack

From this directory, in PowerShell:

```powershell
docker compose up --build
```

Compose starts the API (`http://localhost:8000`), MongoDB (`localhost:27017`), and Mailpit (`http://localhost:8025`).
Mailpit captures OTP emails locally; no Gmail account or `.env` is needed. By default the local bootstrap Admin is
`admin@example.com`. To change it, set `BOOTSTRAP_ADMIN_EMAIL` in `.env` (copy `.env.example` first), then restart the
stack. Compose data is stored in the `mongo-data` Docker volume.

1. Open `http://localhost:8000/api/v1/docs` for interactive Swagger docs. OpenAPI JSON is at
   `http://localhost:8000/api/v1/openapi.json`.
2. In Postman (or Swagger), send `POST /api/v1/auth/otp/request` with `{"email":"admin@example.com"}`.
3. Read the six-digit code at `http://localhost:8025`.
4. Send `POST /api/v1/auth/otp/verify` with the email and code. Save the returned `access_token` as a bearer token.
   The refresh cookie is set automatically by clients that retain cookies.
5. Try `GET /api/v1/auth/me`, `GET /api/v1/me/modules`, `GET /api/v1/config/app`, and the Admin endpoints.

Stop with `Ctrl+C`; run `docker compose down` to stop and remove containers. This preserves the database volume.
To also reset local demo data, run `docker compose down --volumes` (this deletes the named MongoDB data volume).

## Option B: use your real MongoDB and Gmail account

This is the recommended way to test actual third-party connections. Run the API directly on your machine so Compose's
local MongoDB and Mailpit settings do not replace your Atlas/Gmail settings. Alternatively, Compose can use Gmail
when `MAIL_TRANSPORT=gmail` is set in `.env`.

### MongoDB Atlas

1. Create a database user with read/write access to the database you will use.
2. Add your current public IP to Atlas Network Access (or configure an appropriate private network for hosted use).
   Avoid allowing access from every IP except for a temporary, controlled test.
3. Copy the Atlas `mongodb+srv://...` connection string. Replace its username/password placeholders, URL-encode
   reserved characters in credentials, and include the database name if needed.

### Gmail SMTP App Password

For this personal, low-volume sender, the backend uses Gmail SMTP over TLS on `smtp.gmail.com:465`. This is simpler to
operate than Gmail API OAuth for a single sending account and requires no custom domain, OAuth client, or billing
account. It supports text/HTML and file attachments. Gmail enforces its own sending limits and anti-abuse rules; this
is not suitable for bulk or marketing mail.

1. In your Google Account, enable **2-Step Verification**.
2. Create an **App Password** in Google Account security settings. Google may hide this option for some account types
   or security configurations.
3. Use your Gmail address and the generated App Password below (paste the 16 characters without spaces). Do not use
   your normal Google password. Store the App Password only in `.env` locally or in the deployment platform's secret
   settings; revoke it in Google if exposed.

The password is sent only to Gmail's SMTP server over TLS. The application strips spaces from Google's displayed
16-character App Password before login. Gmail credentials are not used in the frontend.

### Run the API against those services (Windows PowerShell)

```powershell
# Only for a new checkout that does not already have a .env:
Copy-Item .env.example .env
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

Edit `.env` and set at least:

```dotenv
ENVIRONMENT=local
MONGODB_URI=mongodb+srv://<db-user>:<url-encoded-password>@<cluster-host>/<database>?retryWrites=true&w=majority
MONGODB_DB=trustushealth
JWT_SECRET=<a-random-secret-at-least-32-characters>
BOOTSTRAP_ADMIN_EMAIL=<an-inbox-you-can-read>
MAIL_TRANSPORT=gmail
GMAIL_ADDRESS=<your-gmail-address@gmail.com>
GMAIL_APP_PASSWORD=<your-google-app-password>
```

If `.env` already exists, edit it in place instead of overwriting it. Remove the old `RESEND_API_KEY` and any obsolete
`MAIL_FROM`/`MAIL_TRANSPORT=resend` values; set `MAIL_TRANSPORT=gmail` and the Gmail variables above. An existing
`MAIL_TRANSPORT=resend` is no longer valid. If a Resend API key was previously configured, revoke it at its provider.

Generate a secret rather than using the example value. `.env` is ignored by Git; do not commit it, paste its contents
into chat, or add credentials to Postman. The `Settings` class loads these values from environment variables / `.env`;
when deploying, configure them as platform secrets instead of uploading `.env`. Verify that the hosting provider allows
outbound TCP to Gmail SMTP on port 465 before deploying. Start the server:

```powershell
.\.venv\Scripts\uvicorn.exe app.main:create_app --factory --host 127.0.0.1 --port 8000 --reload
```

The first startup creates the bootstrap Admin if that email is not already registered. Request an OTP for that exact
address, check the destination inbox (and application logs), then verify the code. `GET /healthz` is deliberately cheap
and does not prove MongoDB or email connectivity: successful OTP request/verification exercises MongoDB, and receiving
the OTP exercises Gmail. OTP delivery runs as a FastAPI background task; the request's `202` response does **not**
guarantee delivery. Transient SMTP/network errors are retried up to two times with short delays, then logged without
the recipient, message, OTP, or credentials. There is no durable queue or retry after the process exits.

Keep `ENVIRONMENT=local` for local HTTP testing; production enables secure cookies and disables Swagger docs. Never
expose a production deployment over plain HTTP.

## How to explore and test

- Swagger UI (local/test only): `http://localhost:8000/api/v1/docs`
- OpenAPI contract: `http://localhost:8000/api/v1/openapi.json` or the committed [`openapi.json`](openapi.json)
- Postman collection: [`postman/TrustUsHealth-Backend.postman_collection.json`](postman/TrustUsHealth-Backend.postman_collection.json)
- Health check: `GET http://localhost:8000/healthz`
- In Postman, set `baseUrl` to `http://localhost:8000`. Run **Auth → Request OTP**, obtain the code from Mailpit or
  Gmail, then run **Auth → Verify OTP**. Its test script saves `accessToken`; the collection uses it for protected
  requests. Keep the same hostname (`localhost` versus `127.0.0.1`) across requests so the refresh cookie is retained.
- Useful checks: current identity (`/auth/me`), available dashboard tiles (`/me/modules`), app settings (`/config/app`),
  Admin users/modules (`/access/users`, `/access/modules`), pending requests (`/access/requests`), and grants matrix
  (`/access/matrix`).
- Browse MongoDB with Atlas Data Explorer or Compass. Relevant collections include `users`, `otp_challenges`,
  `refresh_tokens`, `access`, `module_state`, and `audit_logs`; OTPs and refresh tokens are stored hashed, not as the
  original secrets.

The collection has separate folders for health, authentication, dashboard/configuration, staff access requests, and
Admin access management. Add future API requests under the matching feature folder (or create a new feature folder);
use collection variables for shared IDs and keep protected requests on inherited bearer authentication.

## Local development and checks

With Python 3.12 and the development dependencies installed:

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\ruff.exe format --check .
.\.venv\Scripts\python.exe -m scripts.validate_config
.\.venv\Scripts\python.exe -m scripts.export_openapi --check
```

Or run the same project checks with `make check` in an environment that has Make and Python 3.12 installed. Tests use an
in-memory MongoDB and a fake mailer; they do not verify Atlas or Gmail credentials/delivery.

## Configuration and layout

- `.env` contains secrets and environment-specific connection settings; `.env.example` documents the variables.
- `config/*.yaml` contains validated application, module, partner, and workflow configuration.
- `app/core/` contains shared infrastructure; `app/modules/<key>/` contains feature modules.
- A module is mounted only when both `config/modules.yaml` lists it and `app/modules/<key>/router.py` exists. Modules
  import shared infrastructure, not one another.
- Add a module by implementing its router/service/schemas and adding its registry entry. Add a partner using a YAML file
  under `config/partners/`.

## GitHub Actions and deployment status

The checked-in `.github/workflows/ci.yml` runs backend linting, tests, YAML validation, and OpenAPI drift checks on
pull requests to `dev`/`main` and pushes to those branches. `.github/workflows/release.yml` creates a GitHub Release
when code is pushed to `main`; it does not build/push a container image or deploy the service. There is currently no GHCR publishing, Render deploy hook, or production deployment workflow in this backend.

The CI workflow is configured for this FastAPI project (`pyproject.toml` and Python 3.12). A deployment workflow still
needs to be created and configured separately with the intended hosting provider, image/repository settings, and
required GitHub secrets. If a ZIP omitted hidden files, ensure `.github/workflows/` is included in the repository before
expecting GitHub Actions to run; GitHub only discovers workflow YAML committed at that path.

## Known limitations

- The tests use an in-memory MongoDB; test against a separate real database before production.
- Audit client IP and the per-IP limiter depend on proxy headers; the security-critical OTP limits are database-backed.
- The in-memory per-IP limiter assumes one application instance.
