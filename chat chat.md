# Chat Log - Codebase Audit Conversation

> Saved: 2026-09-28 19:30
> Session: `ses_f17474d5bffeKdXpkJVQ7PmaBO`
> Git HEAD: `dc8b91c`
> Content: user prompts and assistant messages only. Tool calls, tool results,
> patches and internal reasoning are excluded for readability.

## Index

1. **You:** You are performing a FULL CODEBASE UNDERSTANDING AND SYSTEM ARCHITECTURE AUDIT.
3. **You:** Continue if you have next steps, or stop and ask for clarification if you are unsure how to proceed.
6. **You:** create a file in this project and name it chat chat and save all this converstaion of me and you in it so that...
8. **You:** do it

---

## 1. You

You are performing a FULL CODEBASE UNDERSTANDING AND SYSTEM ARCHITECTURE AUDIT.

IMPORTANT:
- DO NOT modify, create, delete, rename, move, or format any files.
- DO NOT install dependencies.
- DO NOT run destructive commands.
- DO NOT implement fixes.
- DO NOT assume how anything works based on filenames alone.
- Your task is UNDERSTANDING ONLY.
- Inspect the actual implementation and trace relationships between components.
- If something cannot be verified from the codebase, explicitly mark it as UNKNOWN rather than guessing.

Your goal is to build a complete mental model of this entire repository before any future implementation work begins.

==================================================
1. REPOSITORY INVENTORY
==================================================

Start by inspecting the entire repository structure.

Identify:

- applications
- backend(s)
- frontend(s)
- desktop/mobile applications
- APIs
- services
- workers/background jobs
- scripts
- CLI tools
- databases
- migrations
- configuration
- environment files
- tests
- documentation
- generated files
- assets
- infrastructure/deployment configuration
- Docker/container configuration
- CI/CD
- external integrations
- third-party services
- SDKs
- package/dependency manifests

For every major directory, explain:

- its purpose
- what owns it
- what depends on it
- whether it is runtime code, tooling, documentation, generated code, or unused/dead code

Do not stop at the first level of directories. Follow the repository deeply enough to understand the actual application structure.

==================================================
2. TECHNOLOGY STACK
==================================================

Identify the actual technologies being used.

For each part of the system determine:

- programming language
- framework
- runtime
- package manager
- database
- ORM/database driver
- API framework
- frontend framework
- authentication system
- authorization system
- state management
- validation libraries
- serialization
- caching
- queues
- background processing
- file/object storage
- external APIs
- AI/LLM integrations
- email/SMS integrations
- logging
- monitoring
- testing framework
- build system
- deployment platform

Do not infer technologies from documentation if the source code contradicts it.

==================================================
3. APPLICATION ENTRY POINTS
==================================================

Find every real runtime entry point.

Examples:

- main.py
- app.py
- server.ts
- index.ts
- main.ts
- manage.py
- CLI entry points
- desktop application entry points
- worker entry points
- scheduled jobs
- serverless functions

For each entry point explain:

1. How execution starts.
2. What gets initialized.
3. What configuration is loaded.
4. What dependencies are initialized.
5. What routes/modules/services are registered.
6. What middleware is applied.
7. What database connections are established.
8. What background processes start.
9. What happens during shutdown.

Trace the actual execution path.

==================================================
4. COMPLETE ARCHITECTURE
==================================================

Construct the architecture of the entire system.

Identify the major layers, for example:

Client
↓
UI
↓
API
↓
Middleware
↓
Authentication
↓
Authorization
↓
Routes/controllers
↓
Services/use-cases
↓
Repositories/data-access
↓
ORM
↓
Database

But DO NOT assume this exact architecture exists.

Determine the real architecture from the code.

For every layer explain:

- responsibility
- important files
- public interfaces
- dependencies
- callers
- dependencies it calls
- data it receives
- data it returns
- failure behavior

Identify violations of architectural boundaries.

==================================================
5. COMPLETE DATA FLOW
==================================================

Trace data through the entire system.

For each major user/system action, trace:

INPUT
↓
validation
↓
transformation
↓
authentication
↓
authorization
↓
routing
↓
controller/endpoint
↓
service/business logic
↓
repository/data access
↓
database/external service
↓
response transformation
↓
client/UI

Do this for ALL major workflows discovered in the repository.

For each workflow document:

- starting point
- input shape
- validation
- transformations
- business rules
- database operations
- external calls
- response shape
- error paths
- side effects
- logging
- transactions
- caching
- asynchronous behavior

==================================================
6. DATA MODEL
==================================================

Build a complete data model.

Identify every:

- database table
- model/entity
- schema
- DTO
- request model
- response model
- enum
- relationship
- foreign key
- unique constraint
- check constraint
- index
- trigger
- view
- stored procedure/function
- migration

For each important entity explain:

- purpose
- fields
- required/optional fields
- relationships
- lifecycle
- who creates it
- who updates it
- who deletes it
- who reads it

Trace data from API input all the way to persisted database records and back.

==================================================
7. DATABASE ARCHITECTURE
==================================================

Inspect:

- migrations
- schema definitions
- ORM models
- database configuration
- connection pooling
- transactions
- constraints
- indexes
- cascading behavior
- seed data
- initialization scripts

Determine whether the ORM/model layer and actual database schema agree.

Identify:

- missing migrations
- schema drift
- unused tables
- unused columns
- duplicate concepts
- dangerous relationships
- integrity risks

Do not modify anything.

==================================================
8. API SURFACE
==================================================

Find EVERY API endpoint.

For each endpoint document:

METHOD
PATH
AUTHENTICATION
AUTHORIZATION
REQUEST
VALIDATION
BUSINESS LOGIC
DATABASE OPERATIONS
EXTERNAL SERVICES
RESPONSE
ERRORS
SIDE EFFECTS

Also identify:

- undocumented endpoints
- deprecated endpoints
- duplicate endpoints
- internal endpoints
- admin endpoints
- public endpoints
- health endpoints
- debug endpoints

Trace each endpoint into the actual implementation.

==================================================
9. AUTHENTICATION & AUTHORIZATION
==================================================

Understand security completely.

Trace:

login
↓
credential validation
↓
token/session creation
↓
token/session storage
↓
request authentication
↓
identity extraction
↓
role/permission resolution
↓
authorization
↓
resource-level access checks

Identify:

- users
- roles
- permissions
- ownership rules
- admin privileges
- service accounts
- API keys
- JWT/session handling
- password handling
- refresh tokens
- expiration
- revocation
- rate limiting

Pay special attention to object-level authorization.

For every sensitive resource determine:

"How does the system prove that THIS user is allowed to access THIS specific record?"

Do not merely report that authentication exists.

==================================================
10. FRONTEND / CLIENT DATA FLOW
==================================================

If a frontend exists, trace it too.

For each major screen/page/feature determine:

UI
↓
state
↓
event
↓
API call
↓
request
↓
backend
↓
response
↓
state update
↓
UI rendering

Identify:

- API clients
- hooks
- stores
- context
- forms
- validation
- loading states
- error states
- optimistic updates
- caching
- authentication state
- route protection

Find frontend/backend contract mismatches.

==================================================
11. EXTERNAL SERVICES
==================================================

Identify every external dependency.

For each one explain:

- what it is used for
- where it is called
- what credentials/configuration it requires
- request format
- response format
- failure behavior
- retry behavior
- timeout behavior
- whether it is required for startup
- whether it is required for core functionality

Include:

- AI APIs
- payment systems
- email
- SMS
- storage
- authentication providers
- maps
- analytics
- third-party APIs

==================================================
12. CONFIGURATION & ENVIRONMENT
==================================================

Trace configuration from:

environment variables
↓
configuration loader
↓
application configuration
↓
runtime consumers

Identify:

- required variables
- optional variables
- defaults
- secrets
- development configuration
- testing configuration
- production configuration

Never expose secret values in the report.

Report variable NAMES only.

==================================================
13. ERROR HANDLING
==================================================

Map the error architecture.

Identify:

- validation errors
- authentication errors
- authorization errors
- database errors
- external API errors
- unexpected exceptions
- global exception handlers
- frontend error handling

Trace how an error travels from its origin to the user.

Identify places where errors are:

- swallowed
- incorrectly transformed
- leaked
- inconsistently formatted
- logged incorrectly

==================================================
14. LOGGING & OBSERVABILITY
==================================================

Identify:

- logging system
- log levels
- request logging
- error logging
- audit logging
- metrics
- tracing
- monitoring
- health checks

Determine whether important security/business events are observable.

==================================================
15. ASYNC / CONCURRENCY / BACKGROUND WORK
==================================================

Find:

- async functions
- workers
- queues
- scheduled jobs
- background tasks
- event handlers
- WebSockets
- subscriptions
- cron jobs

Trace their lifecycle and data flow.

Pay attention to:

- race conditions
- duplicate processing
- retries
- idempotency
- transaction boundaries

==================================================
16. TEST ARCHITECTURE
==================================================

Inspect ALL tests.

Determine:

- unit tests
- integration tests
- API tests
- database tests
- end-to-end tests
- frontend tests
- fixtures
- mocks
- test database
- test configuration

Map which production components are actually tested.

Identify important untested paths.

Do not write tests.

==================================================
17. BUILD / DEPLOYMENT PIPELINE
==================================================

Understand the entire pipeline from source code to running application.

Trace:

Developer change
↓
Git
↓
CI
↓
lint/type checking
↓
tests
↓
build
↓
artifact/container
↓
deployment
↓
environment configuration
↓
database migrations
↓
application startup
↓
health checks

Inspect:

- GitHub Actions/GitLab CI/etc.
- Dockerfiles
- docker-compose
- deployment configs
- build scripts
- release scripts
- migration commands
- startup scripts
- environment configuration

Document what actually happens, not what documentation claims happens.

==================================================
18. DEPENDENCY GRAPH
==================================================

Construct a high-level dependency graph.

Show:

- applications
- modules
- services
- major classes/functions
- databases
- external services

Identify:

- circular dependencies
- highly coupled modules
- isolated modules
- shared utilities
- dangerous dependency directions

==================================================
19. BUSINESS LOGIC
==================================================

Find the actual business rules hidden inside the implementation.

Document:

- validation rules
- state transitions
- lifecycle rules
- permissions
- calculations
- uniqueness rules
- required relationships
- conditional behavior
- edge cases

Separate:

FACTUAL IMPLEMENTATION
from
INTENDED/DOCUMENTED BEHAVIOR.

If documentation says one thing but code does another, report both.

==================================================
20. DEAD / DUPLICATE / SUSPICIOUS CODE
==================================================

Identify potential:

- dead code
- unused files
- unused functions
- duplicate implementations
- abandoned features
- legacy code
- conflicting implementations
- temporary hacks
- TODOs
- FIXME comments
- unreachable code

Do not delete anything.

Do not automatically classify something as dead unless evidence supports it.

Use confidence levels:

HIGH
MEDIUM
LOW

==================================================
21. CONTRACT CONSISTENCY
==================================================

Compare:

- frontend ↔ backend
- API schemas ↔ implementation
- ORM models ↔ database
- migrations ↔ current schema
- documentation ↔ implementation
- configuration ↔ actual consumers
- tests ↔ production behavior

Report mismatches explicitly.

==================================================
22. SECURITY REVIEW
==================================================

Perform a READ-ONLY security architecture review.

Look for:

- broken authentication
- broken authorization
- object-level authorization gaps
- privilege escalation paths
- insecure defaults
- secret exposure
- unsafe input handling
- injection risks
- insecure file handling
- unsafe debug endpoints
- excessive permissions
- insecure CORS
- weak session/token handling
- missing rate limits
- sensitive data exposure

Do NOT exploit vulnerabilities.

Do NOT modify anything.

Only document evidence and affected code paths.

==================================================
23. FULL SYSTEM PIPELINES
==================================================

For every major feature, create a pipeline like:

USER ACTION
    ↓
UI COMPONENT
    ↓
CLIENT STATE
    ↓
HTTP REQUEST
    ↓
API ROUTE
    ↓
MIDDLEWARE
    ↓
AUTHENTICATION
    ↓
AUTHORIZATION
    ↓
VALIDATION
    ↓
SERVICE
    ↓
REPOSITORY
    ↓
DATABASE
    ↓
RESPONSE
    ↓
CLIENT STATE
    ↓
UI

Adapt this to the actual architecture.

Include alternate/error paths where important.

==================================================
24. MASTER SYSTEM MAP
==================================================

At the end, create one consolidated system map showing:

APPLICATIONS
    ↓
ENTRY POINTS
    ↓
MODULES
    ↓
API
    ↓
AUTH
    ↓
BUSINESS LOGIC
    ↓
DATA ACCESS
    ↓
DATABASE
    ↓
EXTERNAL SERVICES

Also show:

- frontend → backend
- backend → database
- backend → external APIs
- background workers
- CI/CD
- deployment
- authentication boundaries
- trust boundaries

==================================================
25. FINDINGS
==================================================

Categorize findings into:

A. VERIFIED ARCHITECTURE
B. VERIFIED DATA FLOWS
C. VERIFIED BUSINESS RULES
D. VERIFIED SECURITY CONTROLS
E. CONTRACTS
F. ARCHITECTURAL RISKS
G. SECURITY RISKS
H. DATA INTEGRITY RISKS
I. PERFORMANCE RISKS
J. MAINTAINABILITY RISKS
K. DEAD/DUPLICATE CODE
L. DOCUMENTATION MISMATCHES
M. UNKNOWN / UNVERIFIED AREAS

Do not rank political opinions, obviously. This is code, thankfully.

For every finding include:

- evidence
- file path
- relevant symbol/function/class
- what happens
- why it matters
- confidence level

==================================================
26. FINAL OUTPUT FORMAT
==================================================

Produce the final report in this exact structure:

# 1. Executive System Summary

# 2. Repository Structure

# 3. Technology Stack

# 4. Runtime Entry Points

# 5. Complete Architecture

# 6. Module Map

# 7. Complete Data Model

# 8. Database Architecture

# 9. API Map

# 10. Authentication & Authorization

# 11. Frontend / Client Architecture

# 12. External Services

# 13. Configuration & Environment

# 14. Major Feature Data Flows

# 15. Business Rules

# 16. Error Handling

# 17. Async / Background Processing

# 18. Logging & Observability

# 19. Testing Architecture

# 20. CI/CD & Deployment Pipeline

# 21. Dependency Graph

# 22. Security Architecture Review

# 23. Contract Consistency Review

# 24. Dead / Duplicate / Legacy Code

# 25. Documentation vs Implementation

# 26. Risks & Findings

# 27. Unknown / Unverified Areas

# 28. MASTER SYSTEM MAP

# 29. MASTER DATAFLOW

# 30. MASTER EXECUTION PIPELINE

# 31. Recommended Reading Order for Future Development

==================================================
27. EVIDENCE RULE
==================================================

Every important conclusion must be traceable to actual code.

Use:

FILE → SYMBOL → BEHAVIOR → CONSEQUENCE

Example:

backend/app/api/v1/endpoints/registrations.py
→ create_student()
→ calls StudentRegistrationService.register()
→ creates user + student + enrollment in one transaction
→ returns StudentRegistrationResponse

Do NOT write vague statements such as:

"the backend handles registration."

