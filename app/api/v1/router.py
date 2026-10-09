"""API v1 router — aggregates all v1 endpoint routers.

Role-split layout (no endpoint serves two roles):

- ``auth.router`` / ``auth.me_router`` — public auth + generic self-service,
- ``auth.student_me_router`` — student self-service (`/me/student`),
- ``auth.teacher_me_router`` — teacher self-service (`/me/teacher`),
- ``teacher_offerings.router`` — teacher self-service
  (`/me/teacher/offerings`, Phase 1, plus the offering-scoped
  `/offerings/{id}/students` roster: a teacher sees only the students
  who joined their own offering),
- ``teacher_curriculum.router`` — teacher self-service topics/lessons
  under an offering (`/me/teacher/offerings/{id}/topics/...`, Phase 2 2A),
- ``teacher_materials.router`` — teacher self-service teaching materials
  under an offering (`/me/teacher/offerings/{id}/materials/...`, Phase 2 2B),
- ``teacher_classes.router`` — teacher self-service online classes under
  an offering (`/me/teacher/offerings/{id}/classes/...`, Phase 3 3A),
- ``learning_enrollments.router`` — student self-service
  (`/me/learning-enrollments`, Phase 1),
- ``student_content.router`` — student self-service content access
  (`/me/learning-content`, Phase 2, slice 2C),
- ``student_classes.router`` — student self-service online classes
  (`/me/classes`, Phase 3 3A),
- ``marketplace.router`` — student marketplace
  (`/marketplace`, Phase 1),
- ``admin_students.router`` — administration (`/admin/students`),
- ``admin_users.router`` — administration (`/admin/teachers`, `/admin/users`),
- ``admin_materials.router`` — administration (`/admin/materials`, Phase 2 2B),
- ``registrations.router`` / ``registrations.me_router`` — public readiness
  + student self-service (`/me/registrations`),
- ``registrations.admin_router`` — administration (`/admin/registrations`),
- catalog, health, registrations listing under ``/me`` (unchanged).
"""
from fastapi import APIRouter

from app.api.v1.endpoints import (
    admin_materials,
    admin_students,
    admin_users,
    auth,
    catalog,
    health,
    learning_enrollments,
    marketplace,
    registrations,
    student_classes,
    student_content,
    teacher_classes,
    teacher_curriculum,
    teacher_materials,
    teacher_offerings,
)

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(auth.me_router)
api_router.include_router(auth.student_me_router)
api_router.include_router(auth.teacher_me_router)
api_router.include_router(teacher_offerings.router)
api_router.include_router(teacher_curriculum.router)
api_router.include_router(teacher_materials.router)
api_router.include_router(teacher_classes.router)
api_router.include_router(learning_enrollments.router)
api_router.include_router(student_content.router)
api_router.include_router(student_classes.router)
api_router.include_router(marketplace.router)
api_router.include_router(catalog.router)
api_router.include_router(admin_students.router)
api_router.include_router(admin_users.router)
api_router.include_router(admin_materials.router)
api_router.include_router(registrations.router)
api_router.include_router(registrations.me_router)
api_router.include_router(registrations.admin_router)
