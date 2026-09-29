# SuperTeacher API (backend/)

## What this backend is

This is the **SuperTeacher backend API**. It serves the SuperTeacher project
as a whole, and the **Student Registration Portal** is its first module.
It is a brand-new, self-contained Python service added *beside* the existing
applications in this repository.

It is **independent from the existing desktop application** and from every
other application in the repo. The existing SuperTeacher desktop application
and other applications are not modified by this backend.

## Purpose

Serves the SuperTeacher backend as a whole. Current module: the Student
Registration Portal — student accounts, schools, education levels and
pathways (O-Level, A-Level, TVET), programs and their versions, subjects,
and enrollment. Future modules (authentication, tutoring, etc.) will live
in the same backend.

## Current technology stack

- Python 3.11+ (developed and verified on Python 3.14)
- FastAPI — web framework
- Uvicorn — ASGI development server
- Pydantic / pydantic-settings — settings and (later) request/response schemas
- SQLAlchemy 2.0.x — ORM and database toolkit
- Alembic — schema migrations
- psycopg 3 (psycopg[binary]) — PostgreSQL driver

## Authentication (Phase 5G)

The backend derives identity from authenticated credentials — never from
`student_id`/`user_id` parameters supplied by callers, and never from
frontend-selected identity.

### Account creation

`POST /api/v1/auth/register` creates a **student account** (user + student
profile, atomically) and issues a token pair:

- role is fixed server-side to `student` — the request cannot create
  teacher/admin identities;
- email is required, normalized to lowercase and unique — including
  case-insensitive duplicates (functional index on `lower(email)`); a
  duplicate returns **409** with the deliberately generic detail
  `"email cannot be used to create an account"` so the public endpoint
  never confirms an email exists (anti-enumeration);
- `date_of_birth` must be plausible: not in the future, not before
  `1900-01-01`, at least `STUDENT_MIN_AGE_YEARS` (default 2) old and
  not older than `STUDENT_MAX_AGE_YEARS` (default 120) — violations
  are a readable **422**, never a database 500;
- `full_name` must contain only letters (Unicode), spaces, hyphens,
  apostrophes and periods — digits and markup are refused with 422;
- the password is validated server-side (minimum length, never empty —
  `PASSWORD_MIN_LENGTH`) and hashed with **Argon2id** (`argon2-cffi`).
  Plaintext is never stored or logged;
- a failure at any step rolls back the whole operation.

Administrative bare-profile creation (`POST /admin/students`) requires
`admin` authorization and **also requires `email`**
(phone-only identities can never authenticate). Its duplicate-email 409
keeps the explicit message (`a user with email '...' already exists`) —
only the public register endpoint is generic. Every profile creation
(admin, self-registration, or self-service attach) writes a
`student_profile_history` row with `change_type="create"` attributed to
the acting administrator (or the account itself). A student account
that somehow lacks a profile can attach one via
`POST /api/v1/me/student` (409 when a profile already exists, 403 for
non-students).

### Login, refresh, logout

```
POST /api/v1/auth/login     { "email", "password" } → { access_token, refresh_token, ... }
POST /api/v1/auth/refresh   { "refresh_token" }      → new pair (old session revoked — rotation)
POST /api/v1/auth/logout    { "refresh_token" } (Bearer required) → revokes that session
```

Login failures return one generic 401 message — unknown email, wrong
password and suspended/disabled accounts are deliberately
indistinguishable. Login never mutates account status.

### Token usage

Send `Authorization: Bearer <access_token>` on protected endpoints. Access
tokens are short-lived HS256 JWTs (`ACCESS_TOKEN_EXPIRE_MINUTES`, default
30) with minimal claims (`sub` user UUID, `role`, `typ`, `iat`, `exp`) —
never passwords or profile/catalog payloads. Refresh tokens are JWTs bound
to a server-side `auth_sessions` row (SHA-256 digest stored, raw token
never persisted); they are **revocable** and rotate on every refresh, so a
replayed refresh token is refused. Logout revokes the session server-side.

The database — not the token — is the authority for role and status: a
suspended or disabled account is refused on its next request even with a
valid, unexpired access token.

### Role-split endpoints and authorization

Every protected endpoint belongs to exactly **one** role namespace — no
route serves both a student and an administrator:

- **Self-Service** (`/me`: `GET /me`, `POST /me/change-password`,
  `POST /me/deactivate`) — any authenticated role;
- **Student Self-Service** (`/me/student`, `/me/registrations`) —
  `role=student` only; the identity always comes from the Bearer token,
  so registering "as someone else" is not even expressible
  (`POST /me/registrations` has no `student_id` field — supplying one is
  422);
