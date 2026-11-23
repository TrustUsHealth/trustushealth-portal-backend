# TrustUs Health Portal Backend

FastAPI backend for the TrustUs Health operations portal. It provides authentication, role-based access control,
configuration and dashboard APIs, and the initial Access Portal. The product scope is described in the workspace
[`PRD.md`](../PRD.md).

## Build status

| # | Build | Status |
|---|---|---|
| 0 | **Foundation** (this repo's part) | **Done:** config loader + validation, email-OTP login, RBAC, module auto-discovery, Access Portal API, dashboard tiles API, audit writing, Docker Compose, CI/CD |
| 0 | Foundation (frontend repo, Render setup) | **Done**: separate repo / manual hosting setup |
| 1 | `patient_registry` | **Done (backend):** lab-first upload + manual entries, generic YAML parsing engine (Spectrum, Metropolis), standard field list, atomic `bill_no`, duplicate rules, customers, yearly Excel register with monthly send job |
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
- Transactional emails use a shared branded HTML layout with a plain-text alternative and an embedded TrustUs Health logo.
  Keep `app/core/assets/brand-logo.png` synchronized with the frontend's `public/brand/logo-full.svg`.


The backend folders for `bill_generator`, `reports`, `delivery`, `logs`, and `analytics` do not exist yet; their YAML
registry entries are placeholders. `patient_registry` is live (`/api/v1/patient_registry`): `POST /entries/parse`
(lab + file), `POST /entries` (duplicate check, merge/conflict decisions), `PATCH /entries/{id}`, search, customers,
`/excel_register/*`, and `POST /jobs/excel_monthly` (header `X-Job-Secret`, called by the 1st-of-month cron).
Parsing rules are YAML only: run `python -m scripts.check_samples` after editing `config/partners/*.yaml`.
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

## Option B: use your real MongoDB and Google Apps Script email

This is the recommended way to test actual third-party connections. Run the API directly on your machine so Compose's
local MongoDB and Mailpit settings do not replace your Atlas settings. For Render Free, configure the
Google Apps Script mail transport below.

### MongoDB Atlas

1. Create a database user with read/write access to the database you will use.
2. Add your current public IP to Atlas Network Access (or configure an appropriate private network for hosted use).
   Avoid allowing access from every IP except for a temporary, controlled test.
3. Copy the Atlas `mongodb+srv://...` connection string. Replace its username/password placeholders, URL-encode
   reserved characters in credentials, and include the database name if needed.

### Google Apps Script Gmail delivery (Render Free)

The `google_apps_script` transport sends mail by HTTPS to a Google Apps Script web app, which uses `MailApp` to send
from the Google account that owns the script. It avoids outbound SMTP and requires no paid provider, custom domain, or
separate hosting. Gmail and Apps Script quotas still apply; this is intended for low-volume transactional email, not
bulk mail.

1. Sign in to [Google Apps Script](https://script.google.com/) with the Gmail account that should send the emails.
2. Create a project and copy [`scripts/google_apps_script.gs`](scripts/google_apps_script.gs) into its `Code.gs` file.
3. Generate a random secret of at least 32 characters (for example,
   `python -c "import secrets; print(secrets.token_urlsafe(32))"`). In **Project Settings → Script Properties**, add
   `GOOGLE_SCRIPT_SECRET` with that value. Do not put the secret in the script source or a URL.
4. Choose **Deploy → New deployment → Web app**. Set **Execute as** to **Me** and **Who has access** to **Anyone**,
   then deploy and authorize the requested Gmail permission. Copy the deployed web app URL ending in `/exec`.
5. In Render's backend service **Environment** settings, set:

   ```dotenv
   MAIL_TRANSPORT=google_apps_script
   GOOGLE_SCRIPT_URL=https://script.google.com/macros/s/<deployment-id>/exec
   GOOGLE_SCRIPT_SECRET=<the-same-random-secret>
   ```

   Keep the URL and secret only in backend environment settings; never put them in frontend configuration. The secret
   must match the Apps Script property. Do not share it or print it in logs.
6. Deploy/restart the backend, request an OTP for an invited account, and confirm delivery in the recipient inbox
   (including spam). Render's `202` response means the request was accepted; it does not guarantee delivery.

The script stores an idempotency marker before sending and keeps it for 24 hours. Backend retries reuse the same
delivery key, so a response lost after sending will not cause a duplicate. As with any at-most-once mechanism, if the
script stops after recording the marker but before sending, that email is not automatically resent. There is no durable
email queue. When changing the Apps Script code, create a new deployment version and update the existing deployment.

For local development, use `MAIL_TRANSPORT=smtp` with Mailpit. The optional `gmail` transport still uses Gmail SMTP and
requires `GMAIL_ADDRESS` and `GMAIL_APP_PASSWORD`; it may not work on hosts that restrict outbound SMTP.

### Private Google Drive report storage (optional)

Report PDFs are stored in MongoDB by default. To use one personal Google Drive account instead, configure the Drive
backend only after migrating existing report files. Drive files remain private; the application does not create public
permissions or share links. MongoDB retains report metadata and the Drive file ID. Existing MongoDB-backed files
continue to be readable after the backend switch.
The existing `file.sha256` remains the original-upload dedupe hash; Drive-backed records also store
`file.content_sha256` to verify the exact stored bytes when PDFs have been optimized.

1. In [Google Cloud Console](https://console.cloud.google.com/), create/select a project and enable the **Google Drive
   API**.
2. Configure the OAuth consent screen and set its publishing status to **In production**. A refresh token issued while
   the app is in **Testing** mode expires after 7 days.
3. Create an OAuth client ID with application type **Desktop app** and download its client JSON.
4. On a trusted local machine, install the project dependencies and run
   `python -m scripts.gdrive_auth --create-root-folder path\to\client_secret.json`. Complete consent with the intended
   Google account and securely save the printed refresh token and `GDRIVE_ROOT_FOLDER_ID`. The helper creates the root
   folder through the API so it is accessible using the narrow `drive.file` scope.
5. Configure `GDRIVE_CLIENT_ID`, `GDRIVE_CLIENT_SECRET`, `GDRIVE_REFRESH_TOKEN`, and `GDRIVE_ROOT_FOLDER_ID` in the
   deployment environment. Never commit or log these values. `FILE_STORE` remains `mongo` until migration is verified.

If report uploads fail with `invalid_grant` or the API says Drive authentication expired or was revoked, the refresh
token cannot be renewed automatically. Re-run `python -m scripts.gdrive_auth path\to\client_secret.json` on a trusted
machine, then replace `GDRIVE_REFRESH_TOKEN` in the deployment environment and restart the backend. This is a Drive
credential issue, not a file-size or parsing failure.

#### Rollout

Run commands from the backend directory with a Python environment containing the project dependencies. Use the same
MongoDB settings and Drive credentials for both migration invocations:

1. Set the four `GDRIVE_*` variables in the local environment used for migration. Leave `FILE_STORE=mongo` so the
   running service continues to write to MongoDB.
2. Review the eligible documents with
   `python -m scripts.migrate_files_to_drive --dry-run` (optionally add `--collection reports_YYYY_MM` or `--limit N`).
3. Pause report uploads during the final migration/switch, or repeat the dry-run immediately before switching.
   Migrate with `python -m scripts.migrate_files_to_drive`; repeat until a final dry-run selects zero documents. The
   script verifies each uploaded file's size and SHA-256 before atomically removing its Mongo bytes. Failures are
   reported at the end and can be retried safely.
4. After the migration reports no failures and is complete, set `FILE_STORE=drive` in the service environment and
   restart/deploy the backend. New report uploads then go to Drive; reads continue to follow each document's metadata.

#### Rollback

Set `FILE_STORE=mongo` and restart the service. Newly uploaded reports will return to MongoDB; already migrated
documents continue to be read from Drive, so keep the Google credentials and Drive files available. To fully return
those documents to MongoDB, run a separately verified reverse migration before removing Drive credentials or files.
Do not delete the Drive root folder or its contents as a rollback step.

### Run the API against those services (Windows PowerShell)

```powershell
# Only for a new checkout that does not already have a .env:
Copy-Item .env.example .env
py -3.12 -m venv .venv
./.venv/Scripts/python.exe -m pip install --upgrade pip
./.venv/Scripts/python.exe -m pip install -e ".[dev]"
```

Edit `.env` and set at least:

```dotenv
ENVIRONMENT=local
MONGODB_URI=mongodb+srv://<db-user>:<url-encoded-password>@<cluster-host>/<database>?retryWrites=true&w=majority
MONGODB_DB=trustushealth
JWT_SECRET=<a-random-secret-at-least-32-characters>
BOOTSTRAP_ADMIN_EMAIL=<an-inbox-you-can-read>
MAIL_TRANSPORT=google_apps_script
GOOGLE_SCRIPT_URL=https://script.google.com/macros/s/<deployment-id>/exec
GOOGLE_SCRIPT_SECRET=<the-same-random-secret-configured-in-Apps-Script>
```

If `.env` already exists, edit it in place instead of overwriting it. Set the Apps Script environment variables above
for Render; local Compose can continue using SMTP with Mailpit.

Generate a secret rather than using an example value. `.env` is ignored by Git; do not commit it, paste its contents
into chat, or add credentials to Postman. The `Settings` class loads these values from environment variables / `.env`;
when deploying, configure them as platform secrets instead of uploading `.env`. The Apps Script URL and shared secret
are used only by the backend and are never exposed to the frontend. Start the server:

```powershell
./.venv/Scripts/uvicorn.exe app.main:create_app --factory --host 127.0.0.1 --port 8000 --reload
```

The first startup creates the bootstrap Admin if that email is not already registered. Request an OTP for that exact
address, check the destination inbox (and application logs), then verify the code. `GET /healthz` is deliberately cheap
and does not prove MongoDB or email connectivity: successful OTP request/verification exercises MongoDB, and receiving
the OTP exercises the configured mail transport. OTP delivery runs as a FastAPI background task; the request's `202`
response does **not** guarantee delivery. Transient transport failures are retried up to two times with short delays;
logs omit the recipient, message, OTP, response body, and credentials. There is no durable queue or retry after the
process exits.

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