Explain HOW it handles registration.

==================================================
28. IMPORTANT OPERATING RULE
==================================================

DO NOT START IMPLEMENTING.

The purpose of this task is to create an accurate architectural understanding that another developer/agent can rely on before making changes.

If the repository is too large to inspect in one pass:

1. Continue systematically.
2. Track what has already been inspected.
3. Do not pretend the entire repository was understood.
4. Clearly identify remaining uninspected areas.
5. Continue until all relevant runtime code has been examined.

At the end, explicitly state:

- what you inspected
- what you verified
- what remains unknown
- what assumptions, if any, remain
- which files/modules are most important for future implementation

The final report should be detailed enough that a developer who has never seen this repository can understand its architecture, execution paths, data flows, dependencies, and boundaries without opening every file themselves. ==================================================
SUPERTEACHER BACKEND SPECIAL REQUIREMENT
==================================================

Treat backend/ as the complete backend boundary.

Do not assume that Student Registration is the only thing that exists merely because it is the current development focus.

First understand the ENTIRE backend architecture, including:

- application startup
- configuration
- database
- migrations
- models
- schemas
- repositories
- services
- API routers
- middleware
- authentication
- authorization
- rate limiting
- security dependencies
- error handling
- logging
- health checks
- development guards
- tests
- scripts
- verification utilities
- database relationships
- existing endpoints
- existing contracts

Then identify Student Registration as the currently active feature and trace its complete implementation through the entire backend.

Specifically trace:

Student registration request
→ request schema
→ validation
→ authentication
→ authorization
→ registration endpoint
→ service/business logic
→ transaction boundary
→ user creation
→ student creation
→ academic/pathway/school relationships
→ enrollment creation
→ subject/program relationships where applicable
→ database constraints
→ response schema
→ API response

For Rwanda education logic, identify where the system represents:

- Ordinary Level S1–S3
- Advanced Level S4–S6
- TVET L3–L5
- schools
- pathways
- education levels
- programs
- program versions
- subjects
- enrollments
- academic years

Do not assume these relationships are correct. Verify them against the actual schema, migrations, models, services, and endpoints.

Also identify which parts of the backend are:

ACTIVE
INCOMPLETE
PLACEHOLDER
LEGACY
UNUSED
UNVERIFIED

Do not modify any code during this audit.

---

## 2. Assistant

> *Auto-generated session summary. Earlier turns of this conversation were compacted by the tool and no longer exist in the session store - the earlier progress-question exchange and its reply were lost to this compaction.*

## Objective
- Perform a FULL READ-ONLY codebase understanding + system architecture audit of the repository (no modifications, no installs, no fixes), producing a 31-section report; special emphasis on `backend/` as the complete backend boundary and tracing the Student Registration feature end-to-end.

## Important Details
- READ-ONLY mandate: no file changes, no dependency installs, no destructive commands, no implementation — understanding only; mark anything unverified as UNKNOWN.
- Final deliverable must follow the exact 31-section report format specified by the user (sections 1–31), with evidence rule FILE → SYMBOL → BEHAVIOR → CONSEQUENCE.
- Repo contains THREE products in one directory: (1) SuperTeacher desktop "RINA" (Python/PyQt6/Gemini, `main.py` → `ui.JarvisUI`), (2) React/Vite "letters" web app + Student Portal (root `package.json` name=`letters`), (3) FastAPI backend `backend/` ("SuperTeacher API").
- Git: HEAD `dc8b91c`; 134 uncommitted paths; `backend/` entirely UNTRACKED (0 tracked files); student portal frontend files (`src/components/student/`, `src/lib/api.js`, `src/services/*.js`) also untracked; `src/App.jsx` and `.env.example` modified.
- Backend: FastAPI 0.121.2, SQLAlchemy 2.0.44, Alembic 1.17.0, psycopg3, Argon2id (argon2-cffi), PyJWT HS256, slowapi rate limiting, pydantic v2, pydantic-settings, PostgreSQL; dev deps pytest/pytest-cov/httpx. `resend` package is in requirements.txt but NOT installed in `.venv` (verified `Test-Path ... site-packages\resend` → False).
- No Dockerfile, no docker-compose, no CI/CD anywhere; no pyproject/setup.py/Makefile in backend.
- Auth: Bearer JWT (access + revocable refresh sessions in `auth_sessions` via SHA-256 digest, rotation on refresh); roles = student/teacher/admin (`UserRole`); DB is authority for role/status, not JWT claims; 401 generic for bad credentials, 403 for authorized-but-forbidden.
- Error hierarchy: `ProfileError` (400 base; Validation 422, Conflict 409, NotFound 404, Forbidden 403) mapped by `@app.exception_handler(ProfileError)` in `backend/app/main.py`; `RegistrationError` subclasses mapped in registrations endpoint (Validation 422, Conflict 409, NotFound 404, Unavailable 503).
- CONFIRMED CONTRACT MISMATCH (HIGH): `RegisterWizard.jsx` sends `programVersion?.id` where `programVersion` is a `SchoolProgramRead` (id = `school_programs.id`), but backend `_resolve_program_version` does `session.get(ProgramVersion, id)` → 404.
- CONFIRMED: seed datasets for `schools`, `programs`, `program_versions`, `school_programs` are EMPTY → `/registrations/readiness` returns ready=False; registration wizard cannot complete (schools list empty).
- Wizard hardcodes `pathwayCode = "O_LEVEL"` — no pathway selection offered.
- Stale bytecode: `backend/app/api/v1/endpoints/__pycache__/students.cpython-314.pyc` exists with no `students.py` source (deleted module); root `__pycache__` has `gen_reb_content`, `or_client`, `setup` pyc without sources.
- README says "19 tables" but 20 exist (`auth_events` unlisted) and claims datasets "intentionally EMPTY" while 40 rows exist — stale docs.
- `.env` key NAMES verified only (values never to be exposed): backend `.env.example` 24 keys incl. `DB_*`, `SECRET_KEY`, `PASSWORD_MIN_LENGTH`, `ACCESS_TOKEN_EXPIRE_MINUTES`, `REFRESH_TOKEN_EXPIRE_DAYS`, `MAX_SESSIONS_PER_USER`, `CORS_ORIGINS`, `RATE_LIMIT_*`, `DB_POOL_*`, `RESEND_API_KEY`, `RESEND_FROM_EMAIL`, `FRONTEND_URL`, `ENVIRONMENT`; `backend/.gitignore` ignores `.env`. Root `.env.example` has `VITE_FIREBASE_*` + `VITE_API_URL=http://127.0.0.1:8000/api/v1`.
- Frontend API base: `src/lib/api.js` → `import.meta.env.VITE_API_URL || "http://127.0.0.1:8000/api/v1"`; tokens in localStorage keys `st_access_token` / `st_refresh_token`.
- Rwandan education model encoded as DATA (rows), not logic: O_LEVEL→S1,S2,S3; A_LEVEL→S4,S5,S6; TVET→L3,L4,L5 via `pathway_levels`; seeded in `backend/app/data/*` and `backend/scripts/seed_reference_data.py`.
- Three explore subagents already completed: (A) frontend/root tooling audit, (B) backend migrations/scripts/tests/tooling audit, (C) backend `app/data` seed datasets audit, (D) backend auth/profile services audit.

## Work State
### Completed
- Repo inventory (root dir listing, git log/status, backend file tree).
- Read backend entry/config/core: `backend/app/main.py`, `app/core/config.py`, `app/core/database.py`, `app/core/security.py`, `app/core/auth_dependencies.py`, `app/core/dev_guard.py`, `app/core/rate_limit.py`, `app/core/time_mixin.py`.
- Read API layer: `app/api/v1/router.py`, `endpoints/auth.py`, `endpoints/registrations.py`, `endpoints/health.py`, `endpoints/catalog.py`, `endpoints/admin_students.py`.
- Read services: `registration_service.py`, `auth_service.py`, `catalog_service.py`.
- Read ALL models: `enums.py`, `user.py`, `student.py`, `enrollment.py`, `academic_year.py`, `education_level.py`, `pathway.py`, `pathway_level.py`, `program.py`, `program_version.py`, `program_subject.py`, `subject.py`, `school.py`, `school_program.py`, `student_subject.py`, `tvet_program.py`, `auth_session.py` (+ `student_profile_history`, `auth_event`, `password_reset_token`, `tvet_sector` via subagent).
- Read schemas: `schemas/registration.py`, `schemas/catalog.py`; repository `enrollment_repository.py`.
- Read frontend `src/components/student/RegisterWizard.jsx`, `src/lib/api.js`; verified env files.
- Subagent reports captured: migrations 0001–0005 + alembic/env.py + schema drift (naming_convention not passed to `context.configure`), tests structure, scripts (`cleanup_auth_sessions.py`, `seed_reference_data.py`, `verify_database.py`, `schema_reset.sql`); seed datasets (12 dataset modules, `loader.py`/`registry.py`/`validation.py`/`_spec.py`); student_service.py/auth schemas/repositories/core.email.py/dead-code greps.
- Verified stale `students.pyc`, `resend` absent, git tracking status, README headings + endpoint list (lines 160–360), root/backend `.env.example` key names.

### Active
- Assembling findings into the final 31-section report; a few backend source files still unread directly (read only via subagent summaries): `backend/README.md` lines 1–159 and 360–454, `app/repositories/catalog_repository.py`, `app/schemas/auth.py`/`student_profile.py` (covered by subagent), remaining portions of `registration_service.py`, `auth.py`, `registrations.py`, `catalog_service.py` (all truncated reads).

### Blocked
- (none)

## Next Move
1. Finish remaining verifications: read `src/services/{auth,catalog,registrations}.js` and `src/components/student/*` for full frontend flow, skim `backend/README.md` remainder and `docs/system-analysis/*` for doc-vs-implementation mismatches, and confirm the full endpoint list from `registrations.py`/`auth.py` truncations.
2. Write the complete 31-section final report (Executive Summary → … → Recommended Reading Order), including MASTER SYSTEM MAP / DATAFLOW / EXECUTION PIPELINE, categorized findings A–M with evidence + confidence, and an explicit list of what was inspected, verified, unknown, assumptions, and most important files for future work.

## Relevant Files
- `backend/app/main.py`: FastAPI entry; CORS, HTTPBearer OpenAPI, exception handlers (`ProfileError`), rate limiter wiring, router registration.
- `backend/app/core/config.py`: `Settings`, `database_url` builder, `_DEVELOPMENT_STAGE_ENVIRONMENTS`, `_UNSAFE_SECRETS`.
- `backend/app/core/database.py`: `Base` + naming convention, engine pool settings, `SessionLocal`, `get_db`.
- `backend/app/core/security.py`: Argon2id hashing, JWT create/decode (`access`/`refresh` token types).
- `backend/app/core/auth_dependencies.py`: `get_current_user`, `get_current_student`, `require_admin` chain.
- `backend/app/api/v1/router.py`: aggregation of health/auth/me/student_me/catalog/admin_students/registrations routers.
- `backend/app/api/v1/endpoints/registrations.py`: public readiness + `/me/registrations` + `/admin/registrations` (role-split).
- `backend/app/api/v1/endpoints/admin_students.py`: `/admin/students` CRUD + history.
- `backend/app/services/registration_service.py`: authoritative enrollment business rules, flush-only transaction convention.
- `backend/app/services/auth_service.py`: account creation, login, refresh rotation, logout/revocation.
- `backend/app/services/catalog_service.py` + `repositories/catalog_repository.py`: catalog read rules, `CatalogNotFoundError`.
- `backend/app/models/*`: full data model (20 tables incl. `auth_events`).
- `backend/app/data/*`: 12 seed datasets encoding Rwanda O_LEVEL/A_LEVEL/TVET structure.
- `backend/alembic/versions/0001..0005`: schema history; `alembic/env.py` URL from settings.
- `backend/scripts/seed_reference_data.py`, `verify_database.py`, `schema_reset.sql`, `cleanup_auth_sessions.py`: tooling.
- `backend/tests/`: pytest suite (unit + integration) — coverage map from subagent report.
- `backend/README.md`: primary backend documentation (some stale claims).
- `src/lib/api.js`: frontend API client, token storage, `friendlyError`.
- `src/components/student/RegisterWizard.jsx`: registration wizard — source of programVersion id mismatch.
- `src/services/{auth,catalog,registrations}.js`: frontend service layer (still to read directly).
- `docs/system-analysis/*`: prior analysis docs (likely stale vs current code).

---

## 3. You

> *Auto-injected continuation prompt (not typed by the user).*

Continue if you have next steps, or stop and ask for clarification if you are unsure how to proceed.

---

## 4. Assistant

Recovered the original 31-section spec. Finishing the last verifications.

---

## 5. Assistant

All verification complete. Here is the final report.

---

# 1. Executive System Summary

This directory is **one git repository containing three unrelated products**, not one application.

| # | Product | Stack | Entry point | Tracked? |
|---|---|---|---|---|
| 1 | **SuperTeacher desktop "RINA"** — AI tutor/lesson browser | Python 3.14, PyQt6, `google-genai` (Gemini Live), sounddevice | `main.py` → `from ui import JarvisUI` | Yes (2707 tracked files) |
| 2 | **"Letters" web app + Student Portal** — React/Vite map experience, admin console, student registration UI | React 18, Vite, Tailwind, Firebase (Firestore/Storage) + FastAPI client | `src/main.jsx` → `src/App.jsx` | `src/App.jsx` **modified**; `src/components/student/*`, `src/lib/api.js`, `src/services/{auth,catalog,registrations}.js` **untracked** |
| 3 | **SuperTeacher API** — FastAPI backend for accounts, profiles, catalog, enrollment | FastAPI, SQLAlchemy 2, Alembic, PostgreSQL/psycopg3, Argon2id, PyJWT, slowapi | `backend/app/main.py` → `app` | **Entirely untracked (0 files)** |

**Backend is the intended future boundary.** `backend/README.md:5-8` states it "serves the SuperTeacher project as a whole" and the Student Registration Portal "is its first module". It is architecturally the cleanest of the three (layered, tested, migrated, rate-limited) but is the *least* wired into the repository: no CI, no Docker, not committed, no root README mention, no run script.

**The one active feature is Student Registration**, and in the *seeded* state of the database **it cannot complete end-to-end**:

1. `backend/app/data/schools.py`, `programs.py`, `program_versions.py`, `school_programs.py` are empty → `GET /api/v1/registrations/readiness` returns `ready=False` (blocked at `src/components/student/RegisterWizard.jsx:132`, which renders `UnavailableNotice` and stops).
2. The single seeded academic year `2025/2026` has `status="closed"` → `registration_service._validate_year_registrable` would raise 503 even if the UI were bypassed (`registration_service.py:220-230`, `REGISTRABLE_YEAR_STATUSES`).
3. Even with a populated catalog, `RegisterWizard.jsx:112` sends `program_version_id: programVersion?.id` where `programVersion` is a `SchoolProgramRead` (`id` = `school_programs.id`), while `registration_service._resolve_program_version` does `session.get(ProgramVersion, program_version_id)` (`registration_service.py:233-250`) → **404**.

