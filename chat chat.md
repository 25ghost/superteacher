Final report — Authentication & Authorization MVP (Phase B)
Endpoint matrix as built
PUBLIC (no token)
GET    /api/v1/health                          rate-limited (RATE_LIMIT_HEALTH=120/min)
POST   /api/v1/auth/register                   student account + tokens (role fixed server-side)
POST   /api/v1/auth/login                      lockout: 5 refused → 15 min, generic 401
POST   /api/v1/auth/refresh                    rotation; replay refused
POST   /api/v1/auth/forgot-password            204 always (no enumeration)
POST   /api/v1/auth/reset-password             single-use token → password + sessions revoked
POST   /api/v1/auth/accept-invite              teacher invite token → active + token pair
GET    /api/v1/registrations/readiness
GET    /api/v1/catalog/*                       academic-years|pathways|education-levels|subjects|
                                               programs|program-versions|tvet/*|schools(+/{code}/programs)

ANY AUTHENTICATED ROLE
POST   /api/v1/auth/logout                     Bearer + refresh; audited (IP + User-Agent)
GET    /api/v1/me          GET /api/v1/auth/me identity from token only
POST   /api/v1/me/change-password              204
POST   /api/v1/me/deactivate                   204 → DB-authoritative status gate

role=student
GET|POST|PATCH /api/v1/me/student              missing profile → 403 + remedy pointer
GET|POST /api/v1/me/registrations              no student_id field (spoof = 422)
GET    /api/v1/me/registrations/{id}           owner only, else 403

role=teacher
GET    /api/v1/me/teacher                      own profile; 404 when none exists
PATCH  /api/v1/me/teacher                      full_name|phone|subject only, extra=forbid

role=admin
POST|GET /api/v1/admin/teachers                create pending + invite email; paginated list
POST   /api/v1/admin/teachers/{id}/invite|activate|deactivate
PATCH  /api/v1/admin/users/{id}/role           sole role-choosing route; self-change 403
POST   /api/v1/admin/users/{id}/unlock         clear lockout; 409 when not locked
POST|GET|PATCH /api/v1/admin/students[...]  +  .../history + /registrations (3 routes)
Cross-role guarantees are pinned by tests/unit/test_endpoint_role_matrix.py: anonymous → 401 on all 27 protected routes, wrong role → 403 before the handler (administrator role required… / teacher role required…), each role reaches exactly its namespace; public routes never answer 401/403.
Slices delivered (test-first, full suite + diff each time)
1 CLI admin bootstrap → 2 config fail-fast → 3 teachers table + pending → 4 admin user management (invite/activate/deactivate/role) → 5 invite acceptance + teacher profile (/me/teacher) → 6 cross-role matrix + named teacher 403 → 7 lockout + IP/UA audit + /health throttle → 8 README matrix. Suite: 273 → 447 passed, 151 deselected.
Assumptions on record (▲)
Admin bootstrap = CLI only (scripts/create_admin.py); teachers = admin-created, pending until accept-invite; role change = PATCH /admin/users/{id}/role revoking sessions; lockout 5/15 min with admin unlock; email_verified deferred; no new deps beyond stdlib + argon2, PyJWT, resend, slowapi.
Deferred / blocked
- Integration tests vs test DB are skipped (4 passed / 147 skipped): backend/.env has empty DB_PASSWORD and postgres refuses passwordless connections. Set DB_PASSWORD, CREATE DATABASE super_teacher_db_test, run alembic upgrade head against it, then pytest -m integration — that is the last unmet DoD item.
- scripts/verify_database.py still fails structurally at baseline (pre-existing drift in 0003/0004-era parsing); only its EXPECTED_REVISION/table expectations were kept in sync (now 0008).
- No lockout-listing endpoint for admins (unlock works by id); lockout does not block refresh with an already-issued token; email_verified and Resend delivery in real environments remain untested.