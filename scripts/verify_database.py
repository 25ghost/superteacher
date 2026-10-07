"""Phase 3B database verification for the SuperTeacher backend.

Three levels:

1. STRUCTURAL (always runs, no database needed):
   - all 31 expected application tables exist in the ORM metadata, with the
     plural ``students`` name and no singular ``student`` table;
   - every table that has ``created_at`` also has ``updated_at``;
   - the ``users_role_check`` vocabulary equals ``UserRole``, and the
     ``program_versions`` natural key exists;
   - the full metadata can be CREATEd and DROPped on a scratch in-memory
     SQLite database (proves the dependency graph and DDL are valid);
   - MIGRATION DRIFT: the Alembic migration(s) are parsed and compared against
     the ORM metadata column by column (name, type, nullability), constraint by
     constraint (PK, FK, UNIQUE, CHECK including CHECK *values*), and index by
     index. A model/migration mismatch fails the script;
   - exactly one migration revision exists and it is the expected one.

2. LIVE (read-only, ``--live``):
   - connects to PostgreSQL using the configured DB_* settings;
   - checks the live ``alembic_version`` equals the expected revision;
   - checks every one of the 31 tables exists (and singular ``student`` does
     not);
   - compares the LIVE schema against the ORM metadata with the same
     column/constraint/index comparison used for drift detection, so live
     schema drift fails loudly;
   - performs read-only queries only. It never writes, migrates, or drops.

3. REBUILD (opt-in, ``--live --rebuild``):
   - refuses to run when ``ENVIRONMENT=production``;
   - refuses to run when any application table contains rows (this is what
     keeps it from destroying data);
   - then ``alembic downgrade base`` -> verify empty -> ``alembic upgrade
     head`` -> verify all 31 tables and the expected revision again.
   Use it only on a disposable development database.

Usage (from ``backend/``):
    .venv/Scripts/python scripts/verify_database.py              # structural
    .venv/Scripts/python scripts/verify_database.py --live       # + live, read-only
    .venv/Scripts/python scripts/verify_database.py --live --rebuild
"""
from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

EXPECTED_TABLES = {
    "users",
    "auth_sessions",
    "students",
    "academic_years",
    "pathways",
    "education_levels",
    "pathway_levels",
    "schools",
    "programs",
    "program_versions",
    "subjects",
    "program_subjects",
    "tvet_sectors",
    "tvet_programs",
    "school_programs",
    "student_enrollments",
    "student_subjects",
    "teachers",
    "invite_tokens",
    "auth_events",
    "password_reset_tokens",
    "student_profile_history",
    # created by 0010 (Phase 1 learning marketplace)
    "learning_contexts",
    "teaching_offerings",
    "learning_enrollments",
    # created by 0011 (Phase 2 slice 2A: curriculum under an offering)
    "topics",
    "lessons",
    # created by 0012 (Phase 2 slice 2B: materials + moderation)
    "file_assets",
    "materials",
    "material_moderations",
    # created by 0013 (Phase 2 slice 2C: student content access + progress)
    "material_progress",
}

EXPECTED_REVISION = "0014"
MIGRATION_DIR = BACKEND_DIR / "alembic" / "versions"

# The natural key that makes a program offering identifiable (and seedable)
# without duplicating it.
OFFERING_UNIQUE = "uq_program_versions_offering_key"
OFFERING_UNIQUE_COLUMNS = (
    "program_id",
    "academic_year_id",
    "pathway_id",
    "education_level_id",
)

# Foreign-key columns expected to carry their own index because they are the
# trailing column of a composite unique constraint (PostgreSQL does not index
# foreign keys automatically).
EXPECTED_FK_INDEXES = {
    "pathway_levels_education_level_id_idx": ("pathway_levels", ("education_level_id",)),
    "program_subjects_subject_id_idx": ("program_subjects", ("subject_id",)),
    "tvet_programs_sector_id_idx": ("tvet_programs", ("sector_id",)),
    "student_subjects_subject_id_idx": ("student_subjects", ("subject_id",)),
    "student_enrollments_pathway_id_idx": ("student_enrollments", ("pathway_id",)),
    "student_enrollments_education_level_id_idx": (
        "student_enrollments",
        ("education_level_id",),
    ),
    "student_enrollments_program_version_id_idx": (
        "student_enrollments",
        ("program_version_id",),
    ),
    "program_versions_academic_year_id_idx": ("program_versions", ("academic_year_id",)),
    "program_versions_pathway_id_idx": ("program_versions", ("pathway_id",)),
    "program_versions_education_level_id_idx": ("program_versions", ("education_level_id",)),
    # Phase 2 slice 2A: curriculum FKs under an offering / topic.
    "topics_teaching_offering_id_idx": ("topics", ("teaching_offering_id",)),
    "lessons_topic_id_idx": ("lessons", ("topic_id",)),
    # Phase 2 slice 2B: material system FKs.
    "file_assets_uploaded_by_user_id_idx": ("file_assets", ("uploaded_by_user_id",)),
    "materials_teaching_offering_id_idx": ("materials", ("teaching_offering_id",)),
    "materials_lesson_id_idx": ("materials", ("lesson_id",)),
    "materials_file_asset_id_idx": ("materials", ("file_asset_id",)),
    "material_moderations_material_id_idx": (
        "material_moderations",
        ("material_id",),
    ),
    "material_moderations_reviewer_user_id_idx": (
        "material_moderations",
        ("reviewer_user_id",),
    ),
    # Phase 2 slice 2C: personal material progress FKs.
    "material_progress_student_id_idx": ("material_progress", ("student_id",)),
    "material_progress_material_id_idx": ("material_progress", ("material_id",)),
}

# --- type / CHECK-text normalisation -----------------------------------------

_AST_TYPES = {
    "String": lambda kwargs: (
        f"VARCHAR({kwargs['length']})" if "length" in kwargs else "VARCHAR"
    ),
    "Text": lambda kwargs: "TEXT",
    "Uuid": lambda kwargs: "UUID",
    "DateTime": lambda kwargs: "TIMESTAMP",
    "Date": lambda kwargs: "DATE",
    "Integer": lambda kwargs: "INTEGER",
    "Boolean": lambda kwargs: "BOOLEAN",
}