**Verified architectural quality (high confidence):** identity is never taken from the request body on student routes; role/status are re-read from the DB per request; refresh tokens are revocable and rotate; Argon2id hashing; explicit exception→status mapping; 383 tests (252 unit / 131 integration); 5 linear Alembic migrations; per-endpoint rate limits on 24 of 35 routes.

**Verified gaps (high confidence):** `resend` is declared in `backend/requirements.txt` but **absent from `.venv`** → password-reset email silently never sends (caught + logged, still 204); `POST /admin/students` creates users with `password_hash=None`; rate limiter keys on `request.client.host` with no proxy-header trust → behind nginx all users share one bucket; no CI/CD, no container config, no lint/typecheck anywhere; `backend/` untracked → a fresh clone has no backend at all.

---

# 2. Repository Structure

```
/                                   (git repo, HEAD dc8b91c, 2707 tracked files, 134 dirty paths)
├─ main.py                 43 KB   ENTRY — desktop RINA (asyncio + Gemini Live)      [tracked]
├─ ui.py                  222 KB   PyQt6 god-file, class JarvisUI (ui.py:4489)       [tracked]
├─ index.html, package.json, vite.config.js, tailwind.config.js, postcss.config.js   [tracked]
├─ requirements.txt                 desktop deps (PyQt6, google-genai, …)            [tracked]
├─ firebase.rules, storage.rules    Firestore/Storage security rules                 [tracked]
├─ readme.md, DEPLOYMENT.md         desktop + Letters docs                           [tracked]
├─ .env.example                     VITE_FIREBASE_*, VITE_API_URL                    [modified]
├─ .gitignore                       ignores .env, __pycache__, node_modules, dist    [tracked]
├─ dev-server.log, dev-server.err.log  committed Vite logs (leak local user path)    [tracked]
│
├─ core/           9 files         desktop curriculum loader, student mode, tools    [tracked]
├─ memory/         3 files         long-term memory; memory_manager.py imports
│                                  missing `or_client` → extraction fails at runtime [tracked]
├─ data/        2265 files         curriculum trees, official books, lessons         [tracked]
├─ assets/        359 files        images/diagrams                                   [tracked]
├─ scripts/        15 files        desktop one-shot data generators                  [tracked]
├─ config/          4 files        api_keys.json / accounts.json templates            [tracked]
├─ public/          8 files        Vite static assets                                [tracked]
├─ docs/           16 files        docs/system-analysis/* (16 prior-audit markdowns)  [tracked]
│
├─ src/
│  ├─ main.jsx, App.jsx, styles.css                       App.jsx MODIFIED
│  ├─ components/  PublicMapExperience, MapCanvas, LetterOverlay, Journal,
│  │               AmbientPlayer, AdminWorkspace          [tracked]
│  ├─ components/student/  StudentPortal, AuthScreens,
│  │               RegistrationFlow, RegisterWizard, StudentUI   [UNTRACKED]
│  ├─ services/    auth.js, catalog.js, registrations.js  [UNTRACKED]
│  │               letters.js                             [tracked]
│  ├─ lib/         api.js [UNTRACKED]  firebase.js [tracked]
│  ├─ data/seedLetters.js, utils/mapProjection.js         [tracked]
│
├─ backend/                     ★ ENTIRELY UNTRACKED ★
│  ├─ app/
│  │  ├─ main.py                 FastAPI app factory (module level)
│  │  ├─ api/v1/router.py        aggregates 9 routers, prefix /api/v1
│  │  ├─ api/v1/endpoints/       auth, catalog, health, registrations, admin_students
│  │  ├─ core/                   config, database, security, auth_dependencies,
│  │  │                          rate_limit, dev_guard, email, time_mixin
│  │  ├─ models/                 20 SQLAlchemy models + enums
│  │  ├─ schemas/                auth, student_profile, catalog, registration
│  │  ├─ repositories/           7 repos (queries + flush only)
│  │  ├─ services/               4 services (business rules, flush only)
│  │  └─ data/                   12 seed dataset modules + loader/registry/validation
│  ├─ alembic/{env.py, versions/0001..0005}
│  ├─ scripts/                   seed_reference_data, verify_database,
│  │                             cleanup_auth_sessions, schema_reset.sql
│  ├─ tests/{conftest.py, unit/ (12 files), integration/ (9 files)}
│  ├─ .env, .env.example, .gitignore, alembic.ini, pytest.ini
│  ├─ README.md (560 lines), requirements.txt, requirements-dev.txt
│  └─ .venv/                     Python 3.14.2 (resend NOT installed)
│
├─ actions/, agent/, tmp/, .agents/          non-source residue (pyc-only / empty)
├─ dist/                                     built SPA — API base baked in at build time
├─ rahura superteacher L 2.0/                dist/ + node_modules/ only (no source)
└─ __pycache__/                              or_client, gen_reb_content, setup — no sources
```

**Ownership / classification**

| Path | Kind | Owner | Depends on |
|---|---|---|---|
| `main.py`, `ui.py`, `core/`, `memory/`, `data/`, `assets/`, `scripts/` | runtime (desktop) | desktop app | Gemini API, filesystem, PyQt6 |
| `index.html`, `src/` (excl. `student/`, `lib/api.js`) | runtime (web) | Letters app | Firebase |
| `src/components/student/`, `src/services/{auth,catalog,registrations}.js`, `src/lib/api.js` | runtime (web) | Student Portal | `backend/` HTTP API |
| `backend/app/`, `backend/alembic/` | runtime (server) | SuperTeacher API | PostgreSQL |
| `backend/scripts/` | tooling | DB operators | PostgreSQL |
| `backend/tests/` | tests | backend | SQLite (unit) / PostgreSQL (integration) |
| `docs/system-analysis/` | documentation | prior audit (2026-09-10) | **stale** — predates `backend/` |
| `dist/`, `__pycache__/`, `backend/.venv/`, `backend/**/__pycache__/` | generated | build/runtime | — |
| `actions/`, `agent/`, `tmp/`, `.agents/`, `rahura … L 2.0/` | dead / residue | unknown | — |

---

# 3. Technology Stack

Verified from manifests and imports, not documentation.

**Desktop (product 1)** — `requirements.txt`, `main.py:1-22`
- Language/runtime: Python 3.14 (`backend/.venv` is 3.14.2; `.pyc` tags are `cpython-314`)
- GUI: PyQt6 (`ui.py:32`)
- AI: `google-genai` (Gemini Live), OpenRouter via missing `or_client`
- Audio: `sounddevice`; PDF: PyMuPDF; images: `pillow`
- Concurrency: `asyncio` task group + `threading` for memory extraction
- Logging: stdlib `logging.basicConfig(level=INFO)` → `logger = logging.getLogger("RINA")` (`main.py:10-15`)
- Package manager: `pip` + `requirements.txt` (**unpinned**)
- No tests, no lint config, no formatter config

**Web (product 2)** — `package.json`, `vite.config.js`
- React 18.3.1, Vite 6, `@vitejs/plugin-react`, Tailwind 3.4, `postcss`, `autoprefixer`
- HTTP: native `fetch` wrapped by `src/lib/api.js`
- Auth store: `localStorage` (`st_access_token`, `st_refresh_token`)
- External: Firebase (Firestore + Storage) for Letters; `lucide-react` icons
- State: local `useState`/`useEffect` only — no Redux/Zustand/React Query
- Tests: **none**. Lint/format: **none**

**Backend (product 3)** — `backend/requirements.txt`, `requirements-dev.txt`, `.venv`

| Concern | Technology | Verified version |
|---|---|---|
| Web framework | FastAPI | 0.121.2 |
| ASGI server | Uvicorn | in `.venv` |
| Validation/settings | Pydantic v2 + pydantic-settings | 2.12.5 |
| ORM | SQLAlchemy 2.0 (typed `Mapped[]`) | 2.0.44 |
| Migrations | Alembic | 1.17.0 |
| Database | PostgreSQL via **psycopg 3** | 3.2.13 |
| Password hashing | **Argon2id** (`argon2-cffi`) | in `.venv` |
| JWT | PyJWT, **HS256** | in `.venv` |
| Rate limiting | **slowapi** (in-memory storage) | in `.venv` |
| Email | `resend` | **listed, NOT installed** |
| Testing | pytest + pytest-cov + httpx | 8.4.2 / dev only |
| Typing/lint | — | **none** |

**Infrastructure** — none. `Get-ChildItem -Recurse -Include *.yml,*.yaml,Dockerfile,docker-compose*,Jenkinsfile` → **0 hits**; `.github/` → `False`.

---

# 4. Runtime Entry Points

### 4.1 `backend/app/main.py` → `app` (server) — **primary**
1. **Starts:** `python -m uvicorn app.main:app --reload --port 8000` from `backend/` (`README.md:508`).
2. **Config:** `settings = get_settings()` at module import — `pydantic-settings` reads `backend/.env` **relative to CWD**.
3. **`_is_production`** gates: OpenAPI/Redoc/docs disabled in production.
4. **`app.state.limiter = limiter`** (slowapi, memory storage) + `RateLimitExceeded` → 429 JSON handler.
5. **Exception handlers:** `ProfileError` (MRO → 404/409/422/403), plus a catch-all `Exception` handler → `{"detail":"Internal server error"}` + `logger.exception`.
6. **Middleware:** `_security_headers` (nosniff, `X-Frame-Options: DENY`, `Referrer-Policy`, `Permissions-Policy`, HSTS only in production) then `CORSMiddleware` (explicit origin list, `allow_credentials=False`, methods `GET/POST/PATCH`, headers `Content-Type/Accept/Authorization`).
7. **OpenAPI:** custom `custom_openapi` caches a schema with an `HTTPBearer` security scheme.
8. **Routers:** `app.include_router(api_router, prefix="/api/v1")`.
9. **DB:** the engine is created at **import time** by `app/core/database.py` (module-level `_settings` + `create_engine`) — lazy connect, `pool_pre_ping=True`, `pool_size=5`, `max_overflow=10`, `pool_recycle=1800`.
10. **Startup/shutdown:** **no `lifespan`, no `@app.on_event`, no `create_all`, no seeding.** Nothing runs at boot. `get_db` closes each session per request.
11. **Background processes:** none.

### 4.2 `main.py` (desktop)
`logging.basicConfig` → `from ui import JarvisUI` → `from core.curriculum_loader import get_lesson_context`, `core.student_mode`, `memory.memory_manager` → `sounddevice`, `google.genai`. Initializes audio, the PyQt6 `QApplication` (`ui.py:4491`: `self._app = QApplication.instance() or QApplication(sys.argv)`), the Gemini Live session, and an asyncio task group with an **infinite fixed-interval reconnect loop**. Shutdown: interpreter exit; no explicit teardown.

### 4.3 `src/main.jsx` → `src/App.jsx` (SPA)
Vite dev (`npm run dev`) or static serve of `dist/`. `App.jsx` does **pathname-based routing with no router library**:
```
route = window.location.pathname.replace(/\/+$/,"") || "/"
"/register" → <StudentPortal/>
"/admin"    → <AdminWorkspace/>
default     → <PublicMapExperience/>
```

### 4.4 CLI / tooling
- `backend/scripts/seed_reference_data.py` (`--dry-run`), `verify_database.py` (`--live`), `cleanup_auth_sessions.py`, `schema_reset.sql`
- `alembic upgrade head` (`alembic/env.py` builds URL from `get_settings()`)
- root `scripts/*.py` (15 desktop content generators)
- **No** `pyproject.toml`, `setup.py`, `Makefile`, `manage.py`, worker entry point, cron, or serverless function.

---

# 5. Complete Architecture

Three independent applications sharing only a git directory. There is **no shared library, no monorepo workspace, no common build**.

```
┌──────────────────────────────────────┐   ┌──────────────────────────────────────┐
│ PRODUCT 1 — Desktop "RINA"          │   │ PRODUCT 2 — Letters + Student Portal │
│ main.py → ui.JarvisUI (PyQt6)       │   │ React/Vite SPA                       │
│ core/curriculum_loader              │   │  ├─ PublicMapExperience (Firebase)    │
│ memory/memory_manager               │   │  ├─ AdminWorkspace   (Firebase auth)  │
│ Gemini Live  ←→  OpenRouter (broken)│   │  └─ /register → StudentPortal (JWT)   │
│ local JSON: memory/, config/        │   │       localStorage tokens             │
└──────────────────────────────────────┘   └────────────────┬─────────────────────┘
                                                            │ fetch  (VITE_API_URL)
                                                            │ Bearer access_token
                       ┌────────────────────────────────────▼─────────────────────┐
                       │ PRODUCT 3 — SuperTeacher API  (backend/)                │
                       │  middleware: security_headers → CORS → limiter           │
                       │  /api/v1 ─ auth | me | me/student | catalog              │
                       │            registrations | admin/*                       │
                       │  endpoints ──▶ services ──▶ repositories ──▶ models      │
                       │  (commit)      (flush)       (flush)         (ORM)       │
                       │  auth: get_current_user → DB-authoritative role/status   │
                       └────────────────────────┬─────────────────────────────────┘
                                                │ psycopg 3
                                          ┌─────▼──────┐
                                          │ PostgreSQL │  20 tables, Alembic 0005
                                          └────────────┘
                       external: resend (NOT INSTALLED)  ·  Gemini (desktop only)
                       CI/CD: NONE   ·   containers: NONE
```

**Layer contract (backend only, and strictly enforced):**

| Layer | Files | Owns | Must NOT do |
|---|---|---|---|
| Endpoint | `app/api/v1/endpoints/*.py` | HTTP verbs/status, `session.commit()`/`rollback()`, exception→`HTTPException` | business rules, SQL |
| Service | `app/services/*.py` | cross-entity rules, raise `*Error` | `commit()`, HTTP |
| Repository | `app/repositories/*.py` | SELECT/INSERT, joins, ordering, `flush()` | `commit()`, business rules |
| Model | `app/models/*.py` | tables, FKs, CHECKs | — |
| Schema | `app/schemas/*.py` | request/response shape + field validators | DB access |

**Trust boundaries:** (1) browser ↔ API — CORS + Bearer only, body never supplies identity; (2) API ↔ PostgreSQL — credentials in `backend/.env`; (3) desktop ↔ Gemini/OpenRouter — API keys in `config/api_keys.json`; (4) web ↔ Firebase — `firebase.rules` / `storage.rules`.

---

# 6. Module Map

### `backend/app/` (the complete backend boundary)

