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
    """Account lifecycle, enforced by ``users_status_check``.

    ``pending`` was added for the teacher invite flow: an account created by
    an administrator exists but cannot authenticate until the invite is
    accepted (or an administrator activates it). Only ``active`` passes the
    status gate in :mod:`app.core.auth_dependencies`.
    """

    ACTIVE = "active"
    PENDING = "pending"
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


class TeacherVerificationStatus(str, enum.Enum):
    """Teacher vetting state, independent of the account status.

    ``teachers.verification_status`` is a separate axis from
    ``users.status``: an account can be active (able to log in) while its
    profile is still ``pending`` an administrator's decision. Only
    ``approved`` teachers may publish teaching offerings. Enforced by the
    ``teachers_verification_status_check`` database CHECK.
    """

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    SUSPENDED = "suspended"


class TeachingOfferingStatus(str, enum.Enum):
    """Lifecycle of one teacher's offering of a learning context.

    ``active`` offerings are the only ones the marketplace shows and the
    only ones a teacher may hold at a time for a given context (the partial
    unique index ``uq_teaching_offerings_teacher_context_active_key``).
    """

    ACTIVE = "active"
    PAUSED = "paused"
    ARCHIVED = "archived"


class LearningEnrollmentStatus(str, enum.Enum):
    """A student's membership of one teaching offering.

    ``active`` → ``ended`` only, through ``POST /me/learning-enrollments/
    {id}/leave``: a student holds at most one active enrollment per
    learning context (partial unique index), so leaving is the explicit
    step before joining another offering of the same context.
    """

    ACTIVE = "active"
    ENDED = "ended"


class MaterialType(str, enum.Enum):
    """What kind of teaching material a ``materials`` row describes.

    The MVP accepts exactly two kinds of teacher-uploaded study content:

    - ``video`` — a study video (mp4/webm/quicktime);
    - ``pdf_document`` — a real PDF document. Books, notes and worksheets
      are simply PDFs with their own title and lesson placement; there is
      no separate book/note/exercise row, table or upload category.

    Photos and other images are never teaching material. Assignments are a
    later domain and must never appear here. Enforced by
    ``materials_material_type_check`` and cross-checked against the stored
    file's content type by the upload pipeline.
    """

    VIDEO = "video"
    PDF_DOCUMENT = "pdf_document"


class MaterialStatus(str, enum.Enum):
    """Moderation lifecycle of one teacher-authored material.

    DRAFT → PENDING_REVIEW → (PUBLISHED | REJECTED) → ARCHIVED. Only an
    administrator approves or rejects; a teacher cannot publish directly.
    A rejected material returns to DRAFT through the revision flow before
    it may be submitted again. Published materials are effectively
    immutable in the MVP: any change is a new revision (slice 2C). The
    ``published → archived`` step is the retirement path. Enforced by
    ``materials_status_check``.
    """

    DRAFT = "draft"
    PENDING_REVIEW = "pending_review"
    REJECTED = "rejected"
    PUBLISHED = "published"
    ARCHIVED = "archived"


class FileAssetStatus(str, enum.Enum):
    """Upload/validation pipeline state for one stored file asset.

    UPLOAD → VALIDATING → (INVALID | VALID) → STORED. ``INVALID`` is a
    terminal refusal (size, content type or magic-bytes check failed);
    the teacher must upload a different file. No malware-scanning
    infrastructure is bundled — this is the abstraction point for one.
    Enforced by ``file_assets_validation_status_check``.
    """

    UPLOAD = "upload"
    VALIDATING = "validating"
    INVALID = "invalid"
    VALID = "valid"
    STORED = "stored"


class MaterialProgressStatus(str, enum.Enum):
    """A student's progress on ONE material (slice 2C, basic progress).

    Progress belongs to ``(student, material)``, never to the material
    itself. Transitions are forward-only: not_started → in_progress →
    completed. Setting the same status again is idempotent (200); moving
    backwards is a 409. Enforced by ``material_progress_status_check``.
    """

    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


class OnlineClassStatus(str, enum.Enum):
    """Lifecycle of one live online class session (Phase 3).

    ``scheduled`` → ``live`` → ``ended`` and ``scheduled`` → ``cancelled``
    only; both ``ended`` and ``cancelled`` are terminal and a class is
    never reopened. A class is the classroom unit a WebSocket classroom
    binds to — messages, presence and attendance exist only while the
    status is ``live``. Enforced by ``online_class_sessions_status_check``
    plus the service's transition map.
    """

    SCHEDULED = "scheduled"
    LIVE = "live"
    ENDED = "ended"
    CANCELLED = "cancelled"