- **Administration** (`/admin/students`, `/admin/registrations`) —
  `role=admin` only (reusable `require_admin` guard; cross-role calls are
  403 before any handler runs).

A student's data access is token-derived: `GET /me/registrations`
returns only the caller's own rows, and another student's enrollment id
on `GET /me/registrations/{id}` is a 403 (UUIDs are identifiers, not
authorization). An administrator registers on behalf of a student with
an explicit `student_id` on `POST /admin/registrations`. A missing
student profile is a 403 domain error whose detail points at the
self-service remedy (`POST /api/v1/me/student`) — never an implicit
profile creation.

Role authorization is reusable infrastructure (`require_student`,
`require_teacher`, `require_admin`); all three paths are implemented.

### Error contract

`401` missing/invalid/expired credentials (generic detail, no JWT
internals) · `403` wrong role for the namespace (`require_admin` answers
with `"administrator role required for this operation"`; student-only
routes refuse non-students), wrong
object ownership on `/me/*`, or a missing student profile · `404` unknown
id for an authorized caller (an application-wide exception handler maps
`ProfileError` statuses, so an unknown student id is a clean 404 on
admin read **and** PATCH, never a 500 — students get 403 from the role
guard first) · `409` duplicate account/enrollment · `422` invalid request
(including implausible dates of birth, spoofed/unknown body keys and
failed name/phone/country validation) · `503` catalog/year unavailability.

### Configuration (authentication)

```
SECRET_KEY=<generated secret>            # REQUIRED in production; placeholder refused
ACCESS_TOKEN_EXPIRE_MINUTES=30
REFRESH_TOKEN_EXPIRE_DAYS=30
PASSWORD_MIN_LENGTH=8
```

### Password recovery

Implemented: `POST /api/v1/auth/forgot-password` answers 204
identically whether the account exists or not (no email enumeration)
and, for existing accounts, emails a single-use reset link
(`{FRONTEND_URL}/reset-password?token=...`) via `app.core.email` —
delivery requires the `RESEND_*` settings; send failures are logged,
never disclosed. `POST /api/v1/auth/reset-password` consumes the token
(JWT signature + expiry + single-use server-side tracking in
`password_reset_tokens`, revokes all sessions, logs a `password_reset`
audit event). Signed-in users change their password via
`POST /api/v1/me/change-password`.

## Current endpoints (Phases 5B–5G, role-split)

```
# --- public (no token) ---------------------------------------------------------
GET    /api/v1/health
POST   /api/v1/auth/register                     (student account + tokens)
POST   /api/v1/auth/login
POST   /api/v1/auth/refresh                      (refresh token required)
POST   /api/v1/auth/forgot-password              (204 always)
POST   /api/v1/auth/reset-password               (reset token)
GET    /api/v1/registrations/readiness
GET    /api/v1/catalog/academic-years|pathways|education-levels|subjects
GET    /api/v1/catalog/programs|program-versions|tvet/sectors|tvet/programs
GET    /api/v1/catalog/schools | /catalog/schools/{school_code}/programs

# --- self-service (any role, Bearer) ------------------------------------------
POST   /api/v1/auth/logout                       (Bearer + refresh token)
GET    /api/v1/me
GET    /api/v1/auth/me                           (identity: id, role, student link)
POST   /api/v1/me/change-password
POST   /api/v1/me/deactivate

# --- student self-service (Bearer, role=student) ------------------------------
GET    /api/v1/me/student                        (403 + remedy when no profile)
POST   /api/v1/me/student                        (attach a missing profile)
PATCH  /api/v1/me/student
GET    /api/v1/me/registrations
POST   /api/v1/me/registrations                  (no student_id field — 422 if sent)
GET    /api/v1/me/registrations/{enrollment_id}  (owner only, else 403)

# --- administration (Bearer, role=admin) --------------------------------------
POST   /api/v1/admin/students                    (create profile pair)
GET    /api/v1/admin/students/{student_id}
PATCH  /api/v1/admin/students/{student_id}
GET    /api/v1/admin/students/{student_id}/history (paginated audit trail)
POST   /api/v1/admin/registrations               (student_id REQUIRED)
GET    /api/v1/admin/registrations/{enrollment_id}
GET    /api/v1/admin/students/{student_id}/registrations
```

**Migration note (role split).** The former dual-role routes were
removed, not aliased: `GET/PATCH /students/{id}` (split into
`GET/PATCH /me/student` + `/admin/students/{id}`), `POST /registrations`
(split into `/me/registrations` + `/admin/registrations`),
`GET /registrations/{id}` (split into `/me/registrations/{id}` +
`/admin/registrations/{id}`) and `GET /students/{id}/registrations`
(replaced by `/me/registrations` for the owner and
`/admin/students/{id}/registrations` for administrators). Swagger groups
the namespaces under the tags *Self-Service*, *Student Self-Service* and
*Administration*.