| Module | Symbol | Responsibility | Status |
|---|---|---|---|
| `main.py` | `app`, `_security_headers`, `custom_openapi` | app assembly, middleware, handlers | ACTIVE |
| `core/config.py` | `Settings`, `get_settings()`, `_DEVELOPMENT_STAGE_ENVIRONMENTS`, `_UNSAFE_SECRETS` | 24 env keys, prod secret guard | ACTIVE |
| `core/database.py` | `Base`, `engine`, `SessionLocal`, `get_db` | naming-convention `Base`, pool | ACTIVE |
| `core/security.py` | `hash_password`, `verify_password`, `create_token`, `decode_token`, `validate_password_policy`, `PasswordPolicyError` | Argon2id + HS256 JWT (`typ` ∈ `access`/`refresh`) | ACTIVE |
| `core/auth_dependencies.py` | `get_current_user`, `get_current_student`, `require_admin`, `require_student`, `require_teacher` | Bearer→User, DB-authoritative role/status | ACTIVE |
| `core/rate_limit.py` | `limiter`, `key_func` | slowapi, keyed on client host | ACTIVE (risk: no proxy trust) |
| `core/dev_guard.py` | `require_development_stage` | blocks dangerous ops outside dev | **UNUSED** (no non-test callers) |
| `core/email.py` | `send_reset_email` | lazy `import resend` inside try/except | **DEGRADED** (`resend` absent) |
| `core/time_mixin.py` | `TimestampMixin` | `created_at`/`updated_at` | ACTIVE |
| `api/v1/router.py` | `api_router` | includes 9 sub-routers | ACTIVE |
| `endpoints/health.py` | `health` | `GET /health`, **no DB ping** | ACTIVE |
| `endpoints/auth.py` | `router`, `me_router`, `student_me_router` | 13 routes | ACTIVE |
| `endpoints/catalog.py` | `router` | 10 public read routes | ACTIVE |
| `endpoints/registrations.py` | `router`, `me_router`, `admin_router` | 7 routes, owns `RegistrationError` mapping | ACTIVE |
| `endpoints/admin_students.py` | `router` | 4 admin routes | ACTIVE |
| `services/auth_service.py` | `register_student_account`, `authenticate`, `rotate_refresh`, `revoke`, `forgot_password`, `reset_password`, `enforce_session_limit` | 23 KB, the largest service | ACTIVE |
| `services/registration_service.py` | `register_student` + 8 `_resolve_*`/`_validate_*` helpers, `commit()` | enrollment rules | ACTIVE; **`commit()` unused** |
| `services/student_service.py` | `create_student_profile`, `update_student_profile`, `load_student_for_user`, `get_student_profile` | profile CRUD + history | ACTIVE; **2 funcs dead/test-only** |
| `services/catalog_service.py` | `list_*`, `list_school_programs_for_school` | translates `CatalogNotFoundError` → 404 | ACTIVE |
| `repositories/` (7) | `user`, `student`, `enrollment`, `catalog`, `auth_session`, `auth_event`, `student_profile_history` | queries + `flush()` only | ACTIVE |
| `models/` (20) | see §7 | ORM | ACTIVE |
| `schemas/` (4) | `auth`, `student_profile`, `catalog`, `registration` | Pydantic v2 | ACTIVE |
| `data/` (12 + 4 infra) | `loader`, `registry`, `validation`, `_spec` | seed datasets | **runtime-unused** — only `scripts/` and `tests/` import them |

### Frontend modules
| Module | Exports | Used by |
|---|---|---|
| `src/lib/api.js` | `apiFetch`, `saveTokens`, `clearTokens`, `getRefreshToken`, `isLoggedIn`, `friendlyError` | all services + `StudentPortal` |
| `src/services/auth.js` | `registerAccount`, `login`, `logout`, `getMe` | `AuthScreens`, `StudentPortal` |
| `src/services/catalog.js` | `getReadiness`, `getAcademicYears`, `getPathways`, `getLevelsForPathway`, `getSchools`, `getSchoolOfferings` | `RegisterWizard`; **`getAcademicYears`/`getPathways` unused** |
| `src/services/registrations.js` | `createRegistration`, `listMyRegistrations`, `getRegistration` | `RegisterWizard`, `StudentPortal`; **`getRegistration` unused** |
| `src/services/letters.js` | Firestore CRUD | `LetterOverlay`, `MapCanvas` |

### Desktop modules
`core/curriculum_loader.py` (lazy tree + LRU, path-traversal hardened), `core/curriculum_nodes.py`, `core/student_mode.py`, `memory/memory_manager.py` (imports **missing** `or_client`).

---

# 7. Complete Data Model

**20 tables.** Verified by 20 model imports in `app/models/__init__.py:8-27`.

### Identity / auth (4)
| Table | Key columns | Notes |
|---|---|---|
| `users` | `id` PK, `email` (lower(email) unique functional index), `phone`, `password_hash`, `role`, `status`, `created_at` | role ∈ `student\|teacher\|admin`; `password_hash` **nullable** |
| `auth_sessions` | `id`, `user_id` FK, `refresh_token_digest` (SHA-256), `used_at`, `revoked_at`, `expires_at` | rotation; `last_used_at` **never written** |
| `password_reset_tokens` | `id`, `user_id`, `token_digest`, `used_at`, `expires_at` | single-use |
| `auth_events` | `id`, `user_id`, `event_type` (free text), `created_at` | **write-only** — no reader endpoint |

### Profile (2)
| Table | Key columns |
|---|---|
| `students` | `id`, `user_id` FK UNIQUE, `full_name`, `date_of_birth`, `gender` (**CHECK** `students_gender_check`), `country`, `phone`; mirrored by `GENDER_VALUES = ("female","male","other","undisclosed")` in `student_profile.py:41` |
| `student_profile_history` | `student_id`, `change_type`, `changed_by`, `diff` JSON |

### Catalog (12)
| Table | Purpose |
|---|---|
| `academic_years` | `name`, `start_date`, `end_date`, `status` ∈ `planned/active/closed/archived` |
| `pathways` | `code`, `name`, `status` — seeded: `O_LEVEL`, `A_LEVEL`, `TVET`, `TTC` |
| `education_levels` | `code`, `name`, `level_number` (7…15 ordinal) |
| `pathway_levels` | **join** `pathway_id` + `education_level_id` — the real mapping table |
| `programs` | `code`, `program_type` (`academic`/`tvet_program`), `status` |
| `program_versions` | identity = `(program_id, academic_year_id, pathway_id, education_level_id)`; unique on that tuple; `code`, `status` |
| `program_subjects` | `program_version_id` + `subject_id` + `is_compulsory` → drives `student_subjects` |
| `subjects` | `code`, `name`, `status` |
| `tvet_sectors` | `code`, `name` |
| `tvet_programs` | `program_id` + `sector_id` (required for TVET program versions) |
| `schools` | `school_code` UNIQUE, `name`, `province`, `district`, `status` |
| `school_programs` | **join** `school_id` + `program_version_id` — the only proof a school offers a version |

### Enrollment (2)
| Table | Key constraints |
|---|---|
| `student_enrollments` | `student_id`, `academic_year_id`, `school_id` NOT NULL, `pathway_id`, `education_level_id`, `program_version_id` NULL, `status` (`pending/…`); **UNIQUE `(student_id, academic_year_id)`** → `uq_student_enrollments_student_id_academic_year_id_key` |
| `student_subjects` | derived from `program_subjects` at registration; `status='active'` |

### Rwanda education logic — where it actually lives
Encoded as **data rows, not code branches**:

| Concept | Representation | Source |
|---|---|---|
| Ordinary Level **S1–S3** | `pathway_levels` rows: `O_LEVEL`→`S1,S2,S3` | `app/data/pathway_levels.py`, `education_levels.py` |
| Advanced Level **S4–S6** | `A_LEVEL`→`S4,S5,S6` (also `TTC`→`S4,S5,S6`) | same |
| TVET **L3–L5** | `TVET`→`L3,L4,L5` | same |
| levels ordinal | `education_levels.level_number` 7…15 | `app/data/education_levels.py` |
| schools | `schools` table | **seed file empty** |
| pathways | `pathways` table | 4 rows seeded |
| programs / versions | `programs`, `program_versions` | **seed files empty** |
| subjects | `subjects` | 14 rows seeded |
| enrollments | `student_enrollments` | created at runtime |
| academic years | `academic_years` | 1 row, `status="closed"` |

**Nothing in the service layer hard-codes S1/S2/S3.** `_validate_pathway_level` (`registration_service.py:201`) only checks that a `pathway_levels` row exists.

---

# 8. Database Architecture

- **Engine:** module-level singleton, `create_engine(database_url, pool_pre_ping=True, pool_size=5, max_overflow=10, pool_recycle=1800)` — `backend/app/core/database.py`.
- **Session:** `SessionLocal(autocommit=False, autoflush=False, expire_on_commit=False)`; `get_db()` yields and `close()`s (implicit rollback on unclosed transaction). **No explicit `rollback()` in `get_db`.**
- **Base:** `Base = declarative_base(metadata=MetaData(naming_convention={...}))` — but **`alembic/env.py` does not pass `naming_convention` to `context.configure(...)`**, so autogenerate will propose index/constraint name drift (D1–D4 below).
- **Migrations:** 5 linear revisions, head = `0005`.

| Rev | File | Content |
|---|---|---|
| 0001 | `0001_initial_schema.py` | 16 tables (catalog + profile + enrollment) |
| 0002 | `0002_authentication.py` | `users.password_hash`, auth hardening |
| 0003 | `0003_password_reset_and_audit.py` | `password_reset_tokens`, `auth_sessions`, `auth_events` |
| 0004 | `0004_student_profile_hardening.py` | CHECK constraints, indexes |
| 0005 | `0005_role_vocabulary.py` | collapses role vocabulary to `student/teacher/admin` |

- **Connection:** `alembic/env.py` builds the URL from `get_settings()`; `alembic.ini` has no hardcoded DSN.
- **Verification:** `scripts/verify_database.py` (offline ORM-vs-migration compare; `--live` inspects PG + `alembic_version`). Its `EXPECTED_TABLES` = **17**, so the structural check **fails by construction** against the real 20.
- **Reset:** `scripts/schema_reset.sql` — **no production guard**, hard-codes `DROP DATABASE`/role names.
- **Seeder:** `scripts/seed_reference_data.py` (`--dry-run`, idempotent upserts, production guard tested by `test_seeder_production_guard.py`).
- **Index-name drift (D1–D4):** migration-created indexes differ from model-created ones — `auth_sessions` 2 indexes missing in one direction; `password_reset_tokens`/`auth_events` use `ix_*` vs `*_idx`; one unique index unnamed. Consequence: `verify_database --live` and future autogenerate report false diffs.

---

# 9. API Map

**35 endpoints**, all under `/api/v1`. 24 are rate-limited; the 10 catalog reads + `/health` are not.

### Public (no auth) — 18
| Method | Path | Limiter | Notes |
|---|---|---|---|
| GET | `/health` | — | **no DB ping** |
| POST | `/auth/register` | 5/min | 201 `TokenResponse` |
| POST | `/auth/login` | 10/min | generic 401 |
| POST | `/auth/refresh` | 30/min | rotation |
| POST | `/auth/forgot-password` | 5/min | 204 always (anti-enumeration) |
| POST | `/auth/reset-password` | 5/min | 204 always |
| GET | `/registrations/readiness` | 30/min | drives `UnavailableNotice` |
| GET | `/catalog/academic-years` `?status=` | — | |
| GET | `/catalog/pathways` `?status=` | — | |
| GET | `/catalog/education-levels` `?pathway=` | — | filters via real `pathway_levels` |
| GET | `/catalog/subjects` | — | |
| GET | `/catalog/programs` `?program_type=&pathway=&level=` | — | |
| GET | `/catalog/program-versions` | — | |
| GET | `/catalog/tvet/sectors` | — | |
| GET | `/catalog/tvet/programs` `?sector=` | — | |
| GET | `/catalog/schools` `?province=&district=` | — | |
| GET | `/catalog/schools/{school_code}/programs` | — | → `list[SchoolProgramRead]` |

### Any authenticated role — 5
`POST /auth/logout` (30/min) · `GET /auth/me` (60/min) · `GET /me` (60/min) · `POST /me/change-password` (10/min) · `POST /me/deactivate` (5/min)

### Student only (`role=student`) — 6
`GET /me/student` (30) · `POST /me/student` (10) · `PATCH /me/student` (10) · `POST /me/registrations` (10) · `GET /me/registrations` (30) · `GET /me/registrations/{enrollment_id}` (30)

### Admin only (`role=admin`) — 7
`POST /admin/students` (10) · `GET /admin/students/{student_id}` (30) · `PATCH /admin/students/{student_id}` (10) · `GET /admin/students/{student_id}/history` (30) · `POST /admin/registrations` (10) · `GET /admin/registrations/{enrollment_id}` (30) · `GET /admin/students/{student_id}/registrations` (30)

**Route-order safety:** `admin_students.router` (`/admin/students`) is included *before* `registrations.admin_router` (`/admin/...`); `/{student_id}/history` and `/{student_id}/registrations` are distinct literals so no shadowing. `auth.me_router` (`/me`) is included before `registrations.me_router` (`/me/registrations`) — no conflict.

**Not present:** `PUT`, `DELETE`, `PATCH` on any catalog route, WebSocket, GraphQL, pagination parameters (all list endpoints return unbounded rows), OpenAPI-driven client generation.

---

# 10. Authentication & Authorization

### Chain (verified)
```
Authorization: Bearer <jwt>
  → HTTPBearer(auto_error=False)                       # missing header → None, not 403
  → get_current_user (core/auth_dependencies.py)
      → decode_token(token, expected_type="access")    # HS256, checks typ + exp
      → sub → UUID → auth_service.get_user_by_id()      # DB read EVERY request
      → user.status must be ACTIVE else 401
  → require_role / get_current_student
      → role read from the DB row, never from JWT claims
      → student routes: role must be student AND profile must exist
         else 403 with detail pointing at POST /api/v1/me/student
  → handler
```

### Credentials
- **Password:** Argon2id (`argon2-cffi`), policy via `validate_password_policy` (`PASSWORD_MIN_LENGTH`, default 8), enforced at **schema level** (`schemas/auth.py:73-81`) so it cannot be bypassed by a service call.
- **Access token:** HS256, claims `sub`/`role`/`typ`/`iat`/`exp`, `ACCESS_TOKEN_EXPIRE_MINUTES=30`.
- **Refresh token:** JWT **plus** a server-side `auth_sessions` row storing only the **SHA-256 digest**; rotation on every refresh (old row marked used); `enforce_session_limit` revokes oldest beyond `MAX_SESSIONS_PER_USER`; logout revokes server-side.
- **Authority:** DB, not token — a suspended account is refused on the next request even with a valid unexpired access token.

### Anti-patterns explicitly avoided
- `POST /me/registrations` uses `RegistrationCreateSelf` with `extra="forbid"` and **no `student_id` field** (`schemas/registration.py`) → supplying one is 422, not privilege escalation.
- Admin path builds `RegistrationCreate` with `student_id` from an admin-only body + `require_admin`.
- Login returns one generic 401 for unknown email / wrong password / suspended account.
- Public register duplicate-email 409 detail is the generic `"email cannot be used to create an account"` (anti-enumeration); admin duplicate 409 keeps the explicit message.
- `forgot-password` always 204; `reset-password` always 204.
- No response schema includes `password_hash`.

### Role namespaces (exclusive, per README:101-113 and router prefixes)
`/me/*` any role · `/me/student`, `/me/registrations` student · `/admin/*` admin. **No route serves two roles.**

---

# 11. Frontend / Client Architecture

**Routing:** `src/App.jsx` — raw `window.location.pathname` switch, no router library. `/register` → `StudentPortal`, `/admin` → `AdminWorkspace`, else `PublicMapExperience`.

**Two independent auth systems coexist in one bundle:**

| Surface | Auth | Storage | Backend |
|---|---|---|---|
| `/admin` (AdminWorkspace) | Firebase Auth | Firebase SDK | Firestore |
| `/register` (StudentPortal) | FastAPI JWT | `localStorage` `st_access_token` / `st_refresh_token` | `backend/` |

