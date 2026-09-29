"""API v1 router — aggregates all v1 endpoint routers.

Role-split layout (no endpoint serves two roles):

- ``auth.router`` / ``auth.me_router`` — public auth + generic self-service,
- ``auth.student_me_router`` — student self-service (`/me/student`),
- ``admin_students.router`` — administration (`/admin/students`),
- ``registrations.router`` / ``registrations.me_router`` — public readiness
  + student self-service (`/me/registrations`),
- ``registrations.admin_router`` — administration (`/admin/registrations`),
- catalog, health, registrations listing under ``/me`` (unchanged).
"""
from fastapi import APIRouter

from app.api.v1.endpoints import (
    admin_students,
    auth,
    catalog,
    health,
    registrations,
)

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(auth.me_router)
api_router.include_router(auth.student_me_router)
api_router.include_router(catalog.router)
api_router.include_router(admin_students.router)
api_router.include_router(registrations.router)
api_router.include_router(registrations.me_router)
api_router.include_router(registrations.admin_router)
