# SuperTeacher Backend — Codebase Map

*Regenerated 2026-10-01 at HEAD `073b2b2`. Evidence used for this document: a fresh full test-suite run (631 passed in 268 s, 93% line coverage), `alembic heads` → `0009 (head)`, `docker compose config --quiet` (exit 0), `scripts/verify_database.py --live` (PASS against PostgreSQL 18.1), and an AST route inventory cross-checked against the live `app.routes` (46 application routes vs 50 live — the 4 extras are FastAPI's own docs routes). Citations use `path:line` (`identifier`); anything not line-verified is marked **UNVERIFIED**. `.env` was never opened — only `.env.example` was used. No source files were modified.*

## 1. Ten-line summary

1. FastAPI 0.121.2 service (app version 0.2.0) with 46 API routes under `/api/v1`, 22 PostgreSQL tables, Alembic head `0009`.
2. Three-role vocabulary (student / teacher / admin), role-split namespaces (`/me`, `/admin`), JWT auth with rotating, server-side revocable refresh sessions.
3. Layered design: endpoints → services (business rules) → repositories (SQL), with a flush-only transaction convention (endpoints commit/rollback).
4. First module is the Student Registration Portal (accounts, profiles, shared catalog, enrollments) inside a whole-project backend.
5. Per-endpoint slowapi rate limiting; storage is in-process `memory://`, which forces a single-worker deployment.
6. Fail-fast startup validation of `ENVIRONMENT`, `SECRET_KEY` and CORS before the first request.
7. Two audit trails: `auth_events` (write-only, no read API) and `student_profile_history` (admin read API).
8. 631 tests = 457 unit + 174 integration, 93% coverage; a role matrix drives every one of the 46 routes over HTTP.
9. Dockerfile, compose and CI exist and are config-verified; the image build itself is **UNVERIFIED** (Docker daemon unavailable).
10. Top findings: unthrottled public readiness probe, README-vs-code contract drift (403 vs 404), N+1 admin list reads, several dead helpers.

## 2. Stack and versions

| Component | Version | Source |
| --- | --- | --- |
| FastAPI | 0.121.2 | `requirements.txt:1` (`fastapi`) |
| Uvicorn | 0.40.0 | `requirements.txt:2` (`uvicorn`) |
| Pydantic / pydantic-settings | 2.12.5 / 2.13.0 | `requirements.txt:3` (`pydantic`) · `requirements.txt:4` (`pydantic-settings`) |
| SQLAlchemy | 2.0.44 | `requirements.txt:6` (`SQLAlchemy`) |
| Alembic | 1.17.0 | `requirements.txt:7` (`alembic`) |
| psycopg driver | 3.2.13 (binary) | `requirements.txt:8` (`psycopg`) |
| slowapi | >=0.1.9,<1 | `requirements.txt:11` (`slowapi`) |
| argon2-cffi / PyJWT / resend | >=23.1,<26 / >=2.8,<3 / >=2.0,<3 | `requirements.txt:9` (`argon2-cffi`) · `requirements.txt:10` (`PyJWT`) · `requirements.txt:12` (`resend`) |
| pytest / pytest-cov / httpx (dev) | 8.4.2 / 6.3.0 / >=0.27 as installed | `requirements-dev.txt:6` (`pytest`) · `requirements-dev.txt:7` (`pytest-cov`) · `requirements-dev.txt:9` (`httpx`) |
| Python | 3.12.10 (this run); image `python:3.12-slim`; CI `3.12` | `Dockerfile:3` (`python:3.12-slim`) · `.github/workflows/ci.yml:40` (`3.12`) — README claims 3.11+/3.14, see findings |
| PostgreSQL | 18.1 local (live check); `postgres:16-alpine` in compose and CI | `docker-compose.yml:45` (`postgres:16-alpine`) · `.github/workflows/ci.yml:22` (`postgres:16-alpine`) |
| Local dev server | `uvicorn --reload` (running during this audit) | `README.md:610` (`uvicorn`) |

## 3. Directory map (repository root = backend root)

| Path | Purpose | Source |
| --- | --- | --- |
| `app/main.py` | FastAPI entry point: middleware, exception handlers, router mount | `README.md:741` (`main.py`) |
| `app/core/` | config, database, security, auth dependencies, rate limit, logging, email, time mixin | `README.md:742` (`core/`) |
| `app/data/` | reference datasets + validator + seeder core | `README.md:748` (`data/`) |
| `app/models/` | 22 SQLAlchemy models (one file per table) | `README.md:749` (`models/`) |
| `app/schemas/` | Pydantic request/response contracts | `README.md:750` (`schemas/`) |
| `app/api/v1/` | versioned router + 6 endpoint modules | `README.md:751` (`api/v1/`) |
| `app/services/` | business logic (auth, profiles, registrations, admin, catalog) | `README.md:752` (`services/`) |
| `app/repositories/` | SQL data access | `README.md:753` (`repositories/`) |
| `alembic/` | migrations `0001`…`0009` + `env.py` | `README.md:754` (`alembic/`) |
| `scripts/` | `create_admin.py`, `seed_reference_data.py`, `verify_database.py`, `cleanup_auth_sessions.py`, `schema_reset.sql` | `README.md:755` (`scripts/`) |
| `tests/` | `unit/` (default) + `integration/` (opt-in PostgreSQL) | `pytest.ini:7` (`testpaths`) |
| `.github/workflows/ci.yml` | GitHub Actions CI | `.github/workflows/ci.yml:1` (`CI`) |
| root files | `Dockerfile`, `docker-compose.yml`, `pytest.ini`, `requirements*.txt`, `.env.example`, `alembic.ini`, `README.md` | `README.md:766` (`README.md`) |
| `docs/CODEBASE_MAP.md` | this document (new in this commit) | — |

## 4. Entry points: run / test / deploy

**Run (development)** — `python -m uvicorn app.main:app --reload --port 8000` (`README.md:610` (`uvicorn`)); health check `curl http://127.0.0.1:8000/api/v1/health` (`README.md:616` (`curl`)). The app object is built at `app/main.py:61` (`FastAPI`), startup validation runs at `app/main.py:57` (`validate_startup`), and all v1 routers mount under `/api/v1` at `app/main.py:211` (`api_router`).

**Run (production container)** — shell-form exec of uvicorn with proxy headers, exactly one worker: `Dockerfile:41` (`exec uvicorn`). One worker is mandatory because rate-limit counters are per-process (`app/core/config.py:110` (`RATE_LIMIT_STORAGE_URI`), rationale at `Dockerfile:37` (`ONE worker`)). The image ships no migrations/seeding on start (`Dockerfile:22` (`deliberately runs NO migrations`)).

**Test** — three documented commands (`README.md:414` (`python -m pytest`)): default unit-only via `addopts = -m "not integration"` (`pytest.ini:10` (`addopts`)); integration opt-in `-m integration`; everything `-m ""`. Integration tests require `ENVIRONMENT=testing` + a `*_test` database and never create/drop databases (`README.md:419` (`Integration tests`)).

**Deploy (ordered)** — migrations `python -m alembic upgrade head` (`README.md:672` (`alembic upgrade head`)) → seed (`README.md:675` (`seed_reference_data.py`)) → first admin (`README.md:678` (`create_admin.py`)). Docker path: `docker compose up --build -d` (`README.md:715` (`docker compose up`)); compose sets `DB_HOST: db` (`docker-compose.yml:25` (`DB_HOST`)) and its healthcheck probes the readiness route (`docker-compose.yml:37` (`health/ready`)). CI: install → `alembic upgrade head` (`.github/workflows/ci.yml:50` (`alembic upgrade head`)) → `verify_database.py --live` (`.github/workflows/ci.yml:53` (`verify_database.py`)) → full pytest run (`.github/workflows/ci.yml:56` (`pytest`)).

**Operational guards** — seeder refuses production (`scripts/seed_reference_data.py:62` (`production`)); `create_admin.py` requires `--allow-production` (`scripts/create_admin.py:149` (`production`)); `verify_database.py --rebuild` refuses production (`scripts/verify_database.py:1193` (`ENVIRONMENT`)); session-cleanup live runs refuse production (`scripts/cleanup_auth_sessions.py:74` (`args.dry_run`)).

**Maintenance scripts**

| Script | Purpose | Source |
| --- | --- | --- |
| `scripts/create_admin.py` | offline admin bootstrap (password via TTY/`ADMIN_PASSWORD`) | `README.md:756` (`create_admin.py`) |
| `scripts/seed_reference_data.py` | idempotent reference-data seeder (`--dry-run`) | `README.md:758` (`seed_reference_data.py`) |
| `scripts/verify_database.py` | structural + `--live` schema verification | `scripts/verify_database.py:53` (`EXPECTED_TABLES`) |
| `scripts/cleanup_auth_sessions.py` | expired/revoked session purge (`cleanup_expired`) | `scripts/cleanup_auth_sessions.py:118` (`cleanup_expired`) |
| `scripts/schema_reset.sql` | one-shot destructive schema reset (manual) | `scripts/schema_reset.sql:16` (`DROP SCHEMA public CASCADE`) |

## 5. Feature inventory

| Feature | Surface | Tables | Status |
| --- | --- | --- | --- |
| Accounts + JWT auth (register/login/refresh/logout) | 16 `/auth/*` + 3 identity `/me` routes | `users` (`app/models/user.py:23` (`__tablename__`)), `auth_sessions` (`app/models/auth_session.py:33` (`__tablename__`)) | live |
| Login lockout (5 attempts / 15 min) | `/auth/login`, `POST /admin/users/{id}/unlock` | `users.failed_login_count`, `users.locked_until` (migration `0008`) | live (`app/core/config.py:117` (`LOGIN_LOCKOUT_THRESHOLD`)) |
| Password reset via email | `/auth/forgot-password`, `/auth/reset-password` | `password_reset_tokens` (`app/models/password_reset_token.py:20` (`__tablename__`)) | live; needs `RESEND_*` config |
| Teacher invitations (72 h, single-use) | `POST /admin/teachers` + invite/activate/deactivate, `/auth/accept-invite` | `invite_tokens` (`app/models/invite_token.py:24` (`__tablename__`)), `teachers` (`app/models/teacher.py:26` (`__tablename__`)) | live (`app/services/admin_user_service.py:53` (`INVITE_TTL`)) |
| Authentication audit trail | written on every auth/admin mutation — **no read API** | `auth_events` (`app/models/auth_event.py:25` (`__tablename__`)) | write-only: only the repository and model reference `AuthEvent` (grep over `app/`) |
| Student profiles + profile history | `/me/student`, `/admin/students*` | `students` (`app/models/student.py:24` (`__tablename__`)), `student_profile_history` (`app/models/student_profile_history.py:30` (`__tablename__`)) | live (admin history read at `app/api/v1/endpoints/admin_students.py:197` (`get_profile_history`)) |
| Shared catalog (read-only) | 10 `GET /catalog/*` routes | 11 reference tables | live; datasets intentionally empty (`README.md:397` (`EMPTY`)) |
| Student registrations + readiness | `/me/registrations*`, `/admin/registrations*`, `/registrations/readiness` | `student_enrollments` (`app/models/enrollment.py:30` (`__tablename__`)), `student_subjects` (`app/models/student_subject.py:15` (`__tablename__`)) | live |
| Pathway-level / program-subject mappings | no direct endpoints — consumed via `?pathway=` catalog filter and registration subject derivation | `pathway_levels` (`app/models/pathway_level.py:17` (`__tablename__`)), `program_subjects` (`app/models/program_subject.py:22` (`__tablename__`)) | internal (`app/services/registration_service.py:207` (`PathwayLevel`), `app/repositories/enrollment_repository.py:142` (`subject_ids_for_program_version`)) |
| Reference seeding | CLI only | the 11 catalog tables | datasets empty by design |
| First-admin bootstrap | CLI only (no HTTP route) | `users` | live (`README.md:629` (`no HTTP endpoint`)) |
| Session retention cleanup | CLI only | `auth_sessions` | live (`app/repositories/auth_session_repository.py:131` (`cleanup_expired`)) |
| Health liveness / readiness | 2 public routes | none (DB `SELECT 1` for readiness) | live (`app/api/v1/endpoints/health.py:32` (`/health/ready`)) |

## 6. Routes (46 total, all under `/api/v1`)

Cross-check: an AST scan of decorators found **46** routes in 6 endpoint files (11 `APIRouter` objects aggregated in `app/api/v1/router.py:26` (`api_router`)); importing the app and reading `app.routes` found **50**, the 4 extras being FastAPI's docs routes (`/docs`, `/docs/oauth2-redirect`, `/openapi.json`, `/redoc`), which are disabled in production (`app/main.py:69` (`docs_url`)). **Only_AST=0, only_LIVE=4 (docs routes) — no mismatches.**

Rate-limit constants below are defined in `app/core/config.py:87` (`RATE_LIMIT_LOGIN`) … `app/core/config.py:103` (`RATE_LIMIT_REGISTRATION_WRITE`); `—` means no limit decorator. Auth column: `public` = no dependency; others are the FastAPI dependency enforcing the role (`app/core/auth_dependencies.py:152` (`require_student`), `app/core/auth_dependencies.py:158` (`require_admin`)).

### Routers

| File | Routers (prefix) | Routes |
| --- | --- | --- |
| `app/api/v1/endpoints/auth.py` | `router` (`/auth`), `me_router` (`/me`), `student_me_router` (`/me/student`), `teacher_me_router` (`/me/teacher`) — `app/api/v1/endpoints/auth.py:76` (`APIRouter`) | 16 |
| `app/api/v1/endpoints/registrations.py` | `router` (`/registrations`), `me_router` (`/me`), `admin_router` (`/admin`) — `app/api/v1/endpoints/registrations.py:61` (`APIRouter`) | 7 |
| `app/api/v1/endpoints/admin_students.py` | `router` (`/admin/students`) — `app/api/v1/endpoints/admin_students.py:60` (`APIRouter`) | 4 |
| `app/api/v1/endpoints/admin_users.py` | `router` (`/admin`) — `app/api/v1/endpoints/admin_users.py:50` (`APIRouter`) | 7 |
| `app/api/v1/endpoints/catalog.py` | `router` (`/catalog`) — `app/api/v1/endpoints/catalog.py:38` (`APIRouter`) | 10 |
| `app/api/v1/endpoints/health.py` | `router` (no prefix) — `app/api/v1/endpoints/health.py:14` (`APIRouter`) | 2 |

### All 46 routes

| Method | Path | Auth | Rate limit | Handler |
| --- | --- | --- | --- | --- |
| POST | `/admin/registrations` | require_admin | `RATE_LIMIT_REGISTRATION_WRITE` | `app/api/v1/endpoints/registrations.py:252` (`create_registration_for_student`) |
| GET | `/admin/registrations/{enrollment_id}` | require_admin | `RATE_LIMIT_REGISTRATION_READ` | `app/api/v1/endpoints/registrations.py:285` (`read_registration`) |
| POST | `/admin/students` | require_admin | `RATE_LIMIT_STUDENT_WRITE` | `app/api/v1/endpoints/admin_students.py:92` (`create_student_profile`) |
| GET | `/admin/students/{student_id}` | require_admin | `RATE_LIMIT_STUDENT_READ` | `app/api/v1/endpoints/admin_students.py:125` (`read_student_profile`) |
| PATCH | `/admin/students/{student_id}` | require_admin | `RATE_LIMIT_STUDENT_WRITE` | `app/api/v1/endpoints/admin_students.py:157` (`patch_student_profile`) |
| GET | `/admin/students/{student_id}/history` | require_admin | `RATE_LIMIT_STUDENT_HISTORY` | `app/api/v1/endpoints/admin_students.py:197` (`get_profile_history`) |
| GET | `/admin/students/{student_id}/registrations` | require_admin | `RATE_LIMIT_REGISTRATION_READ` | `app/api/v1/endpoints/registrations.py:313` (`list_registrations_for_student`) |
| GET | `/admin/teachers` | require_admin | `RATE_LIMIT_STUDENT_READ` | `app/api/v1/endpoints/admin_users.py:109` (`list_teachers`) |
| POST | `/admin/teachers` | require_admin | `RATE_LIMIT_STUDENT_WRITE` | `app/api/v1/endpoints/admin_users.py:80` (`create_teacher`) |
| POST | `/admin/teachers/{user_id}/activate` | require_admin | `RATE_LIMIT_STUDENT_WRITE` | `app/api/v1/endpoints/admin_users.py:169` (`activate_teacher`) |
| POST | `/admin/teachers/{user_id}/deactivate` | require_admin | `RATE_LIMIT_STUDENT_WRITE` | `app/api/v1/endpoints/admin_users.py:202` (`deactivate_teacher`) |
| POST | `/admin/teachers/{user_id}/invite` | require_admin | `RATE_LIMIT_STUDENT_WRITE` | `app/api/v1/endpoints/admin_users.py:137` (`resend_invite`) |
| PATCH | `/admin/users/{user_id}/role` | require_admin | `RATE_LIMIT_STUDENT_WRITE` | `app/api/v1/endpoints/admin_users.py:240` (`change_role`) |
| POST | `/admin/users/{user_id}/unlock` | require_admin | `RATE_LIMIT_STUDENT_WRITE` | `app/api/v1/endpoints/admin_users.py:280` (`unlock_user`) |
| POST | `/auth/accept-invite` | public | `RATE_LIMIT_REGISTER` | `app/api/v1/endpoints/auth.py:558` (`accept_invite`) |
| POST | `/auth/forgot-password` | public | `RATE_LIMIT_FORGOT_PASSWORD` | `app/api/v1/endpoints/auth.py:483` (`forgot_password`) |
| POST | `/auth/login` | public | `RATE_LIMIT_LOGIN` | `app/api/v1/endpoints/auth.py:168` (`login`) |
| POST | `/auth/logout` | get_current_user | `RATE_LIMIT_REFRESH` | `app/api/v1/endpoints/auth.py:237` (`logout`) |
| GET | `/auth/me` | get_current_user | `RATE_LIMIT_ME_READ` | `app/api/v1/endpoints/auth.py:272` (`read_auth_me`) |
| POST | `/auth/refresh` | public | `RATE_LIMIT_REFRESH` | `app/api/v1/endpoints/auth.py:204` (`refresh`) |
| POST | `/auth/register` | public | `RATE_LIMIT_REGISTER` | `app/api/v1/endpoints/auth.py:136` (`register_student_account`) |
| POST | `/auth/reset-password` | public | `RATE_LIMIT_RESET_PASSWORD` | `app/api/v1/endpoints/auth.py:522` (`reset_password`) |
| GET | `/catalog/academic-years` | public | — | `app/api/v1/endpoints/catalog.py:64` (`read_academic_years`) |
| GET | `/catalog/education-levels` | public | — | `app/api/v1/endpoints/catalog.py:104` (`read_education_levels`) |
| GET | `/catalog/pathways` | public | — | `app/api/v1/endpoints/catalog.py:83` (`read_pathways`) |
| GET | `/catalog/program-versions` | public | — | `app/api/v1/endpoints/catalog.py:180` (`read_program_versions`) |
| GET | `/catalog/programs` | public | — | `app/api/v1/endpoints/catalog.py:150` (`read_programs`) |
| GET | `/catalog/schools` | public | — | `app/api/v1/endpoints/catalog.py:246` (`read_schools`) |
| GET | `/catalog/schools/{school_code}/programs` | public | — | `app/api/v1/endpoints/catalog.py:267` (`read_school_programs`) |
| GET | `/catalog/subjects` | public | — | `app/api/v1/endpoints/catalog.py:128` (`read_subjects`) |
| GET | `/catalog/tvet/programs` | public | — | `app/api/v1/endpoints/catalog.py:223` (`read_tvet_programs`) |
| GET | `/catalog/tvet/sectors` | public | — | `app/api/v1/endpoints/catalog.py:206` (`read_tvet_sectors`) |
| GET | `/health` | public | `RATE_LIMIT_HEALTH` | `app/api/v1/endpoints/health.py:21` (`read_health`) |
| GET | `/health/ready` | public | **none** | `app/api/v1/endpoints/health.py:33` (`read_readiness`) |
| GET | `/me` | get_current_user | `RATE_LIMIT_ME_READ` | `app/api/v1/endpoints/auth.py:300` (`read_me`) |
| POST | `/me/change-password` | get_current_user | `RATE_LIMIT_CHANGE_PASSWORD` | `app/api/v1/endpoints/auth.py:449` (`change_password`) |
| POST | `/me/deactivate` | get_current_user | `RATE_LIMIT_DEACTIVATE_ACCOUNT` | `app/api/v1/endpoints/auth.py:595` (`deactivate_account`) |
| GET | `/me/registrations` | get_current_student | `RATE_LIMIT_REGISTRATION_READ` | `app/api/v1/endpoints/registrations.py:174` (`list_my_registrations`) |
| POST | `/me/registrations` | get_current_student | `RATE_LIMIT_REGISTRATION_WRITE` | `app/api/v1/endpoints/registrations.py:136` (`create_my_registration`) |
| GET | `/me/registrations/{enrollment_id}` | get_current_student | `RATE_LIMIT_REGISTRATION_READ` | `app/api/v1/endpoints/registrations.py:203` (`read_my_registration`) |
| GET | `/me/student` | get_current_student | `RATE_LIMIT_STUDENT_READ` | `app/api/v1/endpoints/auth.py:328` (`read_me_student`) |
| PATCH | `/me/student` | get_current_student | `RATE_LIMIT_STUDENT_WRITE` | `app/api/v1/endpoints/auth.py:407` (`patch_me_student`) |
| POST | `/me/student` | require_student | `RATE_LIMIT_STUDENT_WRITE` | `app/api/v1/endpoints/auth.py:370` (`create_me_student`) |
| GET | `/me/teacher` | require_teacher | `RATE_LIMIT_STUDENT_READ` | `app/api/v1/endpoints/auth.py:627` (`read_me_teacher`) |
| PATCH | `/me/teacher` | require_teacher | `RATE_LIMIT_STUDENT_WRITE` | `app/api/v1/endpoints/auth.py:664` (`patch_me_teacher`) |
| GET | `/registrations/readiness` | public | `RATE_LIMIT_REGISTRATION_READ` | `app/api/v1/endpoints/registrations.py:89` (`read_registration_readiness`) |

## 7. Data model

Verification: structural checks + live checks both **PASS** (this run) — ORM metadata vs migrations drift-free, `alembic_version = ['0009']` (`scripts/verify_database.py:78` (`EXPECTED_REVISION`)), 22 tables present, inventory = 30 foreign keys, 21 unique constraints, 22 check constraints, 30 declared indexes. `alembic heads` reports a single head, `0009`.

### Migrations (linear chain, head = 0009)

| Rev | Revision line | Adds |
| --- | --- | --- |
| 0001 | `alembic/versions/0001_initial_schema.py:38` (`revision`) | initial SuperTeacher schema (`alembic/versions/0001_initial_schema.py:1` (`initial SuperTeacher schema`)) |
| 0002 | `alembic/versions/0002_authentication.py:28` (`revision`) | `users.password_hash` + `auth_sessions` (`alembic/versions/0002_authentication.py:1` (`users.password_hash`)) |
| 0003 | `alembic/versions/0003_password_reset_and_audit.py:11` (`revision`) | reset tokens + audit tables (`alembic/versions/0003_password_reset_and_audit.py:1` (`password reset tokens`)) |
| 0004 | `alembic/versions/0004_student_profile_hardening.py:32` (`revision`) | validation backstops + `lower(email)` unique index (`alembic/versions/0004_student_profile_hardening.py:1` (`student profile hardening`)) |
| 0005 | `alembic/versions/0005_role_vocabulary.py:35` (`revision`) | three-role vocabulary CHECK (`alembic/versions/0005_role_vocabulary.py:1` (`role vocabulary`)) |
| 0006 | `alembic/versions/0006_teachers_and_pending_status.py:32` (`revision`) | `teachers` profile + `pending` status (`alembic/versions/0006_teachers_and_pending_status.py:1` (`teachers profile`)) |
| 0007 | `alembic/versions/0007_invite_tokens_and_audit_actor.py:28` (`revision`) | `invite_tokens` + `auth_events.actor_user_id` (`alembic/versions/0007_invite_tokens_and_audit_actor.py:1` (`invite tokens`)) |
| 0008 | `alembic/versions/0008_users_lockout_columns.py:29` (`revision`) | lockout columns (`alembic/versions/0008_users_lockout_columns.py:1` (`lockout columns`)) |
| 0009 | `alembic/versions/0009_auth_index_drift_fix.py:34` (`revision`) | auth index rename/fix (`alembic/versions/0009_auth_index_drift_fix.py:1` (`auth index drift fix`)) |

### Tables (22)

| Table | Model | Notes (selected keys) |
| --- | --- | --- |
| `users` | `app/models/user.py:23` (`__tablename__`) | role CHECK = student/teacher/admin; lockout columns (0008) |
| `students` | `app/models/student.py:24` (`__tablename__`) | `user_id` UNIQUE FK (`app/models/student.py:28` (`ForeignKey`)) |
| `teachers` | `app/models/teacher.py:26` (`__tablename__`) | `user_id` UNIQUE + optional `school_id` (`app/models/teacher.py:30` (`ForeignKey`)) |
| `student_profile_history` | `app/models/student_profile_history.py:30` (`__tablename__`) | FK students CASCADE (`app/models/student_profile_history.py:34` (`ForeignKey`)), actor SET NULL (`app/models/student_profile_history.py:40` (`ForeignKey`)) |
| `auth_sessions` | `app/models/auth_session.py:33` (`__tablename__`) | FK users CASCADE (`app/models/auth_session.py:37` (`ForeignKey`)) |
| `auth_events` | `app/models/auth_event.py:25` (`__tablename__`) | subject FK CASCADE (`app/models/auth_event.py:29` (`ForeignKey`)), actor SET NULL (`app/models/auth_event.py:38` (`ForeignKey`)), 4 indexes (`app/models/auth_event.py:41` (`__table_args__`)) |
| `password_reset_tokens` | `app/models/password_reset_token.py:20` (`__tablename__`) | FK users CASCADE (`app/models/password_reset_token.py:24` (`ForeignKey`)) |
| `invite_tokens` | `app/models/invite_token.py:24` (`__tablename__`) | FK users CASCADE (`app/models/invite_token.py:28` (`ForeignKey`)) |
| `academic_years` | `app/models/academic_year.py:16` (`__tablename__`) | status vocabulary (planned/active/closed/archived) |
| `pathways` | `app/models/pathway.py:19` (`__tablename__`) | reference |
| `education_levels` | `app/models/education_level.py:15` (`__tablename__`) | reference |
| `pathway_levels` | `app/models/pathway_level.py:17` (`__tablename__`) | pathway↔level mapping (`app/models/pathway_level.py:21` (`ForeignKey`)) |
| `subjects` | `app/models/subject.py:15` (`__tablename__`) | reference |
| `programs` | `app/models/program.py:18` (`__tablename__`) | reference |
| `program_versions` | `app/models/program_version.py:32` (`__tablename__`) | 4-column FKs (`app/models/program_version.py:36` (`ForeignKey`)); lazy `program` relationship (`app/models/program_version.py:80` (`program`)) |
| `program_subjects` | `app/models/program_subject.py:22` (`__tablename__`) | program_version↔subject (`app/models/program_subject.py:26` (`ForeignKey`)) |
| `tvet_sectors` | `app/models/tvet_sector.py:15` (`__tablename__`) | reference |
| `tvet_programs` | `app/models/tvet_program.py:14` (`__tablename__`) | program UNIQUE + sector FK (`app/models/tvet_program.py:18` (`ForeignKey`)) |
| `schools` | `app/models/school.py:16` (`__tablename__`) | reference |
| `school_programs` | `app/models/school_program.py:21` (`__tablename__`) | school↔program_version (`app/models/school_program.py:25` (`ForeignKey`)) |
| `student_enrollments` | `app/models/enrollment.py:30` (`__tablename__`) | one per (student, year) UNIQUE (`app/models/enrollment.py:60` (`uq_student_enrollments`)), status + date CHECKs (`app/models/enrollment.py:64` (`CheckConstraint`)), 5 FK indexes (`app/models/enrollment.py:73` (`Index`)) |
| `student_subjects` | `app/models/student_subject.py:15` (`__tablename__`) | enrollment+subject FKs (`app/models/student_subject.py:19` (`ForeignKey`)) |

## 8. Core flows

**1. Student self-registration** — `POST /auth/register` (`app/api/v1/endpoints/auth.py:136` (`register_student_account`)) → password policy (`app/services/auth_service.py:284` (`validate_password_policy`)) → duplicate pre-check (`app/services/auth_service.py:298` (`get_by_email`)) → atomic user+profile insert (`app/services/auth_service.py:302` (`create_student_user`)) → race losers map to 409 (`app/services/auth_service.py:316` (`IntegrityError`)) → profile-history audit row (`app/services/auth_service.py:332` (`log_change`)) → issue token pair (`app/services/auth_service.py:343` (`_issue_session`)) → endpoint commits (`app/api/v1/endpoints/auth.py:146` (`session.commit`)).

**2. Login + lockout** — `POST /auth/login` (`app/api/v1/endpoints/auth.py:168` (`login`)) checks the lock first (`app/services/auth_service.py:414` (`locked_until`)), always compares against a dummy Argon2id hash for unknown emails (`app/services/auth_service.py:436` (`_DUMMY_HASH`)), counts refused attempts and locks at the threshold (`app/services/auth_service.py:350` (`_register_failed_login`), `app/services/auth_service.py:366` (`LOGIN_LOCKOUT_THRESHOLD`)), clears the budget on success (`app/services/auth_service.py:462` (`failed_login_count`)); the endpoint deliberately **commits** refusals so `login_failed` events survive (`app/api/v1/endpoints/auth.py:179` (`session.commit`)).

**3. Refresh / rotation / logout** — `POST /auth/refresh` validates the token against its server-side session row (`app/services/auth_service.py:223` (`_verify_refresh_credentials`)) under `SELECT FOR UPDATE` (`app/repositories/auth_session_repository.py:47` (`get_by_token_hash_for_update`)), revokes the old session (`app/services/auth_service.py:492` (`revoke`)), issues a new pair (`app/services/auth_service.py:501` (`_issue_session`)) — a replayed token fails on second use. `POST /auth/logout` requires Bearer + refresh token and refuses foreign sessions (`app/services/auth_service.py:523` (`expected_user_id`)). Session cap enforced per issue (`app/services/auth_service.py:196` (`enforce_session_limit`)).

**4. Password reset** — forgot-password stages a SHA-256-digested, 1-hour token (`app/services/auth_service.py:597` (`forgot_password`)) and returns the delivery payload; the endpoint commits first, then queues the email as a background task so no token can be emailed for a rolled-back row (`app/api/v1/endpoints/auth.py:500` (`session.commit`), `app/api/v1/endpoints/auth.py:502` (`background_tasks`)). reset-password consumes the single-use token, marks it used and revokes all sessions (`app/services/auth_service.py:705` (`used_at`), `app/services/auth_service.py:706` (`revoke_all_sessions`)).

**5. Teacher onboarding** — admin creates a `pending` account (`app/services/admin_user_service.py:159` (`create_teacher`), status at `app/services/admin_user_service.py:174` (`PENDING`)); invite token is stored only as a SHA-256 digest (`app/services/admin_user_service.py:126` (`token_hash`)) with 72 h TTL (`app/services/admin_user_service.py:53` (`INVITE_TTL`)); email is best-effort (`app/services/admin_user_service.py:141` (`except Exception`)). Accept-invite verifies token+expiry+single-use, requires pending-teacher (`app/services/auth_service.py:769` (`UserRole.TEACHER`)), sets password and activates (`app/services/auth_service.py:786` (`UserStatus.ACTIVE`)), then logs the invitee in (`app/services/auth_service.py:800` (`_issue_session`)).

**6. Profile self-service / administration** — student creates own profile (`app/api/v1/endpoints/auth.py:370` (`create_me_student`)); PATCH semantics come from `model_fields_set` (`app/services/student_service.py:342` (`model_fields_set`)); admin read/patch (`app/api/v1/endpoints/admin_students.py:125` (`read_student_profile`), `app/api/v1/endpoints/admin_students.py:157` (`patch_student_profile`)); history resolves actor emails in one batched query (`app/api/v1/endpoints/admin_students.py:216` (`actor_ids`)).

**7. Registration create** — student self-route injects the caller's own id (`app/api/v1/endpoints/registrations.py:144` (`student.id`)) → service validates year registrability (`app/services/registration_service.py:226` (`REGISTRABLE_YEAR_STATUSES`)), pathway↔level mapping (`app/services/registration_service.py:207` (`PathwayLevel`)), duplicate enrollment (`app/services/registration_service.py:352` (`find_existing`)), school offering (`app/services/registration_service.py:303` (`SchoolProgram`)) → INSERT with race→409 mapping (`app/services/registration_service.py:389` (`IntegrityError`)) → derives `student_subjects` from program mappings (`app/services/registration_service.py:404` (`subject_ids_for_program_version`)) → caller commits (`app/api/v1/endpoints/registrations.py:155` (`session.commit`)). A foreign enrollment id on the self route gets the same 404 as an unknown id (`app/api/v1/endpoints/registrations.py:213` (`registration.student_id`)).

**8. Admin account operations** — activate/deactivate/role-change/unlock (`app/api/v1/endpoints/admin_users.py:169` (`activate_teacher`), `app/api/v1/endpoints/admin_users.py:240` (`change_role`)) all write `auth_events` naming the actor and revoke every target session on status/role change (`app/services/admin_user_service.py:338` (`revoke_all_sessions`)); self-role-change is refused (`app/services/admin_user_service.py:327` (`actor.id`)).

**9. Health** — liveness `/health` needs no DB and is rate-limited (`app/api/v1/endpoints/health.py:20` (`RATE_LIMIT_HEALTH`)); readiness runs `SELECT 1` (`app/api/v1/endpoints/health.py:46` (`session.execute`)) and answers a generic 503 on failure (`app/api/v1/endpoints/health.py:49` (`HTTPException`)), deliberately unthrottled (`app/api/v1/endpoints/health.py:41` (`Deliberately`)).

## 9. Cross-cutting concerns

- **Configuration / fail-fast** — settings from env + `.env` (`app/core/config.py:244` (`env_file`)); `SECRET_KEY` placeholder refused outside dev/test and must be ≥32 chars (`app/core/config.py:149` (`SECRET_KEY`)); startup refuses missing/blank `ENVIRONMENT` (`app/core/config.py:184` (`ENVIRONMENT`)); production CORS rejects wildcards and drops localhost (`app/core/config.py:217` (`production`)); DB URL built from `DB_*` or `DATABASE_URL` (`app/core/config.py:226` (`database_url`)); cached singleton (`app/core/config.py:251` (`get_settings`)).
- **Identity chain** — decode token → load user → re-check DB status every request (`app/core/auth_dependencies.py:14` (`database`), `app/core/auth_dependencies.py:77` (`UserStatus.ACTIVE`)); role guards are built once (`app/core/auth_dependencies.py:112` (`require_role`)).
- **Rate limiting** — one shared slowapi limiter keyed by remote address (`app/core/rate_limit.py:19` (`Limiter`), `app/core/rate_limit.py:20` (`get_remote_address`)); storage URI from settings (`app/core/rate_limit.py:21` (`storage_uri`)); 429 handler emits API-shaped body with `Retry-After` (`app/main.py:76` (`_rate_limit_exceeded_handler`)).
- **Error contract** — service-family handlers: `AuthError` (`app/main.py:103` (`AuthError`)), `RegistrationError` (`app/main.py:117` (`RegistrationError`)), `ProfileError` (`app/main.py:128` (`ProfileError`)), generic 500 with no leak (`app/main.py:145` (`Exception`)).
- **Logging** — one `dictConfig` applied once at import (`app/core/logging.py:26` (`LOGGING_CONFIG`)), single-line format (`app/core/logging.py:23` (`SINGLE_LINE_FORMAT`)), extra fields not interpolated (structural leak guard).
- **HTTP hardening** — security-headers middleware (`app/main.py:154` (`_security_headers`)), HSTS only in production (`app/main.py:161` (`Strict-Transport-Security`)); CORS explicit origins, no credentials, methods GET/POST/PATCH (`app/main.py:171` (`CORSMiddleware`), `app/main.py:175` (`allow_methods`)); docs disabled in production (`app/main.py:71` (`openapi_url`)); Bearer scheme advertised in OpenAPI (`app/main.py:197` (`securitySchemes`)).
- **Transactions** — services/repositories flush only (`app/services/auth_service.py:19` (`flush`), `app/repositories/enrollment_repository.py:9` (`flush`)); endpoints own commit/rollback (`app/api/v1/endpoints/registrations.py:155` (`session.commit`)).
- **Email** — Resend client configured from settings (`app/core/email.py:20` (`RESEND_API_KEY`)); failures never leak to callers (`app/services/admin_user_service.py:141` (`except Exception`)).
- **Audit** — every auth/admin mutation writes `auth_events` via the repository (`app/repositories/auth_event_repository.py:11` (`log_event`)); profile changes write `student_profile_history`.

## 10. Test strategy

- **Commands** — default unit (`pytest.ini:10` (`addopts`)); `-m integration` opt-in; `-m ""` everything (`README.md:416` (`python -m pytest`)). Marker defined at `pytest.ini:11` (`integration`).
- **This run** — 631 tests = **457 unit + 174 integration**, all passed in 268 s; coverage **93%** (3075 statements, 225 missed) (fresh run evidence at HEAD `073b2b2`).
- **Coverage highlights** — `app/api/v1/endpoints/health.py` 100%, `app/api/v1/endpoints/auth.py` 81% (38 missed, all exception-fallback branches such as `app/api/v1/endpoints/auth.py:279` (`AuthError`)), `app/api/v1/endpoints/registrations.py` 83%, `admin_students.py` 84%, `admin_users.py` 90%, `app/services/auth_service.py` 91%, `app/services/registration_service.py` 99%, `app/main.py` 92% (run output).
- **Integration safety** — tests require `ENVIRONMENT=testing` + `DB_NAME` ending `_test` and only ever truncate/rollback the test database (`README.md:419` (`Integration tests`)).
- **Role matrix** — `tests/unit/test_endpoint_role_matrix.py` enumerates all 46 routes as data (6 student `tests/unit/test_endpoint_role_matrix.py:52` (`STUDENT_ROUTES`), 2 teacher `tests/unit/test_endpoint_role_matrix.py:61` (`TEACHER_ROUTES`), 14 admin `tests/unit/test_endpoint_role_matrix.py:66` (`ADMIN_ROUTES`), 5 any-role `tests/unit/test_endpoint_role_matrix.py:83` (`ANY_ROLE_ROUTES`), 19 public `tests/unit/test_endpoint_role_matrix.py:91` (`PUBLIC_ROUTES`) = 46) and drives real HTTP calls through the full v1 router on a probe app (`tests/unit/test_endpoint_role_matrix.py:198` (`_call`), `tests/unit/test_endpoint_role_matrix.py:147` (`api_app`)) — 401/403/never-401 semantics per route (`tests/unit/test_endpoint_role_matrix.py:212` (`test_anonymous_is_refused_on_every_protected_route`)).
- **Route↔test derivation** — AST route inventory vs test-source scan: **0 of 46 routes lack an HTTP-level test** (all 46 paths appear in `tests/`; the 6 routes without a literal `client.method(path)` call are exercised via the matrix and parametrized contract tests, e.g. `tests/integration/test_admin_users_api.py:201` (`("GET"`)). Analysis method: string-literal extraction + segment-wildcard matching.
- **CI** — runs migrations, `verify_database.py --live`, then the full suite (`.github/workflows/ci.yml:53` (`verify_database.py`), `.github/workflows/ci.yml:56` (`pytest`)).

## 11. Findings (by severity)

### Medium

1. **Public, unthrottled readiness probe holds a pool connection.** `GET /health/ready` has no auth and no rate-limit decorator, takes a request-scoped pool connection and runs `SELECT 1` on every probe (`app/api/v1/endpoints/health.py:33` (`read_readiness`), `app/api/v1/endpoints/health.py:46` (`session.execute`)); the code documents the choice as orchestrator-bounded (`app/api/v1/endpoints/health.py:41` (`Deliberately`)). An unauthenticated flood can pin pool + DB. `/health` by contrast is throttled (`app/api/v1/endpoints/health.py:20` (`RATE_LIMIT_HEALTH`)).
2. **README contradicts the code on a security contract.** README says a foreign enrollment id on `GET /me/registrations/{id}` returns **403** (`README.md:122` (`403`)); the endpoint deliberately returns the identical **404** as an unknown id to avoid an existence leak (`app/api/v1/endpoints/registrations.py:217` (`RegistrationNotFoundError`)). The route docstring is correct; the README is stale.
3. **Endpoint layer bypasses the repository/service layer on three reads.** Direct ORM access from handlers: `session.get(User, …)` (`app/api/v1/endpoints/auth.py:334` (`session.get`)), `session.get(User, …)` (`app/api/v1/endpoints/admin_students.py:132` (`session.get`)), and an inline `select(User)` for actor emails (`app/api/v1/endpoints/admin_students.py:219` (`select`)) — inconsistent with the endpoints-call-services convention used elsewhere.
4. **N+1 queries on admin teacher list.** `list_teachers` loads users in one query, then issues one extra `Teacher` query per row (`app/services/admin_user_service.py:218` (`_read_teacher`) → `app/services/admin_user_service.py:74` (`select`)); at page size 200 that is 201 queries.
5. **Extra queries on registration reads.** Eager loading stops at `program_version` (`app/repositories/enrollment_repository.py:44` (`joinedload`)) while `ProgramVersion.program` is a default lazy relationship (`app/models/program_version.py:80` (`program`)), so each summary issues one more query for `program` (`app/services/registration_service.py:129` (`program_version.program`)); the create-path read-back also lazy-loads `subjects` on a freshly inserted row (`app/services/registration_service.py:150` (`subjects`), built from an instance with no eager options, `app/repositories/enrollment_repository.py:118` (`enrollment`)). The list/get read paths themselves are eager-loaded (`app/repositories/enrollment_repository.py:38` (`joinedload`)).
6. **Seeder refuses production with no supported later catalog-update path.** `seed_reference_data.py` exits when `ENVIRONMENT=production` (`scripts/seed_reference_data.py:62` (`production`)), and no other mechanism updates catalog rows later — the README tells operators to seed before switching to production (`README.md:683` (`refuses`)).

### Low

7. **Access tokens are not revocable before expiry (by design).** Every access token mints a `jti` (`app/core/security.py:124` (`jti`)) that nothing ever reads (grep over `app/`: only `security.py`); revocation therefore relies on refresh-session revocation (`app/services/admin_user_service.py:338` (`revoke_all_sessions`)) plus the per-request DB status check (`app/core/auth_dependencies.py:77` (`UserStatus.ACTIVE`)); the README documents that access tokens simply expire (`app/api/v1/endpoints/auth.py:227` (`Access tokens`)).
8. **Dead code (verified by grep for callers + coverage).** `registration_service.commit()` has no callers anywhere and its body is uncovered (`app/services/registration_service.py:521` (`commit`)); auth-session repo helpers `get_by_token_hash` (`app/repositories/auth_session_repository.py:40` (`get_by_token_hash`)), `get_by_id` (`app/repositories/auth_session_repository.py:62` (`get_by_id`)) and `mark_used` (`app/repositories/auth_session_repository.py:66` (`mark_used`)) have none either (coverage misses lines 63, 68-70); `dev_guard.require_development_stage` is imported only by tests — its own docstring says "nothing, today" (`app/core/dev_guard.py:11` (`nothing`)); `student_service.get_student_profile` has no callers (`app/services/student_service.py:324` (`get_student_profile`)). (Note: `cleanup_expired` is *not* dead — used by `scripts/cleanup_auth_sessions.py:118` (`cleanup_expired`).)
9. **README stack line is stale.** README claims "Python 3.11+ (developed and verified on Python 3.14)" (`README.md:25` (`Python 3.11+`)) while the verified interpreter everywhere else is 3.12 (`Dockerfile:3` (`python:3.12-slim`), CI `3.12`).
10. **Destructive manual escape hatch with only a comment as guard.** `scripts/schema_reset.sql` runs `DROP SCHEMA public CASCADE` (`scripts/schema_reset.sql:16` (`DROP SCHEMA public CASCADE`)); it is documented as a one-shot for empty stale tables (`scripts/schema_reset.sql:15` (`stale`)) but nothing prevents running it against a populated database.
11. **Auth error-fallback branches are the least tested code.** `app/api/v1/endpoints/auth.py` sits at 81% coverage; all 38 missed lines are exception handlers/fallbacks (e.g. `app/api/v1/endpoints/auth.py:279` (`AuthError`)) — acceptable, but a future refactor could break the rollback semantics silently.

### Checked and refuted (seeds verified against code)

- "Every route lacks an HTTP-level test" — **refuted**: all 46 routes are parametrized in the role matrix with real HTTP calls (Section 10).
- "Registration list/get reads are N+1" — **refuted for the read paths**: `get_by_id`/`list_for_student` eager-load all catalog relations (`app/repositories/enrollment_repository.py:38` (`joinedload`), `app/repositories/enrollment_repository.py:78` (`joinedload`)); the residual N+1 is item 5 above.

## 12. Unread / UNVERIFIED

- **Never opened (by rule):** `.env` — only `.env.example` was read.
- **Not built/run:** Docker image build and container start are **UNVERIFIED** (Docker daemon unavailable during this audit); `docker compose config --quiet` passed (syntax/config only).
- **Read selectively, not exhaustively:** `app/schemas/*` bodies (schema behavior is inferred from endpoint docstrings + coverage); `alembic/versions/*` bodies beyond revision lines and 0009's index ops (indirectly verified by `verify_database.py` structural + live drift checks, both PASS); `app/data/*` internals; `app/services/catalog_service.py` and `app/repositories/catalog_repository.py`; most of `app/services/student_service.py`; most of `tests/` (counts and matrix verified, individual files not line-audited); `scripts/create_admin.py` (guard lines verified only); `alembic/env.py`.
- **Environment notes:** local PostgreSQL is 18.1 while compose/CI pin 16-alpine (both verified by their own checks); a `uvicorn --reload` dev server was running during the audit and was left untouched.
- **Citation audit:** every `path:line` in this document was machine-checked (file exists, line number in range, paired identifier present within ±3 lines of the cited line) as part of this regeneration.