`GET /health` needs no database. Catalog endpoints are read-only and
public. Interactive documentation: `/docs` (Swagger UI, with the Bearer
scheme available under "Authorize") and `/redoc`.

### Student profile rules (validation, PATCH semantics, audit)

Profile fields are validated identically on registration, admin create,
self-service create (`POST /me/student`) and PATCH (shared schema
validators — no drift between paths):

- `email` — required on create, `EmailStr`, max 255 chars, stored
  lowercase; identity anchor: never changed by PATCH;
- `full_name` — letters (Unicode, accents preserved), spaces, `-`, `'`,
  `.` only; at least one letter; digits, control characters and markup
  are 422;
- `gender` — `male` | `female` | `other` | `undisclosed` or null;
- `country` — Unicode letters/spaces/`-`/`.`/`'`, 1–80 chars, or null;
- `phone` — canonicalized to E.164 (`+` and digits) when present;
- `date_of_birth` — ISO date, not future, not before `1900-01-01`,
  at least `STUDENT_MIN_AGE_YEARS` old and not older than
  `STUDENT_MAX_AGE_YEARS` (422 otherwise).

**PATCH semantics** (`PATCH /me/student` and `PATCH /admin/students/{id}`):
fields absent from the JSON body are left untouched; an explicit
`null` **clears** `gender` and `country`; `full_name` cannot be
cleared (NOT NULL — 422). Identity anchors (`email`, `phone`,
`date_of_birth`) and unknown keys are rejected outright (422 — the
update schema is `extra="forbid"`).

**Audit history** — every creation and every successful field update
writes a `student_profile_history` row (`change_type` ∈ `create`|`update`,
`field_name` ∈ `profile`|`full_name`|`gender`|`country`, both enforced by
DB CHECK constraints; `changed_by` records the acting admin, or the
student on self-service changes). `GET /admin/students/{id}/history` is
admin
only and paginates with `limit` (1–200, default 50) and `offset`
(≥ 0, 422 outside range); each row resolves `changed_by_email` for
human-readable attribution.

### Registration validation rules (authoritative — the service layer)

Cross-entity rules enforced by `app/services/registration_service.py`
(Phase 3C Option-A: service-layer validation, no composite FKs):

- student, academic year, pathway (by code), education level (by code),
  and school must all exist (404 otherwise);
- the academic year must be `planned` or `active` — a `closed`/`archived`
  year refuses with 503 (the current verified 2025/2026 year is closed);
- pathway ↔ education level must be a real `pathway_levels` row (422);
- a supplied program version must match the full offering context
  `(academic_year, pathway, education_level)` (422) and be `active`;
- a TVET-typed program must have its TVET sector profile (503 when absent);
- the school must actually offer the program version through a real
  `school_programs` row (422) — offerings are never inferred;
- one enrollment per `(student, academic year)`: service pre-check returns
  409, and the database UNIQUE constraint
  `uq_student_enrollments_student_id_academic_year_id_key` is the final
  protection — a concurrent duplicate INSERT surfaces as the same 409,
  never a 500;
- the whole registration is atomic: enrollment + derived subjects commit
  together or roll back completely;
- `student_subjects` derive only from `program_version → program_subjects
  → subjects`, never from the bare subjects table.

### Known catalog limitations (honest by design)

The verified development catalog currently contains academic years (one,
closed), pathways, education levels and subjects. **Programs, program
versions, program subjects, TVET sectors/programs, schools and school
offerings are intentionally empty** until authoritative sources are loaded
(Phase 4B decision — no invented data). Registration therefore completes
end-to-end only against test fixtures in `super_teacher_db_test`; against
the development catalog it truthfully reports unavailability (503 / 422 /
empty lists), and `GET /api/v1/registrations/readiness` reports exactly
which sections are missing.

## Shared reference data (Phase 4A)

The education reference data — academic years, pathways, education levels,
subjects, programs, program versions, TVET sectors/programs, schools, and
school program offerings — is **shared SuperTeacher data**. Student
Registration is its first consumer; Curriculum, Learning, Progress and other
future modules reuse the same catalog.

The datasets live in `app/data/` (one module per dataset, plain Python
structures) and are loaded by the idempotent seeder:

```bash
python scripts/seed_reference_data.py --dry-run   # report only, zero writes
python scripts/seed_reference_data.py             # load (idempotent upserts)
```

The seeder:

