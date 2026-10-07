"""Unit tests: topics and lessons beneath a teaching offering (Phase 2, 2A).

In-memory SQLite, no HTTP — the service layer's own contract:

- only the teacher who owns an offering may create/update/delete its
  topics and lessons; a foreign offering/topic/lesson id answers the same
  404 as an unknown one (L6 existence leak), never a 403 that would
  confirm the row exists;
- a non-teacher caller (student/admin) is refused with the shared
  ``LearningForbiddenError`` even if the service is reached directly;
- topics stay inside the offering's educational context — the topic hangs
  off ``teaching_offerings`` and never re-declares the catalog;
- ordering is stored, not recomputed: ``display_order`` is unique within
  the parent, created topics/lessons take the next free position, and a
  PATCH onto a held position is a 409;
- deleting a topic removes its lessons.
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
    TeacherVerificationStatus,
    UserRole,
    UserStatus,
)
from app.models.pathway import Pathway
from app.models.pathway_level import PathwayLevel
from app.models.program import Program
from app.models.program_subject import ProgramSubject
from app.models.program_version import ProgramVersion
from app.models.subject import Subject
from app.models.teacher import Teacher
from app.models.topic import Topic
from app.models.user import User
from app.schemas.curriculum import (
    LessonCreate,
    LessonUpdate,
    TopicCreate,
    TopicUpdate,
)
from app.schemas.learning import TeachingOfferingCreate
from app.services import curriculum_service, teaching_offering_service
from app.services.learning_context_service import (
    LearningConflictError,
    LearningForbiddenError,
    LearningNotFoundError,
)

PASSWORD = "correct horse battery staple"


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
def catalog(session: Session) -> dict:
    """A minimal coherent catalog: year + pathway + level + program + subject."""
    year = AcademicYear(
        name="2026/2027",
        start_date=date(2026, 9, 1),
        end_date=date(2027, 6, 30),
        status=AcademicYearStatus.ACTIVE.value,
    )
    pathway = Pathway(code="O_LEVEL", name="O Level")
    level = EducationLevel(code="S3", name="Senior 3", level_number=3)
    session.add_all([year, pathway, level])
    session.flush()
    session.add(PathwayLevel(pathway_id=pathway.id, education_level_id=level.id))

    program = Program(code="COMB", name="Combination", program_type="combination")
    session.add(program)
    session.flush()
    program_version = ProgramVersion(
        program_id=program.id,
        academic_year_id=year.id,
        pathway_id=pathway.id,
        education_level_id=level.id,
        code="COMB-2026",
        name="Combination 2026",
    )
    session.add(program_version)

    maths = Subject(code="MATH", name="Mathematics")
    session.add(maths)
    session.flush()
    session.add(
        ProgramSubject(
            program_version_id=program_version.id,
            subject_id=maths.id,
        )
    )
    session.commit()
    return {
        "year": year,
        "pathway": pathway,
        "level": level,
        "program_version": program_version,
        "maths": maths,
    }


def _teacher_user(
    session: Session, *, verification: str, email: str | None = None
) -> User:
    user = User(
        email=email or f"teacher-{uuid.uuid4().hex[:8]}@example.com",
        role=UserRole.TEACHER.value,
        status=UserStatus.ACTIVE.value,
        password_hash=security.hash_password(PASSWORD),
    )
    session.add(user)
    session.flush()
    session.add(
        Teacher(
            user_id=user.id,
            full_name="Test Teacher",
            verification_status=verification,
        )
    )
    session.commit()
    return user


@pytest.fixture()
def approved_teacher(session: Session) -> User:
    return _teacher_user(session, verification=TeacherVerificationStatus.APPROVED.value)


@pytest.fixture()
def other_teacher(session: Session) -> User:
    return _teacher_user(session, verification=TeacherVerificationStatus.APPROVED.value)


@pytest.fixture()
def student(session: Session) -> User:
    user = User(
        email=f"student-{uuid.uuid4().hex[:8]}@example.com",
        role=UserRole.STUDENT.value,
        status=UserStatus.ACTIVE.value,
        password_hash=security.hash_password(PASSWORD),
    )
    session.add(user)
    session.commit()
    return user


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


def _offering(session: Session, catalog: dict, teacher: User) -> uuid.UUID:
    read = teaching_offering_service.create_offering(
        session,
        teacher,
        TeachingOfferingCreate(
            academic_year_id=catalog["year"].id,
            pathway_id=catalog["pathway"].id,
            education_level_id=catalog["level"].id,
            program_version_id=catalog["program_version"].id,
            subject_id=catalog["maths"].id,
        ),
    )
    session.commit()
    return read.offering_id


@pytest.fixture()
def offering_id(session: Session, catalog: dict, approved_teacher: User) -> uuid.UUID:
    return _offering(session, catalog, approved_teacher)


@pytest.fixture()
def other_offering_id(
    session: Session, catalog: dict, other_teacher: User
) -> uuid.UUID:
    return _offering(session, catalog, other_teacher)


# --- topics ---------------------------------------------------------------------------


def test_teacher_can_create_topic_for_own_offering(
    session: Session, approved_teacher: User, offering_id: uuid.UUID
) -> None:
    read = curriculum_service.create_topic(
        session,
        approved_teacher,
        offering_id,
        TopicCreate(title="Algebra basics", description="Opening unit"),
    )
    session.commit()

    assert read.offering_id == offering_id
    assert read.title == "Algebra basics"
    assert read.description == "Opening unit"
    assert read.display_order == 1  # next free position in an empty offering

    events = session.scalars(
        select(AuthEvent).where(AuthEvent.user_id == approved_teacher.id)
    ).all()
    curriculum_events = [e.event_type for e in events if e.event_type.startswith(("topic_", "lesson_"))]
    assert curriculum_events == ["topic_created"]


def test_teacher_cannot_create_topic_for_another_teachers_offering(
    session: Session,
    approved_teacher: User,
    offering_id: uuid.UUID,
    other_offering_id: uuid.UUID,
) -> None:
    # A foreign offering id must be indistinguishable from an unknown one.
    unknown = uuid.uuid4()
    for target in (other_offering_id, unknown):
        with pytest.raises(LearningNotFoundError) as excinfo:
            curriculum_service.create_topic(
                session,
                approved_teacher,
                target,
                TopicCreate(title="Not mine"),
            )
        message = str(excinfo.value)
        assert message.startswith("no teaching offering with id ")
        assert not any(word in message for word in ("other", "not yours", "teacher"))

    # Nothing was written under either id.
    assert (
        session.scalars(
            select(Topic).where(Topic.teaching_offering_id == other_offering_id)
        ).all()
        == []
    )


def test_teacher_can_update_and_delete_own_topic_where_allowed(
    session: Session, approved_teacher: User, offering_id: uuid.UUID
) -> None:
    created = curriculum_service.create_topic(
        session,
        approved_teacher,
        offering_id,
        TopicCreate(title="Geometry", description="Draft"),
    )
    session.commit()

    updated = curriculum_service.update_topic(
        session,
        approved_teacher,
        offering_id,
        created.topic_id,
        TopicUpdate(title="Geometry essentials", description=None),
    )
    assert updated.title == "Geometry essentials"
    assert updated.description is None

    curriculum_service.delete_topic(session, approved_teacher, offering_id, created.topic_id)
    session.commit()

    with pytest.raises(LearningNotFoundError):
        curriculum_service.get_topic(
            session, approved_teacher, offering_id, created.topic_id
        )

    event_types = [
        e.event_type
        for e in session.scalars(
            select(AuthEvent).where(AuthEvent.user_id == approved_teacher.id)
        )
        if e.event_type.startswith(("topic_", "lesson_"))
    ]
    assert event_types == ["topic_created", "topic_updated", "topic_deleted"]


def test_topic_display_order_collision_is_a_conflict(
    session: Session, approved_teacher: User, offering_id: uuid.UUID
) -> None:
    first = curriculum_service.create_topic(
        session, approved_teacher, offering_id, TopicCreate(title="One")
    )
    second = curriculum_service.create_topic(
        session, approved_teacher, offering_id, TopicCreate(title="Two")
    )
    session.commit()

    with pytest.raises(LearningConflictError) as excinfo:
        curriculum_service.update_topic(
            session,
            approved_teacher,
            offering_id,
            second.topic_id,
            TopicUpdate(display_order=first.display_order),
        )
    session.rollback()
    assert "display_order" in str(excinfo.value)
    assert "already occupies" in str(excinfo.value)

    with pytest.raises(LearningConflictError):
        curriculum_service.create_topic(
            session,
            approved_teacher,
            offering_id,
            TopicCreate(title="Collide", display_order=first.display_order),
        )
    session.rollback()


def test_reordering_topics_changes_the_stored_sequence(
    session: Session, approved_teacher: User, offering_id: uuid.UUID
) -> None:
    first = curriculum_service.create_topic(
        session, approved_teacher, offering_id, TopicCreate(title="One")
    )
    second = curriculum_service.create_topic(
        session, approved_teacher, offering_id, TopicCreate(title="Two")
    )
    session.commit()

    # Free position 3, move Two there, then move One to 2 — no collisions.
    curriculum_service.update_topic(
        session, approved_teacher, offering_id, second.topic_id,
        TopicUpdate(display_order=3),
    )
    curriculum_service.update_topic(
        session, approved_teacher, offering_id, first.topic_id,
        TopicUpdate(display_order=2),
    )
    session.commit()

    listed = curriculum_service.list_topics(session, approved_teacher, offering_id)
    assert [(t.title, t.display_order) for t in listed] == [("One", 2), ("Two", 3)]


def test_foreign_topic_id_under_own_offering_is_a_conflict_free_404(
    session: Session, approved_teacher: User, offering_id: uuid.UUID
) -> None:
    curriculum_service.create_topic(
        session, approved_teacher, offering_id, TopicCreate(title="Mine")
    )
    session.commit()
    unknown = uuid.uuid4()
    with pytest.raises(LearningNotFoundError) as excinfo:
        curriculum_service.get_topic(
            session, approved_teacher, offering_id, unknown
        )
    assert str(excinfo.value).startswith("no topic with id ")


# --- lessons --------------------------------------------------------------------------


def test_teacher_can_create_lesson_under_own_topic(
    session: Session, approved_teacher: User, offering_id: uuid.UUID
) -> None:
    topic = curriculum_service.create_topic(
        session, approved_teacher, offering_id, TopicCreate(title="Algebra")
    )
    session.commit()

    read = curriculum_service.create_lesson(
        session,
        approved_teacher,
        offering_id,
        topic.topic_id,
        LessonCreate(title="Linear equations", description="Solve for x"),
    )
    session.commit()

    assert read.topic_id == topic.topic_id
    assert read.offering_id == offering_id
    assert read.title == "Linear equations"
    assert read.description == "Solve for x"
    assert read.display_order == 1

    events = session.scalars(
        select(AuthEvent).where(AuthEvent.user_id == approved_teacher.id)
    ).all()
    curriculum_events = [e.event_type for e in events if e.event_type.startswith(("topic_", "lesson_"))]
    assert curriculum_events == ["topic_created", "lesson_created"]


def test_lesson_cannot_be_attached_to_another_teachers_topic_or_offering(
    session: Session,
    approved_teacher: User,
    offering_id: uuid.UUID,
    other_offering_id: uuid.UUID,
) -> None:
    # A lesson under a foreign offering id: 404, same shape as unknown.
    unknown_offering = uuid.uuid4()
    for target in (other_offering_id, unknown_offering):
        with pytest.raises(LearningNotFoundError) as excinfo:
            curriculum_service.create_lesson(
                session,
                approved_teacher,
                target,
                uuid.uuid4(),
                LessonCreate(title="Not mine"),
            )
        assert str(excinfo.value).startswith("no teaching offering with id ")

    # A lesson under one of YOUR topics but a foreign topic id: 404.
    topic = curriculum_service.create_topic(
        session, approved_teacher, offering_id, TopicCreate(title="Mine")
    )
    session.commit()
    with pytest.raises(LearningNotFoundError) as excinfo:
        curriculum_service.create_lesson(
            session,
            approved_teacher,
            offering_id,
            uuid.uuid4(),
            LessonCreate(title="Foreign topic"),
        )
    assert str(excinfo.value).startswith("no topic with id ")

    assert topic.title == "Mine"


def test_lesson_ordering_is_stored_and_collision_is_a_conflict(
    session: Session, approved_teacher: User, offering_id: uuid.UUID
) -> None:
    topic = curriculum_service.create_topic(
        session, approved_teacher, offering_id, TopicCreate(title="Algebra")
    )
    session.commit()

    first = curriculum_service.create_lesson(
        session, approved_teacher, offering_id, topic.topic_id, LessonCreate(title="A")
    )
    second = curriculum_service.create_lesson(
        session, approved_teacher, offering_id, topic.topic_id, LessonCreate(title="B")
    )
    third = curriculum_service.create_lesson(
        session, approved_teacher, offering_id, topic.topic_id, LessonCreate(title="C")
    )
    session.commit()
    assert [first.display_order, second.display_order, third.display_order] == [1, 2, 3]

    listed = curriculum_service.list_lessons(session, approved_teacher, offering_id, topic.topic_id)
    assert [x.title for x in listed] == ["A", "B", "C"]

    with pytest.raises(LearningConflictError) as excinfo:
        curriculum_service.update_lesson(
            session,
            approved_teacher,
            offering_id,
            topic.topic_id,
            third.lesson_id,
            LessonUpdate(display_order=first.display_order),
        )
    session.rollback()
    assert "already occupies" in str(excinfo.value)

    # A free-slot move works and the stored sequence changes.
    moved = curriculum_service.update_lesson(
        session,
        approved_teacher,
        offering_id,
        topic.topic_id,
        third.lesson_id,
        LessonUpdate(display_order=4),
    )
    session.commit()
    assert moved.display_order == 4
    listed = curriculum_service.list_lessons(
        session, approved_teacher, offering_id, topic.topic_id
    )
    assert [(x.title, x.display_order) for x in listed] == [
        ("A", 1),
        ("B", 2),
        ("C", 4),
    ]


def test_foreign_lesson_id_under_own_topic_is_a_conflict_free_404(
    session: Session, approved_teacher: User, offering_id: uuid.UUID
) -> None:
    topic = curriculum_service.create_topic(
        session, approved_teacher, offering_id, TopicCreate(title="Algebra")
    )
    curriculum_service.create_lesson(
        session, approved_teacher, offering_id, topic.topic_id, LessonCreate(title="A")
    )
    session.commit()
    with pytest.raises(LearningNotFoundError) as excinfo:
        curriculum_service.get_lesson(
            session, approved_teacher, offering_id, topic.topic_id, uuid.uuid4()
        )
    assert str(excinfo.value).startswith("no lesson with id ")


def test_delete_topic_removes_its_lessons(
    session: Session, approved_teacher: User, offering_id: uuid.UUID
) -> None:
    topic = curriculum_service.create_topic(
        session, approved_teacher, offering_id, TopicCreate(title="Algebra")
    )
    curriculum_service.create_lesson(
        session, approved_teacher, offering_id, topic.topic_id, LessonCreate(title="A")
    )
    curriculum_service.create_lesson(
        session, approved_teacher, offering_id, topic.topic_id, LessonCreate(title="B")
    )
    session.commit()

    curriculum_service.delete_topic(session, approved_teacher, offering_id, topic.topic_id)
    session.commit()

    from app.models.lesson import Lesson
    from app.models.topic import Topic

    assert session.scalar(select(Topic).where(Topic.id == topic.topic_id)) is None
    assert session.scalars(
        select(Lesson).where(Lesson.topic_id == topic.topic_id)
    ).all() == []


def test_student_and_admin_cannot_mutate_topics_or_lessons(
    session: Session, student: User, admin: User, offering_id: uuid.UUID
) -> None:
    """Role guard is the route; the service refuses non-teachers too."""
    for caller in (student, admin):
        with pytest.raises(LearningForbiddenError) as excinfo:
            curriculum_service.create_topic(
                session, caller, offering_id, TopicCreate(title="Nope")
            )
        assert excinfo.value.status_code == 403
        assert "teacher role required" in str(excinfo.value)


def test_lesson_read_and_update_roundtrip(
    session: Session, approved_teacher: User, offering_id: uuid.UUID
) -> None:
    topic = curriculum_service.create_topic(
        session, approved_teacher, offering_id, TopicCreate(title="Algebra")
    )
    created = curriculum_service.create_lesson(
        session, approved_teacher, offering_id, topic.topic_id, LessonCreate(title="A")
    )
    session.commit()

    read = curriculum_service.get_lesson(
        session, approved_teacher, offering_id, topic.topic_id, created.lesson_id
    )
    assert read.lesson_id == created.lesson_id
    assert read.offering_id == offering_id

    updated = curriculum_service.update_lesson(
        session,
        approved_teacher,
        offering_id,
        topic.topic_id,
        created.lesson_id,
        LessonUpdate(title="A+", description="Revised"),
    )
    assert updated.title == "A+"
    assert updated.description == "Revised"

    curriculum_service.delete_lesson(
        session, approved_teacher, offering_id, topic.topic_id, created.lesson_id
    )
    session.commit()
    with pytest.raises(LearningNotFoundError):
        curriculum_service.get_lesson(
            session, approved_teacher, offering_id, topic.topic_id, created.lesson_id
        )


# --- MVP authoring rules: approval gate ---------------------------------------------------


def test_unapproved_teacher_can_read_curriculum_but_never_write_it(
    session: Session, approved_teacher: User, offering_id: uuid.UUID
) -> None:
    topic = curriculum_service.create_topic(
        session,
        approved_teacher,
        offering_id,
        TopicCreate(title="Linear equations", display_order=1),
    )
    lesson = curriculum_service.create_lesson(
        session,
        approved_teacher,
        offering_id,
        topic.topic_id,
        LessonCreate(title="Balancing both sides", display_order=1),
    )
    session.commit()

    profile = session.scalar(select(Teacher).where(Teacher.user_id == approved_teacher.id))
    profile.verification_status = TeacherVerificationStatus.SUSPENDED.value
    session.commit()

    # Reads of the teacher's own curriculum stay open.
    assert [t.topic_id for t in curriculum_service.list_topics(
        session, approved_teacher, offering_id
    )] == [topic.topic_id]
    assert curriculum_service.get_lesson(
        session, approved_teacher, offering_id, topic.topic_id, lesson.lesson_id
    ).lesson_id == lesson.lesson_id

    attempts = [
        lambda: curriculum_service.create_topic(
            session, approved_teacher, offering_id, TopicCreate(title="nope")
        ),
        lambda: curriculum_service.update_topic(
            session,
            approved_teacher,
            offering_id,
            topic.topic_id,
            TopicUpdate(title="renamed while suspended"),
        ),
        lambda: curriculum_service.delete_topic(
            session, approved_teacher, offering_id, topic.topic_id
        ),
        lambda: curriculum_service.create_lesson(
            session,
            approved_teacher,
            offering_id,
            topic.topic_id,
            LessonCreate(title="nope"),
        ),
        lambda: curriculum_service.update_lesson(
            session,
            approved_teacher,
            offering_id,
            topic.topic_id,
            lesson.lesson_id,
            LessonUpdate(title="renamed while suspended"),
        ),
        lambda: curriculum_service.delete_lesson(
            session, approved_teacher, offering_id, topic.topic_id, lesson.lesson_id
        ),
    ]
    for attempt in attempts:
        with pytest.raises(LearningForbiddenError) as excinfo:
            attempt()
        message = str(excinfo.value)
        assert "approved teacher" in message
        assert str(topic.topic_id) not in message
        assert str(lesson.lesson_id) not in message
        assert str(offering_id) not in message
    session.rollback()

    # The curriculum that existed before the suspension is untouched.
    topics = curriculum_service.list_topics(session, approved_teacher, offering_id)
    assert [(t.title, t.display_order) for t in topics] == [("Linear equations", 1)]
    assert curriculum_service.list_lessons(
        session, approved_teacher, offering_id, topic.topic_id
    )[0].title == "Balancing both sides"