**HTTP client — `src/lib/api.js`:**
- Base: `import.meta.env.VITE_API_URL || "http://127.0.0.1:8000/api/v1"`
- Adds `Authorization: Bearer` unless `skipAuth`
- On 401 + refresh token → **one** `POST /auth/refresh`, then retry once; stores the rotated pair
- On failure, attaches `error.response.data = detail`
- `friendlyError(err, fallback)` — reads `err.response.data.detail` or falls back; **the `err.message === "Network Error"` branch is dead** (native `fetch` throws `TypeError: Failed to fetch`)

**Student session — `StudentPortal.jsx`:**
- On mount: if `isLoggedIn()` → `getMe()`; on failure `clearTokens()`
- `handleTokens` saves the pair then re-reads `/me`
- `handleLogout` best-effort `POST /auth/logout` then clears locally

**Registration wizard flow (`RegistrationFlow.jsx` → `RegisterWizard.jsx`):**
1. `GET /registrations/readiness` + `GET /catalog/schools`
2. If `!readiness.ready` → `UnavailableNotice`, **stop**
3. Step 1 choose school → `GET /catalog/schools/{code}/programs` (filter `status==='active' && pathway_code==='O_LEVEL' && level_code===level.code`)
4. Step 2 choose class → `GET /catalog/education-levels?pathway=O_LEVEL`
5. Step 3 (optional) choose combination → holds a **`SchoolProgramRead` object**
6. Step 4 confirm → `POST /me/registrations`

**Dead client code:** `services/catalog.js` → `getAcademicYears`, `getPathways`; `services/registrations.js` → `getRegistration`; `api.js` Network-Error branch.

**Deployment defect:** `dist/` was built with `VITE_API_URL` unresolved → the bundled fallback `http://127.0.0.1:8000/api/v1` is baked in. Serving `dist/` to any other host breaks every API call.

---

# 12. External Services

| Service | Where | Used for | Verified state |
|---|---|---|---|
| **Gemini Live** (`google-genai`) | `main.py` | desktop AI tutor | ACTIVE; infinite reconnect loop, no user-facing auth error |
| **OpenRouter** | `memory/memory_manager.py` → `or_client` | memory extraction PII call | **BROKEN** — `or_client` source missing; only `__pycache__/or_client.*.pyc` remains |
| **Firebase Firestore/Storage** | `src/lib/firebase.js`, `firebase.rules`, `storage.rules` | Letters map, journal, admin console | ACTIVE; rules permit any signed-in user to write any document |
| **Resend (email)** | `backend/app/core/email.py`, `RESEND_*` settings | password-reset email | **NOT INSTALLED** in `.venv`; import wrapped in try/except → logged, 204 still returned |
| **PostgreSQL** | `backend/` | sole backend datastore | ACTIVE (unknown whether a DB is currently reachable — no liveness check run) |
| Queues / cache / object storage / SMS / webhooks / 3rd-party APIs (backend) | — | — | **NONE** |

---

# 13. Configuration & Environment

### Backend — 24 keys (`.env.example`, `backend/.gitignore` ignores `.env`)
`DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` → `database_url` builder · `SECRET_KEY` (prod requires non-placeholder; `_UNSAFE_SECRETS` list) · `PASSWORD_MIN_LENGTH` · `ACCESS_TOKEN_EXPIRE_MINUTES` · `REFRESH_TOKEN_EXPIRE_DAYS` · `MAX_SESSIONS_PER_USER` · `STUDENT_MIN_AGE_YEARS` · `STUDENT_MAX_AGE_YEARS` · `CORS_ORIGINS` (production strips localhost) · `RATE_LIMIT_{REGISTER,LOGIN,REFRESH,ME_READ,STUDENT_READ,STUDENT_WRITE,STUDENT_HISTORY}` · `DB_POOL_SIZE`/`DB_MAX_OVERFLOW`/`DB_POOL_RECYCLE` · `RESEND_API_KEY` · `RESEND_FROM_EMAIL` · `FRONTEND_URL` · `ENVIRONMENT`

`_DEVELOPMENT_STAGE_ENVIRONMENTS` controls: docs/OpenAPI exposure, CORS localhost allowance, secret strictness.

### Frontend — `.env.example`
`VITE_FIREBASE_API_KEY`, `VITE_FIREBASE_AUTH_DOMAIN`, `VITE_FIREBASE_PROJECT_ID`, `VITE_FIREBASE_STORAGE_BUCKET`, `VITE_FIREBASE_MESSAGING_SENDER_ID`, `VITE_FIREBASE_APP_ID`, **`VITE_API_URL=http://127.0.0.1:8000/api/v1`**. Line 1 of `.env.example` is `[TEMPLATE]` — invalid dotenv syntax if copied verbatim.

### Desktop
`config/api_keys.json` (Gemini + OpenRouter), `config/accounts.json`, `config/student_profile.json`, `memory/long_term.json` — all gitignored.

**Verified risk:** `settings` is resolved from **CWD-relative** `.env`. Running uvicorn from outside `backend/` silently uses defaults (including an auto-generated `SECRET_KEY`), producing tokens that do not survive a restart.

---

# 14. Major Feature Data Flows

### Flow A — Student account creation
```
AuthScreens.SignupFlow.submit (AuthScreens.jsx:108)
  → services/auth.js registerAccount({email,password,full_name,date_of_birth,gender})
  → apiFetch POST /auth/register  (skipAuth, no Authorization header)
  → [rate limit 5/min]
  → pydantic StudentAccountCreate  (schemas/auth.py:35)
       field validators: _validate_full_name, _validate_gender, _validate_country
       mode="after" password validator → validate_password_policy → 422 on failure
  → Depends(get_db)
  → auth_service.register_student_account (services/auth_service.py)
       email = email.lower()
       ensure_plausible_dob → 422
       user_repo.get_by_email pre-check → 409 generic
       user_repo.create_student_user(role="student", status="active", password_hash=Argon2id)
       student_repo.create                      ── IntegrityError → 409
       student_profile_history_repo.log_change("create", changed_by=user.id)
       _issue_session:
         enforce_session_limit(MAX_SESSIONS_PER_USER)  → revoke oldest
         auth_session_repo.create(raw refresh JWT → SHA-256 digest)
         create_token(sub, typ="refresh") + create_token(sub, typ="access")
  → endpoint: session.commit()
  → 201 TokenResponse {access_token, refresh_token, token_type, expires_in}
  → saveTokens() → localStorage
```
**Transaction boundary:** endpoint-owned. Any raised `ProfileError` → `session.rollback()` → `HTTPException`.

### Flow B — Student Registration (the active feature, full trace)
```
RegisterWizard.submit (RegisterWizard.jsx:104)
  body = {academic_year_id, pathway:"O_LEVEL", education_level:<code>,
          program_version_id: programVersion?.id || null, school_id}
  → POST /me/registrations
  1. slowapi 10/minute
  2. pydantic RegistrationCreateSelf  (extra="forbid", NO student_id)   → 422
  3. Depends(get_current_student)
        get_current_user → decode_token(typ="access") → DB user → status ACTIVE
        role must be "student"                     → else 403
        student_profile must exist                 → else 403 (+remedy text)
  4. endpoint builds RegistrationCreate(student_id=student.id)   ← identity from TOKEN
  5. registration_service.register_student(session, payload)    (registration_service.py:316)
        1. _resolve_student        → 404
        2. _resolve_academic_year  → 404
           _validate_year_registrable: status ∈ REGISTRABLE_YEAR_STATUSES else 503
        3. _resolve_pathway(code)  → 404
           _resolve_education_level(code) → 404
           _validate_pathway_level: real pathway_levels row else 422
        4. _resolve_school(school_id) → 404          (school_id NOT NULL)
        5. enrollment_repo.find_existing(student, year) → 409 duplicate
        6. if program_version_id:
             _resolve_program_version → session.get(ProgramVersion, id) → 404  ◄── MISMATCH
             verify (program, year, pathway, level) tuple match else 422
             _validate_program_status: status=="active" else 422
             _validate_tvet_profile: program_type=="tvet_program" → TVETProgram.sector must exist else 503
             _validate_school_offers: real school_programs row else 422
        7. enrollment_repo.create(status="pending")
             IntegrityError on uq_student_enrollments_* → 409 (race safety)
             other IntegrityError re-raised → 500
        8. subject_ids = program_subjects for the version (only real mappings)
           student_subjects_repo.create_many(status="active")
        9. _load_registration_read → RegistrationRead
  6. endpoint: session.commit()          (except RegistrationError → rollback → HTTPException)
  7. 201 RegistrationRead
  → RegistrationFlow swaps to RegistrationSuccess (reference number)
```

### Flow C — Login / refresh
```
login → authenticate() → one generic 401 → _issue_session → 201 TokenResponse
refresh → auth_session lookup by SHA-256 digest → not used/revoked/expired → 401
        → row locked → mark used → new session row + new pair (rotation) → commit
logout → Bearer required + refresh_token body → revoke that session → 204
```

### Flow D — Desktop lesson request
`ui.JarvisUI` → Gemini Live tool call → `core.student_mode.filter_tool_declarations` (allowlist) → `core.curriculum_loader.get_lesson_context` (lazy tree, LRU, path traversal guarded) → response. Memory: `memory_manager.should_extract_memory` → background thread → **`extract_memory` fails** (missing `or_client`) → swallowed.

### Flow E — Letters
React → Firestore `onSnapshot` subscriptions → `firebase.rules` (any authed user may write any doc) → UI.

---

# 15. Business Rules

Verified in `registration_service.py`, `auth_service.py`, `student_service.py`, `schemas/*`.

**Enrollment**
1. Only years with status ∈ `REGISTRABLE_YEAR_STATUSES` accept registrations (503 otherwise).
2. `pathway` + `education_level` must exist **and** be linked by a real `pathway_levels` row (422).
3. `school_id` is **required** (`RegistrationCreate.school_id`, not nullable) and must exist (404).
4. **At most one enrollment per student per academic year** — pre-checked (409) and enforced by DB UNIQUE (409 on race).
5. When a program version is supplied: it must exist (404), match the (program, year, pathway, level) tuple (422), be `active` (422), have a complete TVET profile if `program_type == "tvet_program"` (503), and be offered by the school via a real `school_programs` row (422).
6. `student_subjects` are derived **only** from `program_subjects` — bare `subjects` are never used to invent enrollments.
7. New enrollments start as `pending`.

**Accounts**
8. Public registration fixes `role="student"` server-side.
9. Email lowercased and unique (functional index on `lower(email)`); duplicate → generic 409.
10. DOB must be plausible: not future, ≥ `1900-01-01`, within `[STUDENT_MIN_AGE_YEARS, STUDENT_MAX_AGE_YEARS]` → 422.
11. `full_name`: Unicode letters + ` -.'`, ≥1 letter; digits/markup → 422.
12. `gender` ∈ `{female, male, other, undisclosed}` — enforced by **both** Pydantic and a DB CHECK (`migration 0004`).
13. Password policy at schema layer (`mode="after"`), Argon2id at persistence layer.
14. Session cap: oldest sessions revoked beyond `MAX_SESSIONS_PER_USER`.
15. Refresh rotates; replay of a used refresh token → 401.
16. Password reset is single-use; success revokes all sessions and logs `password_reset`.
17. Profile writes always log a `student_profile_history` row with `changed_by`.

**Catalog reads**
18. `list_education_levels(pathway_id=...)` filters **through `pathway_levels`**, never by name string (`catalog_repository.py:94-98`).
19. `list_programs(pathway/level)` filters **through `program_versions`**, not by program fields (`catalog_repository.py:134-143`).
20. School offerings are never inferred — only real `school_programs` rows (`catalog_repository.py:241-261`).
21. Documented ordering contract for every list (repo module docstring, `catalog_repository.py:9-24`).

---

# 16. Error Handling

### Exception taxonomy
```
ProfileError (400 base)                      → app-level handler in main.py
├─ ProfileValidationError      → 422
├─ ProfileConflictError        → 409
├─ ProfileNotFoundError        → 404
└─ ProfileForbiddenError       → 403

RegistrationError (per-endpoint handler, registrations.py:67)
├─ RegistrationValidationError → 422
├─ RegistrationConflictError   → 409
├─ RegistrationNotFoundError   → 404
└─ RegistrationUnavailableError→ 503

CatalogNotFoundError (LookupError)           → catalog_service → 404
PasswordPolicyError                          → caught inside a field_validator → ValueError → 422
RateLimitExceeded (slowapi)                  → 429 JSON handler
HTTPException                                → FastAPI default
Anything else                                → app handler → {"detail":"Internal server error"} + logger.exception
```

**Mapping is by MRO**, so a `ProfileNotFoundError` is correctly classified as 404 rather than the 400 base code.

**Transaction safety:** every registration handler does
```python
except RegistrationError as exc:
    session.rollback()
    raise HTTPException(...)   # via _http_error
session.commit()
```
(`registrations.py:140-143`, `247-250`). Services only `flush()`; the endpoint owns commit/rollback.