- processes datasets in an explicit foreign-key-safe order,
- inserts records absent from the database and matches existing rows on each
  dataset's declared natural key (e.g. `pathways.code`, the
  `program_versions` offering tuple — never `program_versions.code`),
- never deletes catalog records and never touches `users`, `students`,
  `student_enrollments`, or `student_subjects`,
- treats `academic_years` and `program_versions` as **verify-only**: drift
  between dataset and database is reported, never auto-rewritten,
- validates every dataset structurally (required fields, unknown fields,
  duplicate natural keys, resolvable parent references) **before** any
  database write.

> **The datasets are intentionally EMPTY right now.** The authoritative
> Rwanda education catalog (real academic years, combinations, subjects,
> TVET trades, schools) is researched, reviewed and added in a later phase,
> through this same loader. Nothing in `app/data/` is authoritative data yet.

## Tests

Development dependencies (test tooling only — runtime deps stay in
`requirements.txt`):

```bash
python -m pip install -r requirements-dev.txt
```

Run from `backend/`:

```bash
python -m pytest                    # unit tests (default; no database needed)
python -m pytest -m integration     # PostgreSQL integration tests (opt-in)
python -m pytest -m ""              # everything
```

Integration tests require a dedicated **test database** and refuse to run
otherwise: `ENVIRONMENT=testing` and `DB_NAME` ending in `_test` (e.g.
`super_teacher_db_test`) must both hold, and the test database must already
have the schema applied (`alembic upgrade head` against it). The suite never
creates or destroys databases, truncates only the test database, and rolls
back every transaction it opens. The development database
`super_teacher_db` can never be targeted by tests.

## Database

PostgreSQL database: **`super_teacher_db`** (configured via the `DB_*`
variables in `.env`; nothing is hard-coded).

The schema (19 tables) is created by Alembic migrations
`0001_initial_schema.py` (the whole-system foundation),
`0002_authentication.py` (`users.password_hash` + `auth_sessions`),
`0003_password_reset_and_audit.py`
(`password_reset_tokens` + `student_profile_history`),
`0004_student_profile_hardening.py` (validation backstops — see below) and
`0005_role_vocabulary.py` (the three-role vocabulary — see below):

`users` (with `password_hash`), `students`, `academic_years`, `pathways`,
`education_levels`, `pathway_levels`, `schools`, `programs`,
`program_versions`, `subjects`, `program_subjects`, `tvet_sectors`,
`tvet_programs`, `school_programs`, `student_enrollments`,
`student_subjects`, `auth_sessions` (revocable refresh sessions),
`password_reset_tokens`, `student_profile_history` (profile audit trail)

Migration `0004` (student-profile hardening) additionally:

- normalizes existing `users.phone` to E.164 and `students.gender` to
  the lowercase enum before adding CHECKs;
- replaces the DOB CHECK with a shared ISO range
  (`'1900-01-01' <= date_of_birth <= CURRENT_DATE`);
- adds vocabulary CHECKs to `student_profile_history`
  (`change_type`, `field_name`);
- adds the functional unique index `uq_users_email_ci_key` on
  `lower(email)` so case-insensitive duplicate emails are refused by the
  database itself (phone-only accounts with `email IS NULL` remain legal —
  NULLs are distinct in unique indexes).

Migration `0005` (role vocabulary) additionally:

- rewrites existing rows: `school_admin`/`rahura_admin` become `admin`,
  and `parent` becomes `student` with `status='suspended'` (a suspended
  legacy parent must be reviewed before it can act as a student);
- drops and recreates `users_role_check` for exactly
  `('student', 'teacher', 'admin')`.

Highlights:

- UUID primary keys (generated by the application), timezone-aware
  `created_at` / `updated_at` on every table that carries timestamps
- Plural table names (`students`, never `student`)
- Explicit constraint names (`students_user_id_fkey`,
  `uq_student_enrollments_student_id_academic_year_id_key`, ...)
- CHECK constraints for roles, statuses, and date ordering
- `program_versions` carries the natural key
  `uq_program_versions_offering_key`
  (`program_id`, `academic_year_id`, `pathway_id`, `education_level_id`), so
  one offering cannot be duplicated — catalog seeders can upsert on it
- Indexes on frequently queried columns, including foreign-key columns that
  are trailing in a composite unique constraint (PostgreSQL does not index
  foreign keys automatically)

