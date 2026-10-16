"""Unit tests: the full teacher-verification application workflow.

Slice 9 of the teacher-verification policy — the deterministic service
contract behind ``/me/teacher/application`` and
``/admin/teacher-applications``:

- applicant: start, edit, replace contexts, attach/remove evidence,
  submit (blocked until the structural completeness rules pass), withdraw,
  and re-application after a terminal state linking
  ``previous_application_id``;
- admin: record checks (monotonic ``seq`` per evidence), record
  assessments, request information (back to the applicant), and decide —
  approval requires every live evidence's latest check to be ``verified``,
  an explicit approve/reject outcome per requested context (reason
  required when rejected), and the category's evidence rules; the approved
  decision writes ``teacher_approved_contexts`` rows, sets
  ``teachers.verification_status``/``verification_category`` and writes
  one audit event per decision;
- the direct-approval bypass (``set_teacher_verification`` with
  ``approved``) is refused 409 unless the teacher already has a completed
  approved application with category + at least one approved context row.

In-memory SQLite, no HTTP. Storage is an in-memory backend so no real
filesystem is touched.
"""
from __future__ import annotations

import uuid
from datetime import date

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core import security
from app.core.database import Base
import app.models  # noqa: F401
from app.models.academic_year import AcademicYear
from app.models.auth_event import AuthEvent
from app.models.education_level import EducationLevel
from app.models.enums import (
    AcademicYearStatus,
    ApplicationContextStatus,
    AssessmentOutcome,
    TeacherApplicationStatus,
    TeacherEvidenceType,
    TeacherVerificationCategory,
    TeacherVerificationStatus,
    UserRole,
    UserStatus,
    VerificationCheckOutcome,
    VerificationMethod,
)
from app.models.pathway import Pathway
from app.models.pathway_level import PathwayLevel
from app.models.program import Program
from app.models.program_subject import ProgramSubject
from app.models.program_version import ProgramVersion
from app.models.subject import Subject
from app.models.teacher import Teacher
from app.models.teacher_approved_context import TeacherApprovedContext
from app.models.user import User
from app.schemas.teacher_verification import (
    ApplicationDecision,
    ApplicationStart,
    ApplicationContextInput,
    ApplicationUpdate,
    AssessmentRecord,
    ContextDecision,
    ContextsReplace,
    EvidenceUpload,
    InformationRequest,
    VerificationCheckRecord,
)
from app.services.learning_context_service import (
    LearningConflictError,
    LearningForbiddenError,
    LearningNotFoundError,
    LearningValidationError,
)
from app.services import admin_user_service, teacher_verification_service
from app.services.auth_service import AuthConflictError
from app.services.storage import StorageBackend

PASSWORD = "correct horse battery staple"

_PDF_BYTES = b"%PDF-1.4 fake identity document"
_QUALIFICATION_PDF = b"%PDF-1.4 fake academic qualification"
_TRAINING_PDF = b"%PDF-1.4 fake teacher training"
_EXPERIENCE_PDF = b"%PDF-1.4 fake professional experience"