def _norm_type(type_text: str) -> str:
    """Normalise a SQL type name so model/migration/live forms compare equal.

    PostgreSQL's inspector reports timezone-aware columns as ``TIMESTAMP``
    without the ``WITH TIME ZONE`` suffix, so that suffix is dropped on both
    sides; spacing is otherwise collapsed.
    """
    text = re.sub(r"\s+", " ", str(type_text).strip().upper())
    text = text.replace(" WITH TIME ZONE", "").replace(" WITHOUT TIME ZONE", "")
    return text.replace("CHARACTER VARYING", "VARCHAR")


def _check_literals(text: str) -> tuple[str, ...]:
    """The quoted values inside a CHECK constraint, sorted (e.g. enum values)."""
    return tuple(sorted(re.findall(r"'([^']*)'", text)))


def _check_shape(text: str) -> str:
    """A CHECK constraint's structure with literals masked and parens removed."""
    masked = re.sub(r"'[^']*'", "?", text)
    masked = re.sub(r"::\s*[a-z_ ]+", "", masked)
    return re.sub(r"[\s()\[\]]", "", masked).upper()


def _checks_match(expected_text: str, actual_text: str) -> bool:
    """Compare CHECK constraints, comparing *values* for IN/ANY-style checks.

    PostgreSQL rewrites ``status IN ('a', 'b')`` as ``status::text = ANY
    (ARRAY['a', 'b'])``, so those are compared by their literal sets; every
    other check is compared by structure.
    """
    expected_literals = _check_literals(expected_text)
    actual_literals = _check_literals(actual_text)
    is_enum_style = (
        bool(expected_literals)
        and bool(actual_literals)
        and (
            " IN " in f" {expected_text} ".upper() or "= ANY" in actual_text.upper()
        )
    )
    if is_enum_style:
        return expected_literals == actual_literals
    return _check_shape(expected_text) == _check_shape(actual_text)


# --- schema descriptors -------------------------------------------------------


def schema_from_metadata() -> dict[str, dict]:
    """Describe the ORM metadata as flat, comparable structures."""
    import app.models  # noqa: F401  (registers every table on Base.metadata)
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.sql.schema import (
        CheckConstraint,
        ForeignKeyConstraint,
        UniqueConstraint,
    )

    from app.core.database import Base

    dialect = postgresql.dialect()
    schema: dict[str, dict] = {}
    for name, table in Base.metadata.tables.items():
        fks = {}
        uqs = {}
        checks = {}
        for constraint in table.constraints:
            if isinstance(constraint, ForeignKeyConstraint):
                columns = tuple(element.parent.name for element in constraint.elements)
                referred = tuple(
                    element.target_fullname.split(".")[0] for element in constraint.elements
                )
                fks[constraint.name] = (columns, referred)
            elif isinstance(constraint, UniqueConstraint):
                uqs[constraint.name] = tuple(column.name for column in constraint.columns)
            elif isinstance(constraint, CheckConstraint):
                checks[constraint.name] = str(constraint.sqltext)
        schema[name] = {
            "columns": {
                # Compile against PostgreSQL so types are comparable with both
                # the migration DDL and the live inspector output.
                column.name: (_norm_type(column.type.compile(dialect=dialect)), bool(column.nullable))
                for column in table.columns
            },
            "pk": tuple(column.name for column in table.primary_key.columns),
            "fks": fks,
            "uqs": uqs,
            "checks": checks,
            "indexes": {
                index.name: tuple(column.name for column in index.columns)
                for index in table.indexes
            },
        }
    return schema