**Roles.** `users.role` is the authoritative whole-system vocabulary defined by
`app.models.enums.UserRole` and enforced by `users_role_check`:
`student`, `teacher`, `admin`. Role authorization dependencies live in
`app/core/auth_dependencies.py` (`require_student`, `require_teacher`,
`require_admin`) and guard the role-split routes: `/me/*` self-service is
student-only, administrative profile creation is admin-only, and
teacher/admin guards are exercised by a test-only probe router. Teacher
and admin accounts are provisioned by direct database seeding — there is
no public endpoint that grants them.

**Status vocabulary.** Enrollment/account tables have their own lifecycles
(`EnrollmentStatus`, `UserStatus`, `AcademicYearStatus`, `StudentSubjectStatus`).
The reference/catalog tables — pathways, education_levels, programs,
program_versions, subjects, tvet_sectors, schools, school_programs — share the
minimal `RecordStatus` vocabulary (`active`, `inactive`), enforced by a
CHECK constraint on each table.

Business rules that foreign keys cannot express (pathway must match level,
program version must match pathway/level, school must offer the program) are
reserved for the registration service layer — see
`app/models/enrollment.py` docstrings.

### Configure the database connection

Copy `.env.example` to `.env` and set the database variables:

```
DB_NAME=super_teacher_db
DB_HOST=127.0.0.1
DB_PORT=5432
DB_USER=postgres
DB_PASSWORD=your_password_here
```

The app builds the SQLAlchemy URL from these (`postgresql+psycopg://...`).
An explicit `DATABASE_URL` may be set to override them entirely (e.g. for a
managed provider). `.env` is git-ignored. Never commit credentials.

### Create the schema

From `backend/` (with the virtual environment active):

```bash
python -m alembic upgrade head
```

To create the `super_teacher_db` database itself (if it does not exist), use
your PostgreSQL tooling, e.g.:

```sql
CREATE DATABASE super_teacher_db;
```

### Verify the database setup

```bash
python scripts/verify_database.py          # structural (offline) checks
python scripts/verify_database.py --live   # also inspect PostgreSQL (read-only)
```

The structural run compares the ORM metadata against the Alembic migration
column by column, constraint by constraint and index by index, so a
model/migration mismatch fails instead of passing silently. The `--live` run
does the same against the live PostgreSQL schema plus the `alembic_version`
revision, using read-only queries. `--live --rebuild` additionally resets a
**disposable, empty** development database (`downgrade base` then `upgrade
head`); it refuses to run when `ENVIRONMENT=production` or when any
application table holds rows.

## How to run locally

All commands below are run from the `backend/` directory.

### 1. Create a Python virtual environment

```bash
python -m venv .venv
```

Activate it:

```bash
# Windows (bash / Git Bash)
source .venv/Scripts/activate

# Windows (PowerShell)
.venv\Scripts\Activate.ps1

# Linux / macOS
source .venv/bin/activate
```

### 2. Install requirements

```bash
python -m pip install -r requirements.txt
```

### 3. Start the FastAPI development server

```bash
python -m uvicorn app.main:app --reload --port 8000
```

### 4. Test the health endpoint

```bash
curl http://127.0.0.1:8000/api/v1/health
```

Expected response:

```json
{ "status": "ok", "service": "superteacher-api" }
```

You can also open the interactive API docs at `http://127.0.0.1:8000/docs`.

## Project layout

```
backend/
├── app/
│   ├── main.py            FastAPI entry point
│   ├── core/
│   │   ├── config.py         Settings: DB, JWT secret validation, token lifetimes
│   │   ├── database.py       engine, Base, SessionLocal, get_db dependency
│   │   ├── security.py       Argon2id hashing, password policy, JWT utilities
│   │   ├── auth_dependencies get_current_user / get_current_student / role guards
│   │   └── time_mixin.py     created_at / updated_at columns
│   ├── data/              shared reference datasets + validator + seed loader core
│   ├── models/            SQLAlchemy models (one logical model per file)
│   ├── schemas/           Pydantic schemas (later phase)
│   ├── api/v1/            versioned router + endpoints/
│   ├── services/          business logic (later phases)
│   └── repositories/      data access (later phases)
├── alembic/               migrations (env.py + versions/0001_initial_schema.py)
├── scripts/
│   ├── seed_reference_data.py   idempotent reference-data seeder (--dry-run supported)
│   └── verify_database.py       structural + live database verification
├── tests/                 unit/ (no DB) + integration/ (PostgreSQL test DB, opt-in)
├── .env.example           environment template (copy to .env)
├── .gitignore
├── pytest.ini             unit by default, integration opt-in
├── requirements.txt       runtime dependencies
├── requirements-dev.txt   development/test dependencies
└── README.md
```

## Boundary with the rest of the repository

This backend does not import, call, or modify any existing SuperTeacher code.
The existing SuperTeacher desktop application and other applications are not
modified by this backend.