**Verified weaknesses**
- `get_db` has **no explicit rollback** — it relies on `Session.close()` performing an implicit rollback.
- `student_service.update_student_profile` maps **any** `IntegrityError` to 422, masking unrelated DB faults as validation errors.
- `GET /me/registrations/{id}` distinguishes 404 (unknown id) from 403 (someone else's) → an **existence oracle** (low severity: UUIDs are unguessable).
- No structured error codes — clients must parse `detail` strings.
- Frontend has **no 429 handling**; a rate-limited user sees a generic failure string.

---

# 17. Async / Background Processing

| Area | Mechanism | Location |
|---|---|---|
| Backend | **None.** No `BackgroundTasks`, no `lifespan`, no WebSockets, no task queue, no scheduler, no cron | grep across `backend/app/**` → 0 hits |
| Backend DB | SQLAlchemy sync engine on the FastAPI threadpool (Starlette `run_in_threadpool`) | `core/database.py` |
| Rate limiting | slowapi **in-memory** counters — per process, lost on restart | `core/rate_limit.py` |
| Desktop | `asyncio` task group for the Gemini Live loop; `threading` for memory extraction; infinite 3 s reconnect | `main.py` |
| Frontend | React `useEffect`/`useState`; Firestore `onSnapshot` subscriptions; `WebAudio` in `AmbientPlayer` | `src/` |
| Scheduled ops | **Manual only**: `scripts/cleanup_auth_sessions.py` must be run by hand | `backend/scripts/` |

Consequence: expired `auth_sessions` rows and used `password_reset_tokens` accumulate unless an operator runs the cleanup script.

---

# 18. Logging & Observability

**Backend:** stdlib `logging` only.
- `app.logger.exception(...)` in the catch-all handler (the only place a stack trace is emitted).
- `logger.info/warning` in auth and email paths (e.g. reset-send failure is logged, never disclosed).
- **No access-log middleware** — uvicorn's own request log is the only per-request record.
- **No request IDs, no correlation IDs, no metrics, no tracing, no health-based readiness, no error reporting/Sentry.**
- `GET /health` returns static JSON — it does **not** touch the database, so it cannot detect a broken connection.

**Desktop:** `logging.basicConfig(level=INFO, format="[%(name)s] %(levelname)s %(message)s")` → `logger = logging.getLogger("RINA")` (`main.py:10-15`); output to console; `dev-server.log` / `dev-server.err.log` at repo root are committed and contain local absolute paths.

**Frontend:** `console.*` only.

**Audit:** `auth_events` and `student_profile_history` are written but `auth_events` has **no reader** — it is write-only.

---

# 19. Testing Architecture

```
backend/tests/
├─ conftest.py          fixtures; APPLICATION_TABLES = 17 (missing 3 auth tables)
├─ unit/        12 files, 252 tests   — SQLite in-memory, no PostgreSQL needed
│  test_auth_core, test_auth_session…, test_registration_service,
│  test_registration_hardening, test_role_authorization,
│  test_catalog_service, test_catalog_schemas, test_student_profile,
│  test_password_and_deactivation, test_session_cleanup,
│  test_datasets, test_loader, test_validation, test_seeder_production_guard,
│  test_school_schema, test_cli, test_cors
└─ integration/ 9 files, 131 tests   — marked `-m integration`, real PostgreSQL
   test_auth_api, test_catalog_api, test_registration_api,
   test_registration_end_to_end, test_role_auth_api,
   test_student_profile_api, test_seeder_idempotency, test_database_fixture
```
**Totals: 383 test functions (252 unit / 131 integration).**

- Runner: `pytest.ini` — **unit by default**, integration opt-in (`python -m pytest -m integration`), all via `pytest -m ""`.
- Coverage: `pytest-cov` in `requirements-dev.txt`.
- HTTP layer: `httpx` + FastAPI `TestClient`.

**Verified gaps**
- **Zero frontend tests** (no `*.test.*`, no vitest/jest in `package.json`).
- **Zero desktop tests.**
- **`conftest.APPLICATION_TABLES` = 17** vs the real 20 → integration teardown/verification silently omits `auth_sessions`, `password_reset_tokens`, `auth_events`.
- **No CI** — nothing runs these tests automatically.
- The `programVersion` contract mismatch in `RegisterWizard.jsx` is untested because there are no JS tests.

---

# 20. CI/CD & Deployment Pipeline

**There is none.**

| Check | Result |
|---|---|
| `.github/` | `False` |
| `**/*.yml`, `**/*.yaml` (excluding `.venv`/`node_modules`) | **0 files** |
| `Dockerfile`, `docker-compose*` | **0 files** |
| `Jenkinsfile` | **0 files** |
| `pyproject.toml` / `setup.py` / `Makefile` in `backend/` | **absent** |
| Lint / typecheck / formatter config anywhere | **absent** (no ruff, mypy, eslint, prettier) |
| Pre-commit hooks | **absent** |

**Documented manual runbooks**
- Backend: `backend/README.md:450` `python -m alembic upgrade head`; `:508` `python -m uvicorn app.main:app --reload --port 8000`; `:463-464` `python scripts/verify_database.py [--live]`; `:302-303` `python scripts/seed_reference_data.py [--dry-run]`
- Web: `npm run dev` / `npm run build`
- Desktop: `python main.py`
- Root `DEPLOYMENT.md` covers **only** the desktop + Letters apps (hosting, Firebase).

**Deployment risk:** `dist/` ships the hardcoded `http://127.0.0.1:8000/api/v1` fallback; `backend/.env` must be present relative to the working directory; production mode requires a non-placeholder `SECRET_KEY` or startup fails.

---

# 21. Dependency Graph

### Backend (declared → installed → actually imported)
```
fastapi ──▶ pydantic ──▶ pydantic-settings ──▶ python-dotenv
   │           │
uvicorn        └── (validation of all schemas)
sqlalchemy ──▶ psycopg (PostgreSQL) ──▶ greenlet
alembic
slowapi
pyjwt
argon2-cffi ──▶ _argon2_cffi_bindings
email-validator  (EmailStr)
resend        ✗ DECLARED, NOT INSTALLED (guarded import)
── dev ── pytest, pytest-cov, httpx
```

### Import layering (enforced, no cycles)
```
endpoints ──▶ services ──▶ repositories ──▶ models ──▶ core.database
    │             │              │
    │             └──▶ schemas ◄─┘ (validators reuse app.schemas.student_profile helpers)
    └──▶ core.auth_dependencies ──▶ core.security ──▶ app.models.user
main.py ──▶ api.v1.router ──▶ endpoints/* , core.rate_limit, core.config
alembic/env.py ──▶ app.models (all 20) ──▶ core.database
scripts/* ──▶ app.data.* , app.models
```
`app/data/*` is imported **only** by `scripts/` and `tests/` — never by the running API.

### Frontend
`main.jsx` → `App.jsx` → {`PublicMapExperience` | `AdminWorkspace` | `StudentPortal`}
`StudentPortal` → `AuthScreens`, `RegistrationFlow` → `RegisterWizard`, `StudentUI`, `services/*`, `lib/api.js`
`PublicMapExperience`/`AdminWorkspace` → `lib/firebase.js` → Firebase SDK

### Desktop
`main.py` → `ui`, `core.*`, `memory.memory_manager`(→ **missing `or_client`**), `google.genai`, `sounddevice`

---

# 22. Security Architecture Review

### Verified controls (high confidence)
| Control | Evidence |
|---|---|
| Argon2id password hashing | `core/security.py`; `hash_password` used by both register paths |
| Password policy at the **schema** layer | `schemas/auth.py:73-81`, `:88-98`, `:113-121` |
| HS256 JWT with `typ` discrimination | `core/security.py` `create_token`/`decode_token(expected_type=...)` |
| Revocable refresh sessions (SHA-256 digest, rotation, session cap) | `auth_service` + `auth_session_repository` |
| **DB is authority for role/status**, not JWT claims | `get_current_user` re-reads the user row every request |
| Identity never taken from body | `RegistrationCreateSelf` has no `student_id`; `extra="forbid"` |
| Generic 401 on login | `auth_service.authenticate` |
| Anti-enumeration on register + forgot/reset | generic 409 detail; unconditional 204s |
| Argon2/DB failures don't leak as 500 on login | caught and folded into the generic 401 |
| Per-endpoint rate limiting (24/35 routes) | `@limiter.limit(...)` across 4 endpoint modules |
| Security headers incl. HSTS in prod | `_security_headers` middleware |
| CORS: explicit origins, no credentials, no wildcard | `main.py`; production strips localhost |
| Production hardening: docs/redoc/openapi off, `SECRET_KEY` validated | `_is_production`, `_UNSAFE_SECRETS` |
| Generic 500, no stack trace to client | catch-all `Exception` handler |
| No secrets in git | root + `backend/.gitignore` ignore `.env`; only **key names** verified |
| Gender vocabulary double-enforced (Pydantic + DB CHECK) | `student_profile.py:41`, migration 0004 |
| Path traversal hardened in curriculum loader | `core/curriculum_loader.py` |

### Verified risks

**G1 — HIGH — `resend` missing ⇒ password recovery silently non-functional.**
`backend/requirements.txt` lists `resend`; `Test-Path .venv/Lib/site-packages/resend` → **False**. `core/email.py` wraps `import resend` in `try/except`; `auth_service.forgot_password` catches send failure, logs it, and still returns 204. A user who forgets a password receives no email and no error. The `password_reset_tokens` row is still created (single-use, expires).
*Confidence: high.*

**G2 — HIGH — Rate limiter trusts `request.client.host` with no proxy awareness.**
`core/rate_limit.py` `key_func` returns the socket peer. There is no `ProxyHeadersMiddleware` and no `X-Forwarded-For` handling. Behind nginx/ALB **every user shares one bucket** → trivial global lockout (e.g. login 10/min) or, if `--forwarded-allow-ips` is misconfigured, complete bypass. Storage is in-memory → per-process, resets on restart, does not work across workers.
*Confidence: high.*

**G3 — HIGH — `POST /admin/students` creates accounts that cannot log in.**
The admin create path builds a `users` row with `password_hash=None` (no password in the body schema). Recovery depends entirely on `forgot-password` → **blocked by G1**. In the current environment those accounts are permanently unusable.
*Confidence: high.*

**G4 — MEDIUM — Catalog + health endpoints are unthrottled and public.**
10 `/catalog/*` routes and `/health` have no `@limiter.limit`. Each catalog read is a real SQL query with `selectinload` chains (`catalog_repository.py:147-261`) and **no pagination**. Cheap DB exhaustion.
*Confidence: high.*

**G5 — MEDIUM — JWTs in `localStorage`.**
Any XSS in the SPA yields a live 30-minute access token plus a refresh token. Combined with `friendlyError` rendering server `detail` strings, and no CSP header (only `X-Content-Type-Options`/`X-Frame-Options` are set), the blast radius is meaningful.
*Confidence: high (pattern), exploitability unknown.*

**G6 — MEDIUM — Two independent admin/auth surfaces with different rules.**
`/admin` (Firebase, `firebase.rules` allows any signed-in user to write any letter/echo) vs `/admin/*` (FastAPI, `require_admin`). An operator must reason about both; there is no single authorization source of truth.
*Confidence: high.*

**G7 — MEDIUM — Existence oracle on `GET /me/registrations/{id}`.**
404 for unknown id, 403 for another student's id (`registrations.py:199-205`). Low practical impact (UUIDs).
*Confidence: high.*

**G8 — LOW/MEDIUM — `schema_reset.sql` has no production guard.**
Drops databases/roles with hard-coded names; guarded only by operator discipline.
*Confidence: high.*

**G9 — LOW — `StudentProfileCreate` lacks `extra="forbid"`** (admin path silently ignores unknown keys), while the self-service and registration schemas do forbid extras — inconsistent strictness.
*Confidence: high.*

**G10 — LOW — `auth_events.event_type` is free text** (no CHECK), and no endpoint reads the table.
*Confidence: high.*

---

# 23. Contract Consistency Review

### C1 — HIGH — `program_version_id` type mismatch (frontend → backend)
- **Evidence:** `src/components/student/RegisterWizard.jsx:112` → `program_version_id: programVersion?.id`
- `programVersion` is set at `:231` from an item of `getSchoolOfferings(...)` → `GET /catalog/schools/{code}/programs` → `response_model=list[SchoolProgramRead]` (`endpoints/catalog.py`)
- `schemas/catalog.py:145-157`: `class SchoolProgramRead` → `id: UUID` (**`school_programs.id`**) **and** `program_version_id: UUID`
- `registration_service.py:233-250` `_resolve_program_version` → `session.get(ProgramVersion, program_version_id)`
- **Behavior:** the `school_programs` PK is sent where a `program_versions` PK is expected → `None` → `RegistrationNotFoundError` → **404**
- **Consequence:** choosing a combination in step 3 always fails registration. Skipping step 3 sends `null` and works.
- **Fix shape (not applied):** send `programVersion.program_version_id`.
- **Confidence: high.**

### C2 — HIGH — Seed data makes the feature unreachable
- `app/data/{schools,programs,program_versions,school_programs}.py` are empty; `academic_years` seeds `2025/2026` with `status="closed"`.
- `/registrations/readiness` → `ready=False` → `RegisterWizard.jsx:132-138` renders `UnavailableNotice`.
- Even bypassing the UI, `_validate_year_registrable` → 503.
- **Confidence: high.**

### C3 — HIGH — Build-time API base baked into `dist/`
`src/lib/api.js` fallback `http://127.0.0.1:8000/api/v1` compiles into the bundle. Serving `dist/` anywhere but `localhost` breaks all API calls.
**Confidence: high.**

### C4 — MEDIUM — `verify_database.py` `EXPECTED_TABLES` = 17 vs real 20
Structural verification fails by construction; the tool's green path is unreachable.
**Confidence: high.**

### C5 — MEDIUM — `tests/conftest.py` `APPLICATION_TABLES` = 17 vs real 20
Integration fixtures omit `auth_sessions`, `password_reset_tokens`, `auth_events`.
**Confidence: high.**

### C6 — MEDIUM — Alembic `env.py` omits `naming_convention` in `context.configure`
Model metadata declares a naming convention; migrations don't. Produces index/constraint name drift (`ix_*` vs `*_idx`, one unnamed unique) → false positives in `verify_database --live` and future autogenerate.
**Confidence: medium-high (drift observed; autogenerate not run — read-only mandate).**

### C7 — LOW — `friendlyError` dead branch
`err.message === "Network Error"` can never be produced by native `fetch` (`TypeError: Failed to fetch`).
**Confidence: high.**

### C8 — LOW — No 429 handling client-side; rate-limit rejections surface as generic failures.
**Confidence: high.**

### C9 — LOW — `password_reset_tokens`/`auth_sessions` cleanup is manual only.
**Confidence: high.**

### Consistent (verified OK)
`StudentAccountCreate` ↔ `SignupFlow` payload: `country` omitted → `default=None`; `gender: null` accepted; field names match exactly.
Gender vocabulary identical on both sides (`female/male/other/undisclosed`).
`RegistrationCreateSelf` ↔ wizard body: all five keys match.
`TokenResponse` ↔ `saveTokens`: `access_token`/`refresh_token` match.
Router prefix mounting produces exactly the documented 35 paths.

---

# 24. Dead / Duplicate / Legacy Code

### Backend
| Item | Evidence | Class |
|---|---|---|
| `registration_service.commit()` (`:521`) | docstring says "called by the API layer"; grep shows **0 non-test callers** — endpoints call `session.commit()` | CONFIRMED UNUSED |
| `dev_guard.require_development_stage` | only callers are `tests/unit/test_auth_core.py` | CONFIRMED UNUSED (prod code) |
| `student_service.load_student_for_user` | no callers | CONFIRMED UNUSED |
| `student_service.get_student_profile` | test-only | LIKELY UNUSED |
| `auth_session_repository.mark_used`, `get_by_id` | `last_used_at` never written by any flow | CONFIRMED UNUSED |
| `student_profile_history_repository.get_by_id` | test-only | LIKELY UNUSED |
| `app/data/*` runtime usage | imported only by `scripts/` and `tests/` | DEAD AT RUNTIME (by design) |
| `__pycache__/endpoints/students.cpython-314.pyc` | **no `students.py` source** | ORPHAN BYTECODE (deleted module) |
| `PROVENANCE` dicts in dataset modules | no readers | CONFIRMED UNUSED |
| `app/api/v1/endpoints/__init__.py` (31 b) | docstring only | doc-only, fine |

### Frontend
`services/catalog.js` → `getAcademicYears`, `getPathways`; `services/registrations.js` → `getRegistration`; `api.js` Network-Error branch — all unreferenced.
`AdminWorkspace` / `PublicMapExperience` / Letters features are a **separate product** living in the same bundle (not dead, but unrelated to the backend).

### Desktop / repo-wide
| Item | Class |
|---|---|
| `memory/memory_manager.py` → `or_client` missing (pyc only) | CONFIRMED BROKEN/DEAD |
| `__pycache__/` → `or_client`, `gen_reb_content`, `setup` pyc, no sources | ORPHAN BYTECODE |
| `actions/`, `agent/` (pyc only), `tmp/`, `.agents/` (empty) | DEAD RESIDUE |
| `rahura superteacher L 2.0/` (only `dist/` + `node_modules/`, no source) | DEAD/UNKNOWN INTENT |
| `dev-server.log`, `dev-server.err.log` committed | LEAK (local paths) |
| `docs/system-analysis/*` (16 files, dated 2026-09-10) | LEGACY — predates `backend/` and the Student Portal entirely |
| root `scripts/*.py` one-shot generators | LIKELY UNUSED |
| `data/curriculum/math/` legacy outputs | CONFIRMED UNUSED |

**Duplicate structures:** 3 admin surfaces (Firebase `AdminWorkspace`, FastAPI `/admin/*`, desktop settings); 2 auth systems; 2 curriculum formats (legacy JSON vs manifest tree).

---

# 25. Documentation vs Implementation

| Claim | Location | Reality |
|---|---|---|
| "The schema (**19 tables**)" | `backend/README.md:355` | **20** models imported in `models/__init__.py:8-27` — `auth_events` unlisted |
| "**The datasets are intentionally EMPTY right now.**" | `README.md:320` (also `:283`, `:287`) | **40 rows** seeded: 1 academic year, 4 pathways, 9 education levels, 12 pathway_levels, 14 subjects. Four datasets *are* empty (schools, programs, program_versions, school_programs) — the sentence overstates. |
| `verify_database.py` structural compares ORM vs migrations | `README.md:467` | `EXPECTED_TABLES=17` → cannot pass |
| Root `readme.md` / `DEPLOYMENT.md` | root | Describe **only** desktop + Letters; no mention of `backend/`, the API, or the Student Portal |
| `docs/system-analysis/EXECUTIVE-SUMMARY.md` | `docs/system-analysis/` (2026-09-10) | Scope line: "2,693 tracked files; desktop PyQt app, React Letters web app, untracked L2.0 nested copy" — **no `backend/`, no Student Portal**. Its Top-10 findings (Firestore, desktop SHA-256, missing `or_client`, `ui.py` god file) remain valid for products 1–2 but are **not** an audit of the current backend. |
| `docs/system-analysis/FILE-COVERAGE.md` | lists `Launch SuperTeacher.bat` | **file no longer exists** in the repo root |
| `backend/README.md:19` "Future modules (authentication, …)" | — | authentication is **already implemented** (Phase 5G) — stale wording |
| `.env.example` line 1 `[TEMPLATE]` | root | invalid dotenv if copied verbatim |

**Accurate documentation:** `backend/README.md` sections on auth semantics, role-split endpoints, error contract, seeder/verify/pytest commands (lines 32–160, 300–360, 440–520) — these matched the code on every check performed.

---

# 26. Risks & Findings

### A. VERIFIED ARCHITECTURE
- **A1** Three coexisting products in one git repo with **no shared build or workspace**. *Evidence:* root `package.json` (Letters) + `main.py` (desktop) + `backend/` (FastAPI) + no `workspaces` field. *Impact:* a clone can silently be missing an entire product (see F1). *Confidence: high.*
- **A2** Backend is strictly layered endpoint → service → repository → model, with commit owned by the endpoint layer only. *Evidence:* `registrations.py:140-143` does `rollback/commit`; `registration_service.py` and all repositories only `flush()`. *Impact:* transaction boundaries are predictable. *Confidence: high.*
- **A3** `app/data/*` is a pure seed-dataset package, decoupled from runtime. *Evidence:* no importer under `app/` outside `scripts/`/`tests/`. *Impact:* schema changes to reference data never affect request handling. *Confidence: high.*
- **A4** Rwanda's O/A/TVET model is **data, not code** — `pathway_levels` rows. *Evidence:* `registration_service._validate_pathway_level` only checks row existence; S1–S3/S4–S6/L3–L5 live in `app/data/pathway_levels.py`. *Confidence: high.*
- **A5** Role-exclusive namespaces (`/me`, `/me/student`, `/admin`) with no shared route. *Evidence:* `router.py` includes 9 routers with disjoint prefixes. *Confidence: high.*

### B. VERIFIED DATA FLOWS
- **B1** Account creation writes `users` + `students` + `student_profile_history` + `auth_sessions` in one committed transaction. *Evidence:* `auth_service.register_student_account`; endpoint `session.commit()`. *Confidence: high.*
- **B2** Registration writes `student_enrollments` (+ `student_subjects` when a program version is given) atomically, with a UNIQUE-constraint race backstop mapped to 409. *Evidence:* `registration_service.py:374-390`. *Confidence: high.*
- **B3** `student_subjects` derive strictly from `program_subjects`. *Evidence:* `registration_service.py` step 7 ("only real mappings, never the bare subjects"). *Confidence: high.*
- **B4** Catalog `pathway`/`level` filters go through `pathway_levels`, never name matching. *Evidence:* `catalog_repository.py:94-98`. *Confidence: high.*

### C. VERIFIED BUSINESS RULES
- **C1** One enrollment per student per academic year (app pre-check + DB UNIQUE). *Confidence: high.*
- **C2** Year must be `planned`/`active`; closed years refuse with 503. *Evidence:* `_validate_year_registrable`. *Confidence: high.*
- **C3** School offering requires a real `school_programs` row (422 otherwise). *Evidence:* `_validate_school_offers`. *Confidence: high.*
- **C4** TVET program versions require a `tvet_programs.sector` (503 otherwise). *Evidence:* `_validate_tvet_profile`. *Confidence: high.*
- **C5** Role is fixed server-side at public registration. *Evidence:* `schemas/auth.py:12-15`. *Confidence: high.*

### D. VERIFIED SECURITY CONTROLS
See §22 table — 16 controls verified.

### E. CONTRACTS
- **E1 (HIGH)** `program_version_id` carries `school_programs.id`. *Evidence:* `RegisterWizard.jsx:112` + `schemas/catalog.py:145-157` + `registration_service.py:233-250`. *Impact:* combination step always 404s. *Confidence: high.*
- **E2 (HIGH)** Seed state blocks registration entirely (`ready=False`; closed year). *Confidence: high.*
- **E3 (HIGH)** `dist/` bakes `127.0.0.1:8000`. *Confidence: high.*
- **E4 (MEDIUM)** `verify_database.EXPECTED_TABLES=17` vs 20. *Confidence: high.*
- **E5 (MEDIUM)** `conftest.APPLICATION_TABLES=17` vs 20. *Confidence: high.*
- **E6 (MEDIUM)** Alembic `env.py` omits `naming_convention` → index-name drift. *Confidence: medium-high.*
- **E7 (LOW)** No 429 handling; dead Network-Error branch. *Confidence: high.*

### F. ARCHITECTURAL RISKS
- **F1 (CRITICAL)** `backend/` is **0% tracked**; `src/components/student/`, `src/lib/api.js`, `src/services/{auth,catalog,registrations}.js` also untracked; 134 dirty paths on HEAD `dc8b91c`. *Impact:* a fresh clone or any `git clean -fd` **destroys the entire backend and the whole student portal** with no recovery. *Confidence: high.*
- **F2 (HIGH)** No CI/CD, no containers, no lint, no typecheck — nothing enforces any contract. *Confidence: high.*
- **F3 (HIGH)** Two auth systems + two admin consoles with different trust models. *Confidence: high.*
- **F4 (MEDIUM)** No pagination on any list endpoint; unbounded queries will degrade as data grows. *Confidence: high.*
- **F5 (MEDIUM)** Session/ reset-token cleanup is a manual script. *Confidence: high.*
- **F6 (MEDIUM)** `.env` resolved CWD-relative; wrong CWD silently yields defaults. *Confidence: high.*

### G. SECURITY RISKS
G1–G10 as enumerated in §22 (G1 resend missing, G2 rate-limit keying, G3 unloginable admin accounts, G4 unthrottled catalog, G5 localStorage JWTs, G6 dual auth surfaces, G7 existence oracle, G8 unguarded reset SQL, G9 inconsistent `extra` policy, G10 write-only free-text audit).

### H. DATA INTEGRITY RISKS
- **H1** `update_student_profile` maps any `IntegrityError` → 422, masking real faults. *Confidence: high.*
- **H2** `get_db` relies on implicit rollback at `close()`. *Confidence: high.*
- **H3** `users.password_hash` is nullable, so a passwordless row is representable (feeds G3). *Confidence: high.*
- **H4** `auth_events.event_type` unconstrained; no reader → audit trail is unverifiable. *Confidence: high.*
- **H5** Firestore writes have no owner check (`firebase.rules`) → any signed-in user can modify any letter/echo. *Confidence: high (from prior audit, rules file still present).*

### I. PERFORMANCE RISKS
- **I1** Catalog endpoints unthrottled + `selectinload` chains + no pagination (G4). *Confidence: high.*
- **I2** `list_program_versions` joins 4 tables then `selectinload`s `program` — fine at seed scale, unbounded at production scale. *Confidence: medium.*
- **I3** Rate limiter is in-memory → O(1) but per-process; no shared store. *Confidence: high.*
- **I4** Desktop: infinite 3 s Gemini reconnect loop with no backoff → burns quota. *Confidence: high.*

### J. MAINTAINABILITY RISKS
- **J1** `ui.py` = 5,176 lines / 222 KB single module. *Confidence: high.*
- **J2** No lint/typecheck in any of the three products. *Confidence: high.*
- **J3** `requirements.txt` unpinned (desktop); backend pinned only in `.venv` state, not lockfile. *Confidence: high.*
- **J4** 16 stale audit documents in `docs/system-analysis/` that a new developer will reasonably trust. *Confidence: high.*
- **J5** Backend naming-convention metadata declared in `Base` but not in Alembic → every future autogenerate produces noise. *Confidence: medium-high.*

### K. DEAD/DUPLICATE CODE
See §24 (10 backend items, 3 frontend items, 8 repo-wide items, 3 duplicate structures).

### L. DOCUMENTATION MISMATCHES
See §25 (9 mismatches).

### M. UNKNOWN / UNVERIFIED AREAS
See §27.

---

# 27. Unknown / Unverified Areas

**Explicitly UNKNOWN — not guessed:**

1. **Whether PostgreSQL is currently reachable / what data it contains.** No liveness check was performed (read-only mandate; running `verify_database --live` would open a DB connection). The seeded-row counts (40) come from `app/data/*.py` **source**, not from a live query.
2. **Whether `alembic upgrade head` has actually been run** against the local database. `alembic_version` was not inspected.
3. **Whether `backend/.env` contains production-grade values.** Only `.env.example` **key names** were read; no values were opened or printed.
4. **`git add`-ability / intent of the untracked backend.** Whether `backend/` is untracked deliberately (work-in-progress) or by accident is unknowable from code.
5. **Desktop files never read in full:** `ui.py` (~2,300 of 5,176 lines read in a prior audit; the remainder is widget painting/diagram drawing), `core/student_mode.py`, `core/curriculum_nodes.py`, most of `memory/memory_manager.py`, all 15 root `scripts/*.py`, `data/**` (2,265 files, sampled only).
6. **Letters/Firebase runtime behaviour** (`MapCanvas`, `LetterOverlay`, `Journal`, `AdminWorkspace` internals) — only structure and auth model traced.
7. **`backend/README.md` lines 161–299 and 361–449** were sampled, not read line-by-line.
8. **Whether `resend` would work if installed** — no network call attempted.
9. **Actual autogenerate diff** for the naming-convention drift (C6) — reading migrations + models is evidence; running autogenerate is a state change and was not done.
10. **Any runtime performance characteristic** (no profiling, no load test).
11. **Whether the `students.pyc` orphan** corresponds to a deleted module still referenced anywhere — grep found **0 references** to a `students` endpoint module; the source is gone.
12. **`docs/system-analysis/*` internal claims** were checked only where they contradict current code; the remaining ~14 documents were not line-verified.

**Assumptions remaining (stated, not verified):**
- A1: The 40-row seed counts reflect `alembic upgrade head` + `seed_reference_data.py` output, assuming both were run on the local DB.
- A2: Python 3.14.2 in `.venv` matches the interpreter used to produce the `cpython-314` bytecode.
- A3: `role` values beyond the three in `UserRole` cannot exist post-migration 0005.

**Most important files for future implementation:**
| Priority | File |
|---|---|
| 1 | `backend/app/services/registration_service.py` — all enrollment rules; the fix target for E1 |
| 2 | `backend/app/api/v1/endpoints/registrations.py` — commit/rollback + error mapping |
| 3 | `src/components/student/RegisterWizard.jsx` — the client half of E1 |
| 4 | `backend/app/core/auth_dependencies.py` + `core/security.py` — the auth boundary |
| 5 | `backend/app/services/auth_service.py` — largest service, owns sessions/reset |
| 6 | `backend/app/models/enrollment.py` + `models/__init__.py` — the schema truth |
| 7 | `backend/app/main.py` — middleware, handlers, CORS |
| 8 | `backend/app/data/*` + `scripts/seed_reference_data.py` — unblocks E2 |
| 9 | `backend/alembic/env.py` + `scripts/verify_database.py` — E4/E6 |
| 10 | `src/lib/api.js` — token lifecycle for every client call |
| 11 | `backend/tests/conftest.py` — fixture correctness (E5) |
| 12 | `backend/README.md` — the only accurate backend doc |

---

# 28. MASTER SYSTEM MAP

```
APPLICATIONS
├─ A1 SuperTeacher Desktop (RINA)          main.py
├─ A2 Letters Web App + Student Portal     package.json / index.html
└─ A3 SuperTeacher API                     backend/            ★ UNTRACKED
        │
ENTRY POINTS
├─ main.py ──────────────► ui.JarvisUI (QApplication)
├─ src/main.jsx ─────────► src/App.jsx  ──► /register | /admin | default
└─ backend/app/main.py ──► uvicorn app.main:app :8000
        │
MODULES
├─ A1: core/*, memory/*, data/*, assets/*, scripts/*
├─ A2: src/components/{PublicMap,Admin,student}/, src/services/, src/lib/
└─ A3: api/v1 | core | models | schemas | repositories | services | data | alembic | scripts | tests
        │
API  (FastAPI, prefix /api/v1, 35 routes, 24 rate-limited)
├─ public   : /health · /auth/{register,login,refresh,forgot-password,reset-password}
│            /registrations/readiness · /catalog/* (10)
├─ any role : /auth/logout · /auth/me · /me · /me/change-password · /me/deactivate
├─ student  : /me/student (GET,POST,PATCH) · /me/registrations (POST,GET,GET/{id})
└─ admin    : /admin/students (POST,GET,PATCH) · /admin/students/{id}/history
              /admin/registrations (POST,GET/{id}) · /admin/students/{id}/registrations
        │
AUTH  ── trust boundary #1 ──
│  HTTPBearer → decode_token(typ=access,HS256) → DB user lookup → status ACTIVE
│  → require_{student,teacher,admin} (role from DB, never from JWT)
│  refresh: JWT + auth_sessions row (SHA-256 digest, rotation, cap, revocable)
│  Identity NEVER taken from request body on /me/* routes
        │
BUSINESS LOGIC
│  auth_service (register/login/rotate/revoke/reset)
│  registration_service (8-step validate → create → derive subjects)
│  student_service (profile CRUD + history) · catalog_service (reads, 404)
        │
DATA ACCESS  (flush only; commit belongs to the endpoint)
│  user_repo · student_repo · enrollment_repo · catalog_repo
│  auth_session_repo · auth_event_repo · student_profile_history_repo
        │
DATABASE  ── trust boundary #2 ──
│  PostgreSQL via psycopg 3 · SQLAlchemy 2 · Alembic head=0005
│  20 tables: users, auth_sessions, password_reset_tokens, auth_events,
│             students, student_profile_history,
│             academic_years, pathways, education_levels, pathway_levels,
│             programs, program_versions, program_subjects, subjects,
│             tvet_sectors, tvet_programs, schools, school_programs,
│             student_enrollments, student_subjects
        │
EXTERNAL SERVICES
├─ resend (email)          ✗ NOT INSTALLED — reset mail never sends
├─ Gemini Live (desktop)   ✓ active
├─ OpenRouter (desktop)    ✗ missing or_client module
├─ Firebase (Letters)      ✓ active — firestore.rules / storage.rules
└─ (backend: NO queues, NO cache, NO storage, NO webhooks)

FRONTEND → BACKEND : fetch + Bearer, base VITE_API_URL (fallback 127.0.0.1:8000)
BACKEND  → DATABASE : psycopg 3, pool_pre_ping
BACKEND  → EXTERNAL : resend only (and it is missing)

BACKGROUND WORKERS : none (backend) · asyncio+threads (desktop) · manual cleanup script
CI/CD              : NONE            DEPLOYMENT : NONE (no Docker, no YAML, no .github)
AUTH BOUNDARIES    : Firebase (A2 admin/letters)  ≠  FastAPI JWT (A2 /register → A3)
TRUST BOUNDARIES   : browser↔API (CORS+Bearer) · API↔PostgreSQL (.env) ·
                     desktop↔Gemini/OpenRouter (config/api_keys.json) · web↔Firebase (rules)
```

---

# 29. MASTER DATAFLOW

```
════════ FLOW 1: ACCOUNT ════════
SignupFlow.jsx ──POST /auth/register──▶ pydantic StudentAccountCreate
   (password policy, name/gender/country validators, DOB plausibility)
   ──▶ auth_service.register_student_account
        ├─ user_repo.create_student_user (role=student, Argon2id)
        ├─ student_repo.create
        ├─ history_repo.log_change("create")
        └─ _issue_session (session cap → auth_sessions row w/ SHA-256 digest)
   ──▶ endpoint session.commit() ──▶ 201 TokenResponse
   ──▶ saveTokens() → localStorage {st_access_token, st_refresh_token}

════════ FLOW 2: LOGIN / REFRESH ════════
login ──▶ authenticate() ── 1 generic 401 ──▶ _issue_session ──▶ 201 TokenResponse
refresh ──▶ digest lookup ── used/revoked/expired? 401
         ──▶ row locked → mark used → new session + new pair (rotation) ──▶ commit
every protected request ──▶ get_current_user ── DB read ── status must be ACTIVE

════════ FLOW 3: READINESS (what the wizard sees) ════════
GET /registrations/readiness (public, 30/min)
   ──▶ find open academic year (status ∈ planned|active)
   ──▶ count schools / program_versions / school_programs
   ──▶ {ready:false, open_academic_year:null, blocked_reasons:[...]}
   ──▶ RegisterWizard.jsx:132 → UnavailableNotice → STOP   ◄── CURRENT STATE

════════ FLOW 4: REGISTRATION (happy path) ════════
Wizard steps 1-4 ──POST /me/registrations──▶ RegistrationCreateSelf (extra=forbid)
   ──▶ get_current_student (Bearer → DB user → role=student → profile exists)
   ──▶ RegistrationCreate(student_id = TOKEN identity)
   ──▶ registration_service.register_student
        ① student 404   ② year 404 → status check 503
        ③ pathway 404 + level 404 + pathway_levels row 422
        ④ school 404    ⑤ duplicate 409
        ⑥ program_version 404 ◄── E1 MISMATCH
              → tuple match 422 → status active 422
              → TVET sector 503 → school_programs row 422
        ⑦ INSERT student_enrollments (pending)   UNIQUE race → 409
        ⑧ INSERT student_subjects ← program_subjects
   ──▶ endpoint session.commit() ──▶ 201 RegistrationRead
   ──▶ RegistrationSuccess (reference number)

════════ FLOW 5: ADMIN ════════
require_admin (role from DB) ──▶ POST /admin/students (explicit student_id)
                                ──▶ POST /admin/registrations (explicit student_id)

════════ FLOW 6: DESKTOP ════════
JarvisUI ⇄ Gemini Live ──tool call──▶ student_mode allowlist
   ──▶ curriculum_loader (lazy tree, LRU, traversal guard) ──▶ response
   ──▶ memory_manager.extract_memory ──▶ ✗ missing or_client (swallowed)

════════ FLOW 7: LETTERS ════════
React ──onSnapshot──▶ Firestore ──▶ firebase.rules (no owner check)
```

---

# 30. MASTER EXECUTION PIPELINE

### Backend cold start
```
$ cd backend && python -m uvicorn app.main:app --reload --port 8000
  1. import app.main
  2. get_settings()            ← reads backend/.env (CWD-relative)
  3. import app.core.database  → create_engine(...)      [lazy connect]
  4. import app.api.v1.router  → imports 20 models onto Base.metadata
  5. build FastAPI(): security headers → CORS → limiter state
  6. register handlers: ProfileError, RateLimitExceeded, Exception
  7. custom_openapi caches schema w/ HTTPBearer
  8. include_router(prefix="/api/v1")
  9. NO lifespan · NO create_all · NO seed · NO health probe
 10. listening
```

### Per-request pipeline
```
socket
 → _security_headers middleware
 → CORSMiddleware (origin allow-list, methods GET/POST/PATCH)
 → slowapi key_func(request.client.host) → limit check → 429
 → route match (registration order = router.py include order)
 → HTTPBearer(auto_error=False)                [if protected]
 → pydantic request validation                  → 422
 → get_current_user → DB SELECT users           → 401
 → require_role / get_current_student           → 403
 → handler:
      service/repo (SELECTs + flush)
      on *Error → session.rollback() → HTTPException
      on success → session.commit()
 → get_db → Session.close()                     [implicit rollback if open]
 → response JSON + security headers
 → on any uncaught → logger.exception + {"detail":"Internal server error"}
```

### Registration write pipeline (atomicity)
```
BEGIN ─ resolve (6 SELECTs) ─ INSERT student_enrollments ─ [INSERT student_subjects]
   ├─ any RegistrationError → ROLLBACK → HTTPException
   └─ success → COMMIT → 201
   (UNIQUE (student_id, academic_year_id) is the final race arbiter → 409)
```

### Schema pipeline
```
edit app/models/*.py
 → python -m alembic revision --autogenerate   (⚠ naming_convention not passed → noisy diff)
 → python -m alembic upgrade head
 → python scripts/verify_database.py [--live]  (⚠ EXPECTED_TABLES=17 → cannot pass)
 → python scripts/seed_reference_data.py       (idempotent upserts, prod-guarded)
```

### Test pipeline (manual only — no CI)
```
python -m pytest             # 252 unit, SQLite
python -m pytest -m integration   # 131 tests, requires PostgreSQL w/ schema
python -m pytest -m ""        # all 383
```

### Client build pipeline
```
npm run dev   → Vite dev server, VITE_API_URL from .env
npm run build → dist/ (⚠ 127.0.0.1:8000 baked in if VITE_API_URL unset)
```

---

# 31. Recommended Reading Order for Future Development

**Read in this order — each file assumes the previous ones.**

| # | File | Why |
|---|---|---|
| 1 | `backend/README.md` (lines 1–160, 300–360, 440–520) | The only accurate map of intent and commands. Skip lines 19, 283, 320, 355 (stale — §25). |
| 2 | `backend/app/main.py` | 5.7 KB — the whole app assembly: middleware order, handlers, CORS, prod gating. Everything else hangs off this. |
| 3 | `backend/app/core/config.py` | 24 settings, prod/dev switches, unsafe-secret guard. Defines what "production" means here. |
| 4 | `backend/app/core/database.py` + `app/models/__init__.py` | `Base` naming convention, pool, `get_db`, and the 20-table registry (the schema truth). |
| 5 | `backend/app/models/enums.py` → `user.py` → `student.py` → `enrollment.py` | Vocabulary and the three identity tables before anything reads them. |
| 6 | `backend/app/core/security.py` + `app/core/auth_dependencies.py` | Argon2id, HS256 `typ`, and the Bearer → DB → role chain. **Read before touching any endpoint.** |
| 7 | `backend/app/api/v1/router.py` | 11 lines that define the entire URL surface and include order. |
| 8 | `backend/app/schemas/registration.py` | `RegistrationCreate` vs `RegistrationCreateSelf` — why `student_id` is absent on the student route. |
| 9 | `backend/app/services/registration_service.py` | The 8-step enrollment rule set (316+) plus the 8 `_resolve_*`/`_validate_*` helpers. **Core domain file.** |
| 10 | `backend/app/api/v1/endpoints/registrations.py` | The commit/rollback discipline and `RegistrationError` → HTTP mapping. |
| 11 | `src/components/student/RegisterWizard.jsx` + `src/lib/api.js` | The client half of contract **E1** — read together with #9. |
| 12 | `backend/app/schemas/catalog.py` (`SchoolProgramRead`, line 145) | The other half of **E1**: `id` ≠ `program_version_id`. |
| 13 | `backend/app/services/auth_service.py` | 23 KB, the largest service: register/login/rotate/revoke/reset/session-cap. |
| 14 | `backend/app/api/v1/endpoints/auth.py` | 13 routes across three routers; the role-namespace split. |
| 15 | `backend/app/repositories/catalog_repository.py` | Ordering contract + "filter through real relationships, never names" (lines 9–24, 94–98, 134–143, 241–261). |
| 16 | `backend/alembic/versions/0001..0005` + `alembic/env.py` | How the 20 tables actually came to be — and the missing `naming_convention` (E6). |
| 17 | `backend/app/data/pathway_levels.py` + `education_levels.py` | Where S1–S3 / S4–S6 / L3–L5 actually live: **rows, not code**. |
| 18 | `backend/scripts/seed_reference_data.py` + `verify_database.py` | How to unblock **E2** and why the checker can never pass (**E4**). |
| 19 | `backend/tests/conftest.py` + one unit + one integration test | The fixture model, and the `APPLICATION_TABLES=17` gap (**E5**). |
| 20 | `src/App.jsx`, `src/components/student/StudentPortal.jsx`, `src/services/*` | Client routing, session restore, and the three thin service modules. |
| 21 | `backend/app/services/student_service.py` + `schemas/student_profile.py` | Profile CRUD, `UNSET` sentinel, gender/country/name validators, DB CHECK mirroring. |
| 22 | `backend/app/core/rate_limit.py` + a sample `@limiter.limit` | The keying problem (**G2**) and per-route limits. |
| 23 | `backend/.env.example` **key names only** | The full configuration surface (24 keys). |
| 24 | `docs/system-analysis/EXECUTIVE-SUMMARY.md` + `FILE-COVERAGE.md` | Useful for products 1–2 only; **dated 2026-09-10, predates `backend/`**. |
| 25 | `main.py` + `ui.py` (class `JarvisUI`, ui.py:4489) | Only if working on the desktop app — note `ui.py` is 5,176 lines and `memory/memory_manager.py` imports a missing `or_client`. |

### What to fix first (if/when implementation is authorized)
1. **F1** — commit `backend/` and the student-portal sources (highest severity: unrecoverable data loss risk).
2. **E1** — `RegisterWizard.jsx:112` → `programVersion.program_version_id`.
3. **E2** — seed an open academic year + schools/programs/program_versions/school_programs.
4. **G1** — install `resend` (or make failure visible to the user).
5. **G2** — add proxy-header trust to the rate-limiter key function.
6. **E4/E5/E6** — align `EXPECTED_TABLES`, `APPLICATION_TABLES`, and Alembic `naming_convention` with the real 20 tables.

---

### Scope statement (per the operating rule)

- **Inspected:** full repo tree; 100% of `backend/app/**/*.py` (82 source files) read directly or via audited subagent reports; all 5 migrations + `alembic/env.py`; all 3 script entry points; all 21 test files (structure + counts); `backend/README.md` (sampled 1–160, 300–360, 440–520 + claim greps); root `main.py` imports, `ui.py` key symbols; `src/` (all 23 files, `student/*` + `lib/api.js` + `services/*` read in full); `package.json`, `vite.config.js`, `firebase.rules`, `storage.rules`, both `.env.example` files, both `.gitignore`s; `docs/system-analysis/` (16 files, claim-checked); git index (2707 tracked files, 134 dirty paths).
- **Verified:** 35-endpoint surface + 24 rate limits; 20-table schema; 5 migrations; 383 tests; the full registration and account-creation traces; contract mismatches E1–E7; risks G1–G10; the absence of CI/Docker/lint; `resend` absence; the `students.pyc` orphan.
- **Unknown:** live DB state, `.env` values, whether migrations were applied, desktop files never opened, 14 unverified legacy audit documents, runtime performance.
- **Assumptions:** seed-row counts reflect source + documented seeder run; `cpython-314` bytecode matches the `.venv` interpreter.
- **No files were created, modified, deleted, or installed during this audit.**

---

## 6. You

create a file in this project and name it chat chat and save all this converstaion of me and you in it so that later i can come and analyse your response carefully, so tell me how are you going to do it so the after we can agree then we excute.

---

## 7. Assistant

## Plan: save this conversation to a file

**How I'd do it** — extract it verbatim rather than retype it. I already recovered the original prompt earlier by reading opencode's local session store (`~/.local/share/opencode/opencode.db`, tables `message`/`part`, session `ses_f17474d5bffeKdXpkJVQ7PmaBO`). Same method gives the whole conversation.

**Steps (after you approve):**

1. Run an **inline Python snippet** (no script file left behind) that opens `opencode.db` read-only, pulls every `part` row for this session in chronological order, keeps `role=user` and `role=assistant` text, and drops tool-call/tool-result payloads.
2. Format as Markdown: header (date, session id, git HEAD `dc8b91c`), then `## You` / `## Assistant` blocks — so the 31-section report renders and is diffable/searchable later.
3. Write to the agreed path in the project.
4. Verify: read the file back, report byte size, line count, and confirm no other file was touched.

**Why not retype:** my report alone is ~12k words and I'd risk paraphrasing it; extraction is exact.

---

**Trade-off you should weigh:** tool-call results (the file listings, greps, DB dumps) are 100s of KB of noise. Dropping them keeps the file readable for analysis; keeping them makes it a complete audit trail.

---

**Questions before we execute:**

1. **Filename** — `chat chat.md` literally as written (space included), or `chat-chat.md` / `chat_chat.md`? Spaces work but are awkward in shells/scripts.
2. **Depth** — (a) user + assistant text only *(recommended, clean to analyse)*, (b) include tool calls/results too *(full audit trail, very large)*, or (c) text-only file + a separate raw dump?
3. **Location** — project root, or `docs/`?
4. **Git** — the repo already has 134 dirty paths. Should this file be **tracked** (committable) or added to `.gitignore` so it stays local?

---

## 8. You

do it

---

## 9. Assistant

Rebuilding with correct per-message labels (found one exchange lost to session compaction):

---

*End of log. Messages after this point are not included.*
