"""SQLAlchemy ORM models.

Importing this package registers every table on
``app.core.database.Base.metadata``, which Alembic consumes via
``backend/alembic/env.py``. Keep the imports exhaustive.
"""
from app.core.database import Base
from app.models.academic_year import AcademicYear
from app.models.auth_event import AuthEvent
from app.models.auth_session import AuthSession
from app.models.education_level import EducationLevel
from app.models.enrollment import StudentEnrollment
from app.models.password_reset_token import PasswordResetToken
from app.models.pathway import Pathway
from app.models.pathway_level import PathwayLevel
from app.models.program import Program
from app.models.program_subject import ProgramSubject
from app.models.program_version import ProgramVersion
from app.models.school import School
from app.models.school_program import SchoolProgram
from app.models.student import Student
from app.models.student_profile_history import StudentProfileHistory
from app.models.student_subject import StudentSubject
from app.models.subject import Subject
from app.models.tvet_program import TVETProgram
from app.models.tvet_sector import TVETSector
from app.models.user import User

__all__ = [
    "Base",
    "AcademicYear",
    "AuthEvent",
    "AuthSession",
    "EducationLevel",
    "PasswordResetToken",
    "StudentEnrollment",
    "Pathway",
    "PathwayLevel",
    "Program",
    "ProgramSubject",
    "ProgramVersion",
    "School",
    "SchoolProgram",
    "Student",
    "StudentProfileHistory",
    "StudentSubject",
    "Subject",
    "TVETProgram",
    "TVETSector",
    "User",
]