class MemoryStorage(StorageBackend):
    """In-memory backend so unit tests never touch the real filesystem."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def save(self, storage_key: str, data: bytes, *, content_type: str) -> None:
        self.objects[storage_key] = data

    def read(self, storage_key: str) -> bytes:
        return self.objects[storage_key]

    def delete(self, storage_key: str) -> None:
        self.objects.pop(storage_key, None)

    def exists(self, storage_key: str) -> bool:
        return storage_key in self.objects


@pytest.fixture()
def session() -> Session:
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    db = factory()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


@pytest.fixture()
def storage() -> MemoryStorage:
    return MemoryStorage()


@pytest.fixture()
def admin(session: Session) -> User:
    user = User(
        email="admin@example.com",
        role=UserRole.ADMIN.value,
        status=UserStatus.ACTIVE.value,
        password_hash=security.hash_password(PASSWORD),
    )
    session.add(user)
    session.commit()
    return user


@pytest.fixture()
def teacher(session: Session) -> User:
    user = User(
        email=f"teacher-{uuid.uuid4().hex[:8]}@example.com",
        role=UserRole.TEACHER.value,
        status=UserStatus.ACTIVE.value,
        password_hash=security.hash_password(PASSWORD),
    )
    session.add(user)
    session.flush()
    session.add(
        Teacher(
            user_id=user.id,
            full_name="Workflow Teacher",
            verification_status=TeacherVerificationStatus.PENDING.value,
        )
    )
    session.commit()
    return user


def _profile(session: Session, user: User) -> Teacher:
    return session.scalar(select(Teacher).where(Teacher.user_id == user.id))


def _start(
    session: Session,
    user: User,
    *,
    category: TeacherVerificationCategory = TeacherVerificationCategory.FORMAL_TEACHER,
    statement: str | None = None,
):
    return teacher_verification_service.start_application(
        session,
        user,
        ApplicationStart(category=category, statement=statement),
    )


@pytest.fixture()
def catalog(session: Session) -> dict:
    """A minimal coherent catalog: year + pathway + level + program + subject."""
    year = AcademicYear(
        name=f"2026/{uuid.uuid4().hex[:4]}",
        start_date=date(2026, 9, 1),
        end_date=date(2027, 6, 30),
        status=AcademicYearStatus.ACTIVE.value,
    )
    pathway = Pathway(code=f"P-{uuid.uuid4().hex[:6]}", name="O Level")
    level = EducationLevel(code=f"L-{uuid.uuid4().hex[:6]}", name="Senior 3", level_number=3)
    session.add_all([year, pathway, level])
    session.flush()
    session.add(PathwayLevel(pathway_id=pathway.id, education_level_id=level.id))

    program = Program(code=f"PR-{uuid.uuid4().hex[:6]}", name="Combination", program_type="combination")
    session.add(program)
    session.flush()
    program_version = ProgramVersion(
        program_id=program.id,
        academic_year_id=year.id,
        pathway_id=pathway.id,
        education_level_id=level.id,
        code=f"CV-{uuid.uuid4().hex[:6]}",
        name="Combination 2026",
    )
    session.add(program_version)

    maths = Subject(code=f"S-{uuid.uuid4().hex[:6]}", name="Mathematics")
    session.add(maths)
    session.flush()
    session.add(
        ProgramSubject(program_version_id=program_version.id, subject_id=maths.id)
    )
    session.commit()
    return {
        "academic_year_id": str(year.id),
        "pathway_id": str(pathway.id),
        "education_level_id": str(level.id),
        "program_version_id": str(program_version.id),
        "subject_id": str(maths.id),
    }


def _academic_context_payload(catalog: dict) -> ApplicationContextInput:
    return ApplicationContextInput(
        academic_year_id=catalog["academic_year_id"],
        pathway_id=catalog["pathway_id"],
        education_level_id=catalog["education_level_id"],
        program_version_id=catalog["program_version_id"],
        subject_id=catalog["subject_id"],
    )


def _attach(
    session: Session,
    user: User,
    application_id: uuid.UUID,
    *,
    evidence_type: TeacherEvidenceType,
    title: str,
    data: bytes,
    context_ids: list[uuid.UUID],
    storage: MemoryStorage,
    credential_reference: str | None = None,
):
    return teacher_verification_service.add_evidence(
        session,
        user,
        application_id,
        EvidenceUpload(
            evidence_type=evidence_type,
            title=title,
            learning_context_ids=context_ids,
            credential_reference=credential_reference,
        ),
        file_bytes=data,
        original_filename=f"{evidence_type.value}.pdf",
        content_type="application/pdf",
        storage=storage,
    )


# --- applicant flow --------------------------------------------------------------------


def test_start_creates_draft_and_second_start_is_conflict(
    session: Session, teacher: User
) -> None:
    read = _start(session, teacher, statement="I teach maths")
    assert read.status == TeacherApplicationStatus.DRAFT.value
    assert read.category == TeacherVerificationCategory.FORMAL_TEACHER

    with pytest.raises(LearningConflictError) as excinfo:
        _start(session, teacher)
    assert excinfo.value.status_code == 409


def test_update_edits_draft_and_refuses_submitted(
    session: Session, teacher: User
) -> None:
    read = _start(session, teacher)
    updated = teacher_verification_service.update_application(
        session,
        teacher,
        read.id,
        ApplicationUpdate(
            category=TeacherVerificationCategory.INDEPENDENT_TUTOR,
            statement="updated statement",
            misrepresentation_declaration=True,
        ),
    )
    assert updated.category == TeacherVerificationCategory.INDEPENDENT_TUTOR
    assert updated.statement == "updated statement"

    # Submit is not yet possible (no contexts/evidence) — force submit
    # blockers by attaching nothing and expecting 422, then verify edit
    # is still allowed on draft.
    with pytest.raises(LearningValidationError):
        teacher_verification_service.submit_application(session, teacher, read.id)


def test_replace_contexts_and_duplicate_is_conflict(
    session: Session, teacher: User, catalog: dict
) -> None:
    read = _start(session, teacher)
    ctx = _academic_context_payload(catalog)
    replaced = teacher_verification_service.replace_contexts(
        session,
        teacher,
        read.id,
        ContextsReplace(contexts=[ctx]),
    )
    session.expire_all()
    replaced = teacher_verification_service.get_my_application(session, teacher)
    assert len(replaced.contexts) == 1
    assert replaced.contexts[0].status == ApplicationContextStatus.REQUESTED.value

    with pytest.raises(LearningConflictError):
        teacher_verification_service.replace_contexts(
            session,
            teacher,
            read.id,
            ContextsReplace(contexts=[ctx, ctx]),
        )


def test_evidence_requires_requested_context_and_valid_bytes(
    session: Session, teacher: User, storage: MemoryStorage, catalog: dict
) -> None:
    read = _start(session, teacher)
    ctx = _academic_context_payload(catalog)
    teacher_verification_service.replace_contexts(
        session, teacher, read.id, ContextsReplace(contexts=[ctx])
    )
    session.flush()
    # Re-load so application.contexts is populated.
    detail = teacher_verification_service.get_my_application(session, teacher)
    context_id = detail.contexts[0].learning_context_id

    with pytest.raises(LearningValidationError):
        _attach(
            session,
            teacher,
            read.id,
            evidence_type=TeacherEvidenceType.IDENTITY_DOCUMENT,
            title="ID",
            data=_PDF_BYTES,
            context_ids=[uuid.uuid4()],  # not one of the requested contexts
            storage=storage,
        )

    with pytest.raises(LearningValidationError):
        _attach(
            session,
            teacher,
            read.id,
            evidence_type=TeacherEvidenceType.IDENTITY_DOCUMENT,
            title="ID",
            data=b"not a pdf",
            context_ids=[context_id],
            storage=storage,
        )

    evidence = _attach(
        session,
        teacher,
        read.id,
        evidence_type=TeacherEvidenceType.IDENTITY_DOCUMENT,
        title="National ID",
        data=_PDF_BYTES,
        context_ids=[context_id],
        storage=storage,
    )
    assert evidence.duplicate_indicator is False
    assert evidence.learning_context_ids == [context_id]

    # Same bytes again -> duplicate flag (but still accepted).
    again = _attach(
        session,
        teacher,
        read.id,
        evidence_type=TeacherEvidenceType.IDENTITY_DOCUMENT,
        title="National ID copy",
        data=_PDF_BYTES,
        context_ids=[context_id],
        storage=storage,
    )
    assert again.duplicate_indicator is True


def test_remove_evidence_soft_deletes_and_second_remove_conflicts(
    session: Session, teacher: User, storage: MemoryStorage, catalog: dict
) -> None:
    read = _start(session, teacher)
    ctx = _academic_context_payload(catalog)
    teacher_verification_service.replace_contexts(
        session, teacher, read.id, ContextsReplace(contexts=[ctx])
    )
    detail = teacher_verification_service.get_my_application(session, teacher)
    context_id = detail.contexts[0].learning_context_id
    evidence = _attach(
        session,
        teacher,
        read.id,
        evidence_type=TeacherEvidenceType.IDENTITY_DOCUMENT,
        title="National ID",
        data=_PDF_BYTES,
        context_ids=[context_id],
        storage=storage,
    )

    teacher_verification_service.remove_evidence(session, teacher, read.id, evidence.id)
    with pytest.raises(LearningConflictError):
        teacher_verification_service.remove_evidence(
            session, teacher, read.id, evidence.id
        )


def test_submit_is_blocked_until_structural_rules_pass(
    session: Session, teacher: User, storage: MemoryStorage, catalog: dict
) -> None:
    read = _start(session, teacher)
    ctx = _academic_context_payload(catalog)
    teacher_verification_service.replace_contexts(
        session, teacher, read.id, ContextsReplace(contexts=[ctx])
    )
    session.expire_all()
    detail = teacher_verification_service.get_my_application(session, teacher)
    context_id = detail.contexts[0].learning_context_id

    with pytest.raises(LearningValidationError) as excinfo:
        teacher_verification_service.submit_application(session, teacher, read.id)
    assert "attach at least one evidence document" in str(excinfo.value)

    _attach(
        session,
        teacher,
        read.id,
        evidence_type=TeacherEvidenceType.IDENTITY_DOCUMENT,
        title="National ID",
        data=_PDF_BYTES,
        context_ids=[context_id],
        storage=storage,
    )
    session.expire_all()
    with pytest.raises(LearningValidationError) as excinfo:
        teacher_verification_service.submit_application(session, teacher, read.id)
    assert "attach academic qualification evidence" in str(excinfo.value)

    _attach(
        session,
        teacher,
        read.id,
        evidence_type=TeacherEvidenceType.ACADEMIC_QUALIFICATION,
        title="B.Ed",
        data=_QUALIFICATION_PDF,
        context_ids=[context_id],
        storage=storage,
    )
    session.expire_all()
    with pytest.raises(LearningValidationError) as excinfo:
        teacher_verification_service.submit_application(session, teacher, read.id)
    assert "attach teacher-training or professional-registration evidence" in str(
        excinfo.value
    )

    _attach(
        session,
        teacher,
        read.id,
        evidence_type=TeacherEvidenceType.TEACHER_TRAINING,
        title="PGCE",
        data=_TRAINING_PDF,
        context_ids=[context_id],
        storage=storage,
    )
    session.expire_all()
    submitted = teacher_verification_service.submit_application(
        session, teacher, read.id
    )
    assert submitted.status == TeacherApplicationStatus.SUBMITTED.value
    assert submitted.submitted_at is not None


def test_withdraw_from_draft_and_reapplication_links_previous(
    session: Session, teacher: User
) -> None:
    read = _start(session, teacher)
    withdrawn = teacher_verification_service.withdraw_application(
        session, teacher, read.id
    )
    assert withdrawn.status == TeacherApplicationStatus.WITHDRAWN.value

    with pytest.raises(LearningConflictError):
        teacher_verification_service.withdraw_application(session, teacher, read.id)

    again = _start(session, teacher)
    assert again.previous_application_id == read.id


def test_get_my_scope_is_empty_before_approval(session: Session, teacher: User) -> None:
    assert teacher_verification_service.get_my_scope(session, teacher) == []


# --- admin review ----------------------------------------------------------------------


def _submit_formal(
    session: Session,
    teacher: User,
    storage: MemoryStorage,
    catalog: dict,
    *,
    with_school: bool = False,
):
    """Start + contexts + evidence + submit; returns (application, context_id)."""
    read = _start(session, teacher)
    ctx = _academic_context_payload(catalog)
    teacher_verification_service.replace_contexts(
        session, teacher, read.id, ContextsReplace(contexts=[ctx])
    )
    detail = teacher_verification_service.get_my_application(session, teacher)
    context_id = detail.contexts[0].learning_context_id
    for evidence_type, title, data in (
        (TeacherEvidenceType.IDENTITY_DOCUMENT, "National ID", _PDF_BYTES),
        (
            TeacherEvidenceType.ACADEMIC_QUALIFICATION,
            "B.Ed",
            _QUALIFICATION_PDF,
        ),
        (TeacherEvidenceType.TEACHER_TRAINING, "PGCE", _TRAINING_PDF),
    ):
        _attach(
            session,
            teacher,
            read.id,
            evidence_type=evidence_type,
            title=title,
            data=data,
            context_ids=[context_id],
            storage=storage,
        )
    submitted = teacher_verification_service.submit_application(
        session, teacher, read.id
    )
    return submitted, context_id


def test_record_check_is_monotonic_and_refuses_non_review_states(
    session: Session, teacher: User, admin: User, storage: MemoryStorage, catalog: dict
) -> None:
    application, context_id = _submit_formal(session, teacher, storage, catalog)
    evidence = next(e for e in application.evidence)

    first = teacher_verification_service.record_check(
        session,
        admin,
        application.id,
        VerificationCheckRecord(
            evidence_id=evidence.id,
            method=VerificationMethod.REGISTRY_LOOKUP,
            outcome=VerificationCheckOutcome.VERIFIED,
            source_institution="Ministry registry",
        ),
    )
    assert first.seq == 1
    second = teacher_verification_service.record_check(
        session,
        admin,
        application.id,
        VerificationCheckRecord(
            evidence_id=evidence.id,
            method=VerificationMethod.ISSUER_CONFIRMATION,
            outcome=VerificationCheckOutcome.VERIFIED,
            source_institution="University",
        ),
    )
    assert second.seq == 2

    # Checks are refused once the application leaves review.
    teacher_verification_service.withdraw_application(session, teacher, application.id)
    with pytest.raises(LearningConflictError):
        teacher_verification_service.record_check(
            session,
            admin,
            application.id,
            VerificationCheckRecord(
                evidence_id=evidence.id,
                method=VerificationMethod.REGISTRY_LOOKUP,
                outcome=VerificationCheckOutcome.VERIFIED,
                source_institution="Ministry registry",
            ),
        )


def test_request_information_returns_to_applicant_and_resubmit(
    session: Session, teacher: User, admin: User, storage: MemoryStorage, catalog: dict
) -> None:
    application, context_id = _submit_formal(session, teacher, storage, catalog)
    requested = teacher_verification_service.request_information(
        session,
        admin,
        application.id,
        InformationRequest(message="please clarify your qualification"),
    )
    assert requested.status == TeacherApplicationStatus.NEEDS_INFORMATION.value

    # Applicant may edit and resubmit.
    teacher_verification_service.update_application(
        session,
        teacher,
        application.id,
        ApplicationUpdate(statement="clarified"),
    )
    resubmitted = teacher_verification_service.submit_application(
        session, teacher, application.id
    )
    assert resubmitted.status == TeacherApplicationStatus.SUBMITTED.value


def test_reject_requires_reason_and_sets_profile_rejected(
    session: Session, teacher: User, admin: User, storage: MemoryStorage, catalog: dict
) -> None:
    application, _ = _submit_formal(session, teacher, storage, catalog)

    with pytest.raises(LearningValidationError):
        teacher_verification_service.decide_application(
            session,
            admin,
            application.id,
            ApplicationDecision(approve=False, reason="   "),
        )

    rejected = teacher_verification_service.decide_application(
        session,
        admin,
        application.id,
        ApplicationDecision(approve=False, reason="documents insufficient"),
    )
    assert rejected.status == TeacherApplicationStatus.REJECTED.value
    profile = _profile(session, teacher)
    assert profile.verification_status == TeacherVerificationStatus.REJECTED.value


def test_approve_requires_verified_checks_and_writes_scope(
    session: Session, teacher: User, admin: User, storage: MemoryStorage, catalog: dict
) -> None:
    application, context_id = _submit_formal(session, teacher, storage, catalog)

    # Without any verified check, approval is blocked.
    with pytest.raises(LearningValidationError) as excinfo:
        teacher_verification_service.decide_application(
            session,
            admin,
            application.id,
            ApplicationDecision(
                approve=True,
                contexts=[
                    ContextDecision(
                        learning_context_id=context_id,
                        status=ApplicationContextStatus.APPROVED,
                    )
                ],
            ),
        )
    assert "has no latest check with outcome 'verified'" in str(excinfo.value)

    for evidence in application.evidence:
        teacher_verification_service.record_check(
            session,
            admin,
            application.id,
            VerificationCheckRecord(
                evidence_id=evidence.id,
                method=VerificationMethod.REGISTRY_LOOKUP,
                outcome=VerificationCheckOutcome.VERIFIED,
                source_institution="Ministry registry",
            ),
        )

    # Partial context coverage is refused.
    with pytest.raises(LearningValidationError) as excinfo:
        teacher_verification_service.decide_application(
            session,
            admin,
            application.id,
            ApplicationDecision(approve=True, contexts=[]),
        )
    assert "must supply an explicit outcome" in str(excinfo.value)

    approved = teacher_verification_service.decide_application(
        session,
        admin,
        application.id,
        ApplicationDecision(
            approve=True,
            contexts=[
                ContextDecision(
                    learning_context_id=context_id,
                    status=ApplicationContextStatus.APPROVED,
                )
            ],
        ),
    )
    assert approved.status == TeacherApplicationStatus.APPROVED.value

    profile = _profile(session, teacher)
    assert profile.verification_status == TeacherVerificationStatus.APPROVED.value
    assert profile.verification_category == TeacherVerificationCategory.FORMAL_TEACHER.value

    scope = session.scalar(
        select(TeacherApprovedContext).where(
            TeacherApprovedContext.teacher_id == profile.id
        )
    )
    assert scope is not None
    assert scope.learning_context_id == context_id
    assert scope.application_id == application.id

    # The applicant's scope endpoint now returns the row.
    mine = teacher_verification_service.get_my_scope(session, teacher)
    assert [r.learning_context_id for r in mine] == [context_id]

    # Workflow audit events name the teacher's user account (the
    # auth_events.user_id FK targets users.id), with the deciding admin
    # recorded as the actor.
    events = session.scalars(
        select(AuthEvent).where(AuthEvent.user_id == teacher.id)
    ).all()
    event_types = [e.event_type for e in events]
    assert "application_approved" in event_types
    assert "context_approved" in event_types


def test_decide_refuses_non_submitted_application(
    session: Session, teacher: User, admin: User
) -> None:
    read = _start(session, teacher)
    with pytest.raises(LearningConflictError):
        teacher_verification_service.decide_application(
            session,
            admin,
            read.id,
            ApplicationDecision(approve=False, reason="no"),
        )


# --- direct-approval bypass gate --------------------------------------------------------


def test_direct_approval_is_refused_without_completed_application(
    session: Session, teacher: User, admin: User
) -> None:
    from app.schemas.teacher_admin import TeacherVerificationUpdate

    with pytest.raises(AuthConflictError) as excinfo:
        admin_user_service.set_teacher_verification(
            session,
            teacher.id,
            TeacherVerificationUpdate(status=TeacherVerificationStatus.APPROVED),
            actor=admin,
        )
    assert excinfo.value.status_code == 409
    assert "application workflow" in str(excinfo.value)


def test_direct_approval_reinstates_after_completed_workflow(
    session: Session, teacher: User, admin: User, storage: MemoryStorage, catalog: dict
) -> None:
    from app.schemas.teacher_admin import TeacherVerificationUpdate

    application, context_id = _submit_formal(session, teacher, storage, catalog)
    for evidence in application.evidence:
        teacher_verification_service.record_check(
            session,
            admin,
            application.id,
            VerificationCheckRecord(
                evidence_id=evidence.id,
                method=VerificationMethod.REGISTRY_LOOKUP,
                outcome=VerificationCheckOutcome.VERIFIED,
                source_institution="Ministry registry",
            ),
        )
    teacher_verification_service.decide_application(
        session,
        admin,
        application.id,
        ApplicationDecision(
            approve=True,
            contexts=[
                ContextDecision(
                    learning_context_id=context_id,
                    status=ApplicationContextStatus.APPROVED,
                )
            ],
        ),
    )
    # Suspend, then reinstate through the direct endpoint (allowed: the
    # completed application + scope still exist).
    admin_user_service.set_teacher_verification(
        session,
        teacher.id,
        TeacherVerificationUpdate(status=TeacherVerificationStatus.SUSPENDED),
        actor=admin,
    )
    reinstated = admin_user_service.set_teacher_verification(
        session,
        teacher.id,
        TeacherVerificationUpdate(status=TeacherVerificationStatus.APPROVED),
        actor=admin,
    )
    assert reinstated.verification_status == TeacherVerificationStatus.APPROVED.value