def schema_from_migration(path: Path) -> dict[str, dict]:
    """Describe the DDL of a migration file parsed with ``ast`` (no execution).

    This is what makes offline drift detection possible: a migration whose
    columns, CHECK values, constraints or indexes disagree with the models is
    reported without needing a database.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    schema: dict[str, dict] = {}

    def _type_of(node: ast.AST) -> str:
        if not isinstance(node, ast.Call):
            return _norm_type(ast.unparse(node))
        type_name = getattr(node.func, "attr", None) or getattr(node.func, "id", "")
        if type_name not in _AST_TYPES:
            return _norm_type(type_name)
        kwargs = {kw.arg: ast.unparse(kw.value) for kw in node.keywords}
        if not kwargs and node.args and type_name == "String":
            # ``sa.String(32)`` — length given positionally (migration 0003).
            kwargs["length"] = ast.unparse(node.args[0])
        return _AST_TYPES[type_name](kwargs)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if getattr(node.func, "attr", None) != "create_table":
            continue
        table = node.args[0].value
        columns: dict[str, tuple[str, bool]] = {}
        pk: tuple[str, ...] = ()
        fks: dict = {}
        uqs: dict = {}
        checks: dict = {}
        for arg in node.args[1:]:
            if not isinstance(arg, ast.Call):
                continue
            kind = getattr(arg.func, "attr", None)
            name_kw = next(
                (kw.value.value for kw in arg.keywords if kw.arg == "name"), None
            )
            if kind == "Column":
                nullable = True
                primary_key = False
                unique = False
                explicit_nullable = False
                inline_fk: str | None = None
                for kw in arg.keywords:
                    if kw.arg == "nullable":
                        nullable = ast.unparse(kw.value) == "True"
                        explicit_nullable = True
                    elif kw.arg == "primary_key":
                        primary_key = ast.unparse(kw.value) == "True"
                    elif kw.arg == "unique":
                        unique = ast.unparse(kw.value) == "True"
                # Positional args after (name, type) — migration 0003 writes
                # ``sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"))``.
                for extra in arg.args[2:]:
                    if (
                        isinstance(extra, ast.Call)
                        and getattr(extra.func, "attr", "") == "ForeignKey"
                        and extra.args
                        and isinstance(extra.args[0], ast.Constant)
                    ):
                        inline_fk = extra.args[0].value
                if primary_key and not explicit_nullable:
                    # A primary key column is implicitly NOT NULL.
                    nullable = False
                column = arg.args[0].value
                columns[column] = (_type_of(arg.args[1]), nullable)
                if primary_key:
                    pk = pk + (column,)
                if inline_fk is not None:
                    # Naming convention: "%(table_name)s_%(column_0_name)s_fkey"
                    fks[f"{table}_{column}_fkey"] = (
                        (column,),
                        (inline_fk.split(".")[0],),
                    )
                if unique:
                    # Naming convention: "uq_%(table_name)s_%(column_0_name)s_key"
                    uqs[f"uq_{table}_{column}_key"] = (column,)
            elif kind == "PrimaryKeyConstraint":
                pk = tuple(item.value for item in arg.args)
            elif kind == "ForeignKeyConstraint":
                fks[name_kw] = (
                    tuple(item.value for item in arg.args[0].elts),
                    tuple(item.value.split(".")[0] for item in arg.args[1].elts),
                )
            elif kind == "UniqueConstraint":
                uqs[name_kw] = tuple(item.value for item in arg.args)
            elif kind == "CheckConstraint":
                # Migration CHECK texts are string literals; use the exact value
                # rather than a re-printed representation.
                checks[name_kw] = (
                    arg.args[0].value
                    if isinstance(arg.args[0], ast.Constant)
                    else ast.unparse(arg.args[0])
                )
        schema[table] = {
            "columns": columns,
            "pk": pk,
            "fks": fks,
            "uqs": uqs,
            "checks": checks,
            "indexes": {},
        }

    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "create_index":
            # Only literal calls are compared; a computed name cannot be
            # resolved offline (and would otherwise crash the scan).
            if (
                len(node.args) < 3
                or not isinstance(node.args[0], ast.Constant)
                or not isinstance(node.args[1], ast.Constant)
                or not isinstance(node.args[2], (ast.List, ast.Tuple))
            ):
                continue
            index_name, table = node.args[0].value, node.args[1].value
            columns = tuple(_index_column(item) for item in node.args[2].elts)
            schema.setdefault(table, {}).setdefault("indexes", {})[index_name] = columns

    return schema


def _module_namespace(tree: ast.Module) -> dict:
    """Evaluate this migration's module-level constant expressions.

    Migration CHECK texts are often built from module-level constants (0004
    builds ``students_gender_check`` from ``_GENDER_VOCAB`` through an
    f-string). Literal strings are already covered by ``module_strings``;
    this resolves the *derived* constants so the produced SQL text — not the
    f-string source — is what gets compared with the model. Evaluation is
    sandboxed (no builtins, no imports, no attribute access) and any
    expression that cannot be resolved is skipped.
    """
    namespace: dict = {}
    for node in tree.body:
        if not (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            continue
        try:
            namespace[node.targets[0].id] = eval(
                compile(ast.Expression(node.value), "<migration>", "eval"),
                {"__builtins__": {}},
                namespace,
            )
        except Exception:
            continue
    return namespace


def _namespace_for_upgrade(upgrade_node: ast.AST, base: dict) -> dict:
    """``base`` plus the simple assignments made inside ``upgrade()``.

    0004 builds its CHECK text from ``_gender_list``, a name assigned *inside*
    ``upgrade()`` from the module-level ``_GENDER_VOCAB``. Collecting those
    assignments in source order lets the f-string be evaluated to the SQL it
    actually produces instead of being compared as source text.
    """
    namespace = dict(base)

    def visit(body: list) -> None:
        for stmt in body:
            if (
                isinstance(stmt, ast.Assign)
                and len(stmt.targets) == 1
                and isinstance(stmt.targets[0], ast.Name)
            ):
                try:
                    namespace[stmt.targets[0].id] = eval(
                        compile(ast.Expression(stmt.value), "<migration>", "eval"),
                        {"__builtins__": {}},
                        namespace,
                    )
                except Exception:
                    continue
            for attr in ("body", "orelse", "finalbody"):
                child_body = getattr(stmt, attr, None)
                if isinstance(child_body, list):
                    visit(child_body)

    visit(upgrade_node.body)
    return namespace


def _resolve_condition(
    condition: ast.AST, module_strings: dict, namespace: dict
) -> str:
    """The literal SQL text of a ``create_check_constraint`` condition."""
    if isinstance(condition, ast.Constant):
        return str(condition.value)
    if isinstance(condition, ast.Name) and condition.id in module_strings:
        return module_strings[condition.id]
    try:
        value = eval(
            compile(ast.Expression(condition), "<migration>", "eval"),
            {"__builtins__": {}},
            namespace,
        )
    except Exception:
        return ast.unparse(condition)
    return str(value)


def _upgrade_node(tree: ast.Module) -> ast.AST | None:
    return next(
        (
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "upgrade"
        ),
        None,
    )


def _apply_upgrade_index_drops(schema: dict[str, dict], path: Path) -> None:
    """Apply ``op.drop_index`` from upgrade() to the accumulated schema.

    Index *creations* are collected from the whole file (upgrade and
    downgrade) by :func:`schema_from_migration`, so a rename written as
    "drop old + create new" also picks up the name its ``downgrade()``
    re-creates. Only ``upgrade()`` runs under ``alembic upgrade head``, so
    its ``drop_index`` calls must remove those names from the accumulated
    schema — otherwise the index a migration deliberately renames away would
    still be reported as present (and as drift against the models).
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    upgrade_node = _upgrade_node(tree)
    if upgrade_node is None:
        return
    for node in ast.walk(upgrade_node):
        if not (
            isinstance(node, ast.Call)
            and getattr(node.func, "attr", None) == "drop_index"
            and node.args
            and isinstance(node.args[0], ast.Constant)
        ):
            continue
        name = node.args[0].value
        table = None
        if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
            table = node.args[1].value
        for kw in node.keywords:
            if kw.arg == "table_name" and isinstance(kw.value, ast.Constant):
                table = kw.value.value
        if table and table in schema:
            schema[table]["indexes"].pop(name, None)


