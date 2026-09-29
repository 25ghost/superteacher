"""Enumeration values shared by the whole SuperTeacher backend.

The database stores these as plain VARCHAR columns guarded by named CHECK
constraints (see each model). These enums are the single source of truth for
the allowed values; no pathway or level codes are hard-coded here — those are
data (rows), not logic. The same modules also serve future SuperTeacher
modules (Curriculum, Learning, AI Tutor, Progress, school administration).
"""
import enum


def sql_in_list(enum_cls: type[enum.Enum]) -> str:
    """Render an enum's values as a SQL ``IN (...)`` list literal."""
    return ", ".join(repr(member.value) for member in enum_cls)


class UserRole(str, enum.Enum):
    """Authoritative account-role vocabulary for the whole backend.

    Exactly three roles are allowed anywhere in the system:

    - ``student`` — learns; linked 1:1 to a ``students`` profile row;
    - ``teacher`` — teaches;
    - ``admin``   — administrates the platform and the ``/admin/*`` routes.

    This enum is the single source of truth: it drives the
    ``users_role_check`` database CHECK constraint, the JWT ``role`` claim
    and every role guard in :mod:`app.core.auth_dependencies`. No other
    role value may ever be stored on ``users.role``.
    """

    STUDENT = "student"
    TEACHER = "teacher"
    ADMIN = "admin"


class UserStatus(str, enum.Enum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    DISABLED = "disabled"


class RecordStatus(str, enum.Enum):
    """Lifecycle of a reference/catalog row (pathways, levels, programs,
    program versions, subjects, TVET sectors, schools, school offerings).

    ``active`` is the value every catalog table already defaults to;
    ``inactive`` retires a row without deleting it (history is preserved).
    Richer lifecycles (e.g. an academic-year ``closed``/``archived`` state)
    belong to the tables that model them, not to this shared vocabulary.
    """

    ACTIVE = "active"
    INACTIVE = "inactive"


class AcademicYearStatus(str, enum.Enum):
    PLANNED = "planned"
    ACTIVE = "active"
    CLOSED = "closed"
    ARCHIVED = "archived"


class ProgramType(str, enum.Enum):
    COMBINATION = "combination"
    TVET_PROGRAM = "tvet_program"
    STREAM = "stream"
    OTHER = "other"


class ProgramSubjectType(str, enum.Enum):
    CORE = "core"
    ELECTIVE = "elective"
    OPTIONAL = "optional"
    MODULE = "module"


class EnrollmentStatus(str, enum.Enum):
    PENDING = "pending"
    ACTIVE = "active"
    COMPLETED = "completed"
    TRANSFERRED = "transferred"
    WITHDRAWN = "withdrawn"
    CANCELLED = "cancelled"


class StudentSubjectStatus(str, enum.Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    COMPLETED = "completed"