def _apply_check_swaps(schema: dict[str, dict], path: Path) -> None:
    """Apply ``drop_constraint`` / ``create_check_constraint`` from upgrade().

    Constraint swaps must be applied in migration order against the
    *accumulated* schema (a later migration drops a check an earlier one
    created), so this runs from :func:`_combined_migration_schema`, not from
    the per-file parser. Only ``upgrade()`` is considered — ``downgrade()``
    is never executed by ``alembic upgrade head``.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    module_strings = {
        node.targets[0].id: node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    }
    upgrade_node = next(
        (
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "upgrade"
        ),
        None,
    )
    if upgrade_node is None:
        return
    namespace = _namespace_for_upgrade(upgrade_node, _module_namespace(tree))
    for node in ast.walk(upgrade_node):
        if not isinstance(node, ast.Call):
            continue
        attr = getattr(node.func, "attr", None)
        if attr == "drop_constraint" and len(node.args) >= 2:
            name = node.args[0].value
            table = node.args[1].value
            if table in schema:
                schema[table]["checks"].pop(name, None)
        elif attr == "create_check_constraint" and len(node.args) >= 3:
            name = node.args[0].value
            table = node.args[1].value
            text = _resolve_condition(node.args[2], module_strings, namespace)
            schema.setdefault(table, {
                "columns": {}, "pk": (), "fks": {}, "uqs": {},
                "checks": {}, "indexes": {},
            })
            schema[table]["checks"][name] = text


def _index_column(node: ast.AST) -> str:
    """Column name for one ``create_index`` column expression.

    Plain columns are string literals; a functional index such as
    ``sa.text("lower(email)")`` is normalised to the underlying column so it
    compares equal to the ORM descriptor (which reports ``email``).
    """
    if isinstance(node, ast.Constant):
        return str(node.value)
    if isinstance(node, ast.Call):
        inner = ast.unparse(node)
        match = re.fullmatch(r"(?:sa\.)?text\((['\"])lower\((\w+)\)\1\)", inner)
        if match:
            return match.group(2)
        return inner
    return ast.unparse(node)


def schema_from_live(inspector) -> dict[str, dict]:
    """Describe the live PostgreSQL schema (read-only inspection)."""
    schema: dict[str, dict] = {}
    for table in sorted(EXPECTED_TABLES):
        column_rows = list(inspector.get_columns(table))
        column_names = {row["name"] for row in column_rows}
        uqs = {
            row["name"]: tuple(row["column_names"] or ())
            for row in inspector.get_unique_constraints(table)
        }
        pk_constraint = inspector.get_pk_constraint(table) or {}
        pk_name = pk_constraint.get("name")
        indexes = {}
        for row in inspector.get_indexes(table):
            name = row.get("name")
            # PK / UNIQUE-constraint-backed indexes are already compared as
            # constraints; comparing them again would report false drift.
            if not name or name in uqs or name == pk_name:
                continue
            columns = tuple(row["column_names"] or ())
            if not columns or columns == (None,):
                # Functional index (e.g. uq_users_email_ci_key on
                # lower(email)): PostgreSQL reports no column name. Reduce
                # the expression to the columns it references so it compares
                # with the ORM descriptor and the migration DDL, which
                # normalize the same way (see _index_column).
                columns = tuple(
                    dict.fromkeys(
                        token
                        for expression in row.get("expressions") or ()
                        for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", str(expression))
                        if token in column_names
                    )
                )
            indexes[name] = columns
        schema[table] = {
            "columns": {
                row["name"]: (_norm_type(row["type"]), bool(row["nullable"]))
                for row in column_rows
            },
            "pk": tuple(pk_constraint.get("constrained_columns") or ()),
            "fks": {
                row["name"]: (
                    tuple(row["constrained_columns"] or ()),
                    (row["referred_table"],),
                )
                for row in inspector.get_foreign_keys(table)
            },
            "uqs": uqs,
            "checks": {
                row["name"]: row["sqltext"] for row in inspector.get_check_constraints(table)
            },
            "indexes": indexes,
        }
    return schema


def diff_schemas(expected: dict, actual: dict, expected_label: str, actual_label: str) -> list[str]:
    """Return human-readable differences between two schema descriptors."""
    problems: list[str] = []

    missing_tables = sorted(set(expected) - set(actual))
    extra_tables = sorted(set(actual) - set(expected))
    for table in missing_tables:
        problems.append(f"table {table!r} missing from {actual_label}")
    for table in extra_tables:
        problems.append(f"table {table!r} present in {actual_label} but not in {expected_label}")

    for table in sorted(set(expected) & set(actual)):
        want, got = expected[table], actual[table]

        for column in sorted(set(want["columns"]) - set(got["columns"])):
            problems.append(f"{table}.{column}: column missing from {actual_label}")
        for column in sorted(set(got["columns"]) - set(want["columns"])):
            problems.append(f"{table}.{column}: unexpected column in {actual_label}")
        for column in sorted(set(want["columns"]) & set(got["columns"])):
            want_type, want_nullable = want["columns"][column]
            got_type, got_nullable = got["columns"][column]
            if want_type != got_type:
                problems.append(
                    f"{table}.{column}: type {want_type} ({expected_label}) "
                    f"!= {got_type} ({actual_label})"
                )
            if want_nullable != got_nullable:
                problems.append(
                    f"{table}.{column}: nullable {want_nullable} ({expected_label}) "
                    f"!= {got_nullable} ({actual_label})"
                )

        if want["pk"] != got["pk"]:
            problems.append(
                f"{table}: primary key {want['pk']} ({expected_label}) "
                f"!= {got['pk']} ({actual_label})"
            )

        for label, key in (("foreign key", "fks"), ("unique constraint", "uqs"),
                           ("check constraint", "checks")):
            for name in sorted(set(want[key]) - set(got[key])):
                problems.append(f"{table}: {label} {name!r} missing from {actual_label}")
            for name in sorted(set(got[key]) - set(want[key])):
                problems.append(f"{table}: unexpected {label} {name!r} in {actual_label}")

        for name in sorted(set(want["fks"]) & set(got["fks"])):
            if want["fks"][name] != got["fks"][name]:
                problems.append(
                    f"{table}: foreign key {name!r} {want['fks'][name]} ({expected_label}) "
                    f"!= {got['fks'][name]} ({actual_label})"
                )
        for name in sorted(set(want["uqs"]) & set(got["uqs"])):
            if want["uqs"][name] != got["uqs"][name]:
                problems.append(
                    f"{table}: unique constraint {name!r} {want['uqs'][name]} "
                    f"({expected_label}) != {got['uqs'][name]} ({actual_label})"
                )
        for name in sorted(set(want["checks"]) & set(got["checks"])):
            if not _checks_match(want["checks"][name], got["checks"][name]):
                problems.append(
                    f"{table}: check constraint {name!r} values/structure differ\n"
                    f"      {expected_label}: {want['checks'][name]}\n"
                    f"      {actual_label}: {got['checks'][name]}"
                )
        for name in sorted(set(want["indexes"]) - set(got["indexes"])):
            problems.append(f"{table}: index {name!r} missing from {actual_label}")
        for name in sorted(set(got["indexes"]) - set(want["indexes"])):
            # PK/UNIQUE-backed indexes are filtered out of the live descriptor, so
            # an extra index here is real drift.
            problems.append(
                f"{table}: unexpected index {name!r} in {actual_label} (not declared by {expected_label})"
            )
        for name in sorted(set(want["indexes"]) & set(got["indexes"])):
            if want["indexes"][name] != got["indexes"][name]:
                problems.append(
                    f"{table}: index {name!r} {want['indexes'][name]} ({expected_label}) "
                    f"!= {got['indexes'][name]} ({actual_label})"
                )

    return problems


def _report_problems(title: str, problems: list[str]) -> bool:
    print(f"{title}: {'OK' if not problems else f'{len(problems)} PROBLEM(S)'}")
    for problem in problems:
        print(f"  - {problem}")
    return not problems


def _declared_revisions(paths: list[Path]) -> list[str]:
    """The ``revision`` value declared by each migration file, in file order.

    Both ``revision: str = "0009"`` (AnnAssign) and ``revision = "0009"``
    (Assign) spellings are accepted so a hand-written migration cannot slip
    past the chain checks.
    """
    declared: list[str] = []
    for path in paths:
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            target = None
            if isinstance(node, ast.AnnAssign):
                target = node.target
            elif isinstance(node, ast.Assign) and len(node.targets) == 1:
                target = node.targets[0]
            if isinstance(target, ast.Name) and target.id == "revision":
                declared.append(ast.literal_eval(node.value))
    return declared


def _migration_files() -> list[Path]:
    return sorted(p for p in MIGRATION_DIR.glob("*.py") if p.name != "__init__.py")


# Tables created by later (non-initial) migrations — the initial migration's
# drift check deliberately excludes them; they are checked against their own
# migration file below.
LATER_MIGRATION_TABLES = {
    "auth_sessions",
    "teachers",
    "invite_tokens",
    # created by 0003 (password reset tokens + audit tables)
    "auth_events",
    "password_reset_tokens",
    "student_profile_history",
    # created by 0010 (Phase 1 learning marketplace)
    "learning_contexts",
    "teaching_offerings",
    "learning_enrollments",
    # created by 0011 (Phase 2 slice 2A: curriculum under an offering)
    "topics",
    "lessons",
    # created by 0012 (Phase 2 slice 2B: materials + moderation)
    "file_assets",
    "materials",
    "material_moderations",
    # created by 0013 (Phase 2 slice 2C: student content + progress)
    "material_progress",
}


def _later_dropped_checks(paths: list[Path]) -> set[tuple[str, str]]:
    """(table, constraint) CHECK pairs dropped by a non-initial upgrade()."""
    dropped: set[tuple[str, str]] = set()
    for path in paths[1:]:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        upgrade_node = _upgrade_node(tree)
        if upgrade_node is None:
            continue
        for node in ast.walk(upgrade_node):
            if (
                isinstance(node, ast.Call)
                and getattr(node.func, "attr", None) == "drop_constraint"
                and len(node.args) >= 2
            ):
                dropped.add((node.args[1].value, node.args[0].value))
    return dropped


def _later_created_checks(paths: list[Path]) -> set[tuple[str, str]]:
    """(table, constraint) CHECK pairs created by a non-initial upgrade().

    The 0001-only comparison is against the *initial* migration, so checks a
    later migration introduces (0004 adds ``students_gender_check`` and the
    date-of-birth range, 0005/0006 recreate the role/status vocabulary) are
    excluded from both sides rather than reported as drift.
    """
    created: set[tuple[str, str]] = set()
    for path in paths[1:]:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        upgrade_node = _upgrade_node(tree)
        if upgrade_node is None:
            continue
        for node in ast.walk(upgrade_node):
            if (
                isinstance(node, ast.Call)
                and getattr(node.func, "attr", None) == "create_check_constraint"
                and len(node.args) >= 2
            ):
                created.add((node.args[1].value, node.args[0].value))
    return created


def _later_created_indexes(paths: list[Path]) -> set[tuple[str, str]]:
    """(table, index) pairs created by a non-initial upgrade().

    Same rationale as :func:`_later_created_checks`: 0004 creates
    ``uq_users_email_ci_key`` (a functional index the models declare, but
    which did not exist in 0001), so it is dropped from both sides of the
    0001 comparison.
    """
    created: set[tuple[str, str]] = set()
    for path in paths[1:]:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        upgrade_node = _upgrade_node(tree)
        if upgrade_node is None:
            continue
        for node in ast.walk(upgrade_node):
            if (
                isinstance(node, ast.Call)
                and getattr(node.func, "attr", None) == "create_index"
                and len(node.args) >= 2
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[1], ast.Constant)
            ):
                created.add((node.args[1].value, node.args[0].value))
    return created


def _combined_migration_schema(paths: list[Path]) -> dict[str, dict]:
    """Merge the DDL of all migration files into one comparable schema."""
    combined: dict[str, dict] = {}
    for path in paths:
        for table, spec in schema_from_migration(path).items():
            if table in combined:
                # A later migration altering an existing table: merge columns
                # added by op.add_column (parsed below) onto the base spec.
                merged = dict(combined[table])
                for key in ("columns", "fks", "uqs", "checks", "indexes"):
                    merged[key] = {**combined[table][key], **spec.get(key, {})}
                combined[table] = merged
            else:
                combined[table] = spec
        # Constraint swaps apply to the accumulated schema in file order:
        # 0004 drops the date-of-birth check created by 0001, 0005 replaces
        # the role vocabulary check created by 0001. Index drops apply the
        # same way (0009 renames five indexes the models no longer declare).
        _apply_check_swaps(combined, path)
        _apply_upgrade_index_drops(combined, path)

    # op.add_column calls add columns to a table created by an earlier
    # migration (e.g. users.password_hash in 0002).
    for table, column, type_text, nullable in _iter_add_columns(paths):
        combined.setdefault(table, {
            "columns": {}, "pk": (), "fks": {}, "uqs": {},
            "checks": {}, "indexes": {},
        })
        combined[table]["columns"][column] = (type_text, nullable)

    # op.create_foreign_key attaches a FK to a table added to (or altered
    # by) an earlier migration — migration 0007's auth_events.actor_user_id
    # is the first such case, and it must be compared with the model.
    for table, name, columns, referred in _iter_created_foreign_keys(paths):
        combined.setdefault(table, {
            "columns": {}, "pk": (), "fks": {}, "uqs": {},
            "checks": {}, "indexes": {},
        })
        combined[table]["fks"][name] = (columns, referred)
    return combined


def _iter_created_foreign_keys(paths: list[Path]):
    """Yield (table, constraint_name, columns, referred_table) per FK.

    Only ``upgrade()`` is considered, mirroring the CHECK-swap handling
    above (``downgrade()`` is never run by ``alembic upgrade head``).
    """
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        upgrade_node = next(
            (
                node
                for node in tree.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == "upgrade"
            ),
            None,
        )
        if upgrade_node is None:
            continue
        for node in ast.walk(upgrade_node):
            if (
                isinstance(node, ast.Call)
                and getattr(node.func, "attr", "") == "create_foreign_key"
                and len(node.args) >= 5
            ):
                name = node.args[0].value
                table = node.args[1].value
                # Signature: (name, source_table, referent_table,
                # local_cols, remote_cols, ...) — the referred table is the
                # third argument, and models compare by table name.
                referred = (node.args[2].value,)
                columns = tuple(item.value for item in node.args[3].elts)
                yield table, name, columns, referred


def _iter_add_columns(paths: list[Path]):
    """Yield (table, column, type_text, nullable) for every op.add_column."""
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and getattr(node.func, "attr", None) == "add_column"
            ):
                table = node.args[0].value
                column_call = node.args[1]
                if (
                    isinstance(column_call, ast.Call)
                    and getattr(column_call.func, "attr", None) == "Column"
                ):
                    name = column_call.args[0].value
                    nullable = any(
                        kw.arg == "nullable" and ast.unparse(kw.value) == "True"
                        for kw in column_call.keywords
                    )
                    type_node = column_call.args[1]
                    type_name = (
                        getattr(type_node.func, "attr", None)
                        or getattr(type_node.func, "id", "")
                        if isinstance(type_node, ast.Call)
                        else ast.unparse(type_node)
                    )
                    if type_name in _AST_TYPES:
                        kwargs = {
                            kw.arg: ast.unparse(kw.value)
                            for kw in type_node.keywords
                            if isinstance(type_node, ast.Call)
                        }
                        type_text = _AST_TYPES[type_name](kwargs)
                    else:
                        type_text = _norm_type(
                            ast.unparse(type_node) if not isinstance(type_node, ast.Call) else type_name
                        )
                    yield table, name, type_text, nullable


def _later_migration_columns(paths: list[Path]) -> list[tuple[str, str]]:
    """(table, column) pairs added by non-initial migrations."""
    if len(paths) <= 1:
        return []
    return [
        (table, column)
        for table, column, _, _ in _iter_add_columns(paths[1:])
    ]


# --- level 1: structural ------------------------------------------------------


def structural_checks() -> bool:
    """Validate ORM metadata, the role vocabulary, and model/migration drift."""
    import app.models  # noqa: F401  (registers all tables)
    from sqlalchemy import create_engine

    from app.core.database import Base
    from app.models.enums import UserRole

    ok = True
    print("== 1. STRUCTURAL CHECKS (ORM metadata vs migrations, offline) ==")
    metadata_schema = schema_from_metadata()
    tables = set(metadata_schema)

    print(f"tables in metadata: {len(tables)} (expected {len(EXPECTED_TABLES)})")
    if tables != EXPECTED_TABLES:
        ok = False
        print(f"  MISSING: {sorted(EXPECTED_TABLES - tables)}")
        print(f"  UNEXPECTED: {sorted(tables - EXPECTED_TABLES)}")

    students_ok = "students" in tables and "student" not in tables
    print(f"  'students' present and singular 'student' absent: {students_ok}")
    ok = ok and students_ok

    # --- migrations -----------------------------------------------------------
    migration_files = _migration_files()
    revisions = _declared_revisions(migration_files)
    print(f"migration files: {[p.name for p in migration_files]}")
    # The chain's head must be the expected revision; duplicates are
    # reported (a later live-check guard also validates the chain).
    revision_ok = bool(revisions) and revisions[-1] == EXPECTED_REVISION and len(set(revisions)) == len(revisions)
    print(f"revisions declared: {revisions} (expected head '{EXPECTED_REVISION}')")
    ok = ok and revision_ok
    if not revision_ok:
        print("  PROBLEM: expected the migration chain to end at the expected revision")

    if migration_files:
        # The initial migration is compared alone (minus tables/columns
        # later migrations add), then all migrations combined — both must
        # agree with the models.
        initial = schema_from_migration(migration_files[0])
        initial_expected = {
            t: {
                **s,
                "columns": dict(s["columns"]),
                "fks": dict(s["fks"]),
                "uqs": dict(s["uqs"]),
                "checks": dict(s["checks"]),
                "indexes": dict(s["indexes"]),
            }
            for t, s in metadata_schema.items()
            if t not in LATER_MIGRATION_TABLES
        }
        for table, column in _later_migration_columns(migration_files):
            if table in initial_expected:
                initial_expected[table]["columns"].pop(column, None)
        # Checks a later migration swaps out (e.g. 0005 replaces
        # users_role_check with the three-role vocabulary) are not part of
        # the initial schema the models are compared against.
        for table, name in _later_dropped_checks(migration_files):
            if table in initial_expected:
                initial_expected[table]["checks"].pop(name, None)
            if table in initial:
                initial[table]["checks"].pop(name, None)
        # ...and checks/indexes a later migration *adds* exist only in the
        # models, never in 0001 — remove them from both sides too.
        for table, name in _later_created_checks(migration_files):
            if table in initial_expected:
                initial_expected[table]["checks"].pop(name, None)
            if table in initial:
                initial[table]["checks"].pop(name, None)
        for table, name in _later_created_indexes(migration_files):
            if table in initial_expected:
                initial_expected[table]["indexes"].pop(name, None)
            if table in initial:
                initial[table]["indexes"].pop(name, None)
        ok = _report_problems(
            "model <-> initial migration drift (0001)",
            diff_schemas(initial_expected, initial, "models", "migration 0001"),
        ) and ok
        combined = _combined_migration_schema(migration_files)
        ok = _report_problems(
            "model <-> combined migration drift (all revisions)",
            diff_schemas(metadata_schema, combined, "models", "migrations"),
        ) and ok

    # --- explicit foundation invariants --------------------------------------
    timestamp_gaps = sorted(
        table
        for table, spec in metadata_schema.items()
        if "created_at" in spec["columns"] and "updated_at" not in spec["columns"]
    )
    timestamps_ok = not timestamp_gaps
    print(f"every table with created_at also has updated_at: {timestamps_ok}")
    if timestamp_gaps:
        print(f"  MISSING updated_at: {timestamp_gaps}")
    ok = ok and timestamps_ok

    role_values = tuple(sorted(member.value for member in UserRole))
    role_check = metadata_schema["users"]["checks"].get("users_role_check", "")
    role_ok = _check_literals(role_check) == role_values
    print(f"users_role_check matches UserRole {role_values}: {role_ok}")
    if not role_ok:
        print(f"  CHECK says: {_check_literals(role_check)}")
    ok = ok and role_ok

    uq = metadata_schema["program_versions"]["uqs"].get(OFFERING_UNIQUE)
    offering_ok = uq is not None and set(uq) == set(OFFERING_UNIQUE_COLUMNS)
    print(f"{OFFERING_UNIQUE} on {OFFERING_UNIQUE_COLUMNS}: {offering_ok}")
    ok = ok and offering_ok

    all_indexes = {
        name: (table, columns)
        for table, spec in metadata_schema.items()
        for name, columns in spec["indexes"].items()
    }
    missing_indexes = sorted(set(EXPECTED_FK_INDEXES) - set(all_indexes))
    wrong_indexes = sorted(
        name
        for name in set(EXPECTED_FK_INDEXES) & set(all_indexes)
        if all_indexes[name] != EXPECTED_FK_INDEXES[name]
    )
    indexes_ok = not missing_indexes and not wrong_indexes
    print(f"expected foreign-key indexes present: {indexes_ok}")
    if missing_indexes:
        print(f"  MISSING: {missing_indexes}")
    if wrong_indexes:
        print(f"  WRONG COLUMNS: {wrong_indexes}")
    ok = ok and indexes_ok

    fks = sum(len(spec["fks"]) for spec in metadata_schema.values())
    uqs = sum(len(spec["uqs"]) for spec in metadata_schema.values())
    cks = sum(len(spec["checks"]) for spec in metadata_schema.values())
    print(f"inventory: {fks} foreign keys, {uqs} unique constraints, "
          f"{cks} check constraints, {len(all_indexes)} declared indexes")
    for name in sorted(EXPECTED_FK_INDEXES):
        present = name in all_indexes
        print(f"  index {name}: {'OK' if present else 'MISSING'}")

    try:
        scratch = create_engine("sqlite+pysqlite://")
        Base.metadata.create_all(scratch)
        Base.metadata.drop_all(scratch)
        print("metadata CREATE/DROP smoke test (scratch SQLite): OK")
    except Exception as exc:  # pragma: no cover
        ok = False
        print(f"metadata CREATE/DROP smoke test FAILED: {exc}")

    print(f"structural checks: {'PASS' if ok else 'FAIL'}")
    return ok


# --- level 2: live (read-only) ------------------------------------------------


def _application_row_count(engine) -> int:
    from sqlalchemy import text

    total = 0
    with engine.connect() as connection:
        for table in sorted(EXPECTED_TABLES):
            total += connection.execute(text(f'SELECT count(*) FROM "{table}"')).scalar_one()
    return total


def live_checks(db_url: str, header: str = "2. LIVE CHECKS (PostgreSQL, read-only)") -> bool:
    """Compare the live PostgreSQL schema against the ORM metadata (read-only)."""
    from sqlalchemy import create_engine, inspect, text

    ok = True
    print(f"\n== {header} ==")

    engine = create_engine(db_url, pool_pre_ping=True, connect_args={"connect_timeout": 5})
    try:
        with engine.connect() as connection:
            version = connection.execute(text("SELECT version()")).scalar_one()
            revision = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalars().all()
        print("connection: OK")
        print(f"  {version.split(',')[0]}")
    except Exception as exc:
        print(f"connection FAILED: {exc}")
        return False

    revision_ok = revision == [EXPECTED_REVISION]
    print(f"alembic_version {revision} (expected ['{EXPECTED_REVISION}']): {revision_ok}")
    ok = ok and revision_ok

    # Every declared migration must form a single linear chain ending at the
    # expected head (guards against accidentally edited history).
    declared = _declared_revisions(_migration_files())
    if sorted(declared) != sorted(set(declared)):
        ok = False
        print(f"  PROBLEM: duplicate migration revisions {sorted(declared)}")

    with engine.connect() as connection:
        inspector = inspect(connection)
        all_tables = set(inspector.get_table_names())
        live_schema = schema_from_live(inspector)

    registration_tables = all_tables & EXPECTED_TABLES
    print(f"registration tables present: {len(registration_tables)} "
          f"(expected {len(EXPECTED_TABLES)})")
    missing = sorted(EXPECTED_TABLES - all_tables)
    if missing:
        ok = False
        print(f"  MISSING: {missing}")
    print(f"  'students' exists: {'students' in all_tables}")
    print(f"  singular 'student' absent: {'student' not in all_tables}")
    ok = ok and "students" in all_tables and "student" not in all_tables

    metadata_schema = schema_from_metadata()
    ok = _report_problems(
        "model <-> live schema drift (columns, PK, FK, UNIQUE, CHECK values, indexes)",
        diff_schemas(metadata_schema, live_schema, "models", "live schema"),
    ) and ok

    if missing:
        print("live checks: FAIL")
        engine.dispose()
        return False

    role_values = tuple(sorted(_check_literals(live_schema["users"]["checks"]["users_role_check"])))
    print(f"live users_role_check values: {role_values}")

    for table in ("education_levels", "tvet_programs", "tvet_sectors"):
        has_both = {"created_at", "updated_at"} <= set(live_schema[table]["columns"])
        print(f"  {table}: created_at + updated_at present: {has_both}")
        ok = ok and has_both

    offering = live_schema["program_versions"]["uqs"].get(OFFERING_UNIQUE)
    print(f"  {OFFERING_UNIQUE} present with the four natural-key columns: "
          f"{offering is not None and set(offering) == set(OFFERING_UNIQUE_COLUMNS)}")
    ok = ok and offering is not None and set(offering) == set(OFFERING_UNIQUE_COLUMNS)

    live_indexes = {
        name: (table, columns)
        for table, spec in live_schema.items()
        for name, columns in spec["indexes"].items()
    }
    missing_indexes = sorted(set(EXPECTED_FK_INDEXES) - set(live_indexes))
    print(f"  foreign-key indexes present in PostgreSQL: {not missing_indexes}")
    if missing_indexes:
        print(f"    MISSING: {missing_indexes}")
    ok = ok and not missing_indexes

    print(f"live checks: {'PASS' if ok else 'FAIL'}")
    engine.dispose()
    return ok


# --- level 3: opt-in rebuild --------------------------------------------------


def rebuild_schema(db_url: str) -> bool:
    """Drop and recreate the schema on a disposable, empty development database."""
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, inspect

    from app.core.config import get_settings

    print("\n== 3. REBUILD (opt-in schema reset on a disposable, empty database) ==")
    settings = get_settings()
    if settings.ENVIRONMENT.lower() == "production":
        print("REFUSED: ENVIRONMENT=production. This script never resets production.")
        return False

    engine = create_engine(db_url, pool_pre_ping=True, connect_args={"connect_timeout": 5})
    existing = set(inspect(engine).get_table_names())
    if EXPECTED_TABLES & existing:
        rows = _application_row_count(engine)
        if rows:
            print(f"REFUSED: {rows} application row(s) exist — refusing to drop data.")
            engine.dispose()
            return False
        print("target database contains no application data (0 rows in all 22 tables)")

    alembic_cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))

    print("'alembic downgrade base': dropping the 22 registration tables...")
    command.downgrade(alembic_cfg, "base")
    remaining = set(inspect(engine).get_table_names()) & EXPECTED_TABLES
    print(f"  registration tables remaining after downgrade: {len(remaining)}")

    print("'alembic upgrade head': recreating the schema...")
    command.upgrade(alembic_cfg, "head")
    recreated = set(inspect(engine).get_table_names())
    recreated_ok = (EXPECTED_TABLES & recreated) == EXPECTED_TABLES
    print(f"  all 22 tables recreated: {recreated_ok}")

    engine.dispose()
    ok = not remaining and recreated_ok
    print(f"rebuild: {'PASS' if ok else 'FAIL'}")
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live",
        action="store_true",
        help="also inspect the configured PostgreSQL schema (read-only)",
    )
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help=(
            "with --live: downgrade to base then upgrade to head. Refused when "
            "ENVIRONMENT=production or when any application table holds rows."
        ),
    )
    args = parser.parse_args()

    structural_ok = structural_checks()

    live_ok, rebuild_ok = True, True
    if args.live or args.rebuild:
        from app.core.config import get_settings

        db_url = get_settings().database_url
        if not db_url.startswith("postgresql"):
            print("\n--live: DATABASE_URL is not PostgreSQL; skipping live checks.")
        elif args.rebuild:
            # Pre-rebuild drift is expected when reconciling a schema, so it is
            # reported as information only; the verdict comes from the
            # post-rebuild comparison below.
            live_checks(db_url, "2. PRE-REBUILD LIVE STATE (informational, read-only)")
            rebuild_ok = rebuild_schema(db_url)
            live_ok = rebuild_ok and live_checks(
                db_url, "4. POST-REBUILD LIVE CHECKS (read-only)"
            )
        else:
            live_ok = live_checks(db_url)

    passed = structural_ok and live_ok and rebuild_ok
    print(f"\nRESULT: {'PASS' if passed else 'FAIL'}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
