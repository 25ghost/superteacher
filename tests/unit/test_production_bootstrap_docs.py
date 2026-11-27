"""Check the documented bootstrap commands without opening a database."""
from pathlib import Path
import re
import shlex

from scripts import create_admin, load_catalog
from scripts.apply_guard import production_apply_refusal

ROOT = Path(__file__).resolve().parents[2]


def _readme_section(heading: str) -> str:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    match = re.search(
        rf"^### {re.escape(heading)}\s*$\n(.*?)(?=^#{{1,3}} |\Z)",
        readme,
        flags=re.MULTILINE | re.DOTALL,
    )
    assert match is not None, heading
    return match.group(1)


def _python_commands(section: str, prefix: str = "python ") -> list[list[str]]:
    return [
        shlex.split(line)
        for raw in section.splitlines()
        if (line := raw.strip().removeprefix("#").strip()).startswith(prefix)
    ]


def test_production_bootstrap_uses_ordered_supported_commands():
    section = _readme_section("Production database initialization")
    commands = _python_commands(section)
    assert production_apply_refusal("production") is not None
    assert "seed_reference_data.py" not in section
    assert "mvp_demo_catalog" not in section
    assert [command[:2] if command[1] != "-m" else command for command in commands] == [
        ["python", "-m", "alembic", "upgrade", "head"],
        ["python", "-m", "alembic", "current"],
        ["python", "-m", "alembic", "heads"],
        ["python", "scripts/verify_database.py"],
        ["python", "scripts/load_catalog.py"],
        ["python", "scripts/load_catalog.py"],
        ["python", "scripts/create_admin.py"],
    ]
    assert commands[3][2:] == ["--live"]
    assert "current_database()" in section
    assert section.index("recovery readiness checks") < section.index("alembic upgrade")
    assert "GET /api/v1/health/ready" in section
    assert "GET /api/v1/me" in section


def test_production_bootstrap_authorization_and_operator_inputs():
    section = _readme_section("Production database initialization")
    commands = _python_commands(section)
    preview, apply = [
        load_catalog.parse_args(command[2:])
        for command in commands if command[1] == "scripts/load_catalog.py"
    ]
    admin, = [
        create_admin.parse_args(command[2:])
        for command in commands if command[1] == "scripts/create_admin.py"
    ]
    assert preview.csv_dir == apply.csv_dir == "$CATALOG_CSV_DIR"
    assert not preview.apply
    assert apply.apply and apply.allow_production
    assert admin.email == "$ADMIN_EMAIL" and admin.allow_production

    prose = " ".join(section.split())
    assert f"`{load_catalog.CONFIRM_ENV}={load_catalog.CONFIRM_PHRASE}`" in prose
    assert "explicit operator confirmation and still requires `--allow-production`" in prose
    assert "Keep `ENVIRONMENT=production` throughout bootstrap" in prose
    assert not re.search(r"ENVIRONMENT\s*=\s*(development|testing)", section, re.I)
    assert "reviewed, authoritative CSVs" in prose
    assert "the effective database for apply" in prose
    assert not re.search(r"(?:ADMIN_PASSWORD|DB_PASSWORD|SECRET_KEY|DATABASE_URL)\s*=", section)


def test_compose_comments_match_local_bootstrap_commands():
    section = _readme_section("Local Docker Compose")
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    prefix = "docker compose exec api "
    commands = _python_commands(section, prefix)
    assert commands == _python_commands(compose, prefix)
    assert commands == [
        ["docker", "compose", "exec", "api", "python", "-m", "alembic", "upgrade", "head"],
        ["docker", "compose", "exec", "api", "python", "-m", "alembic", "current"],
        ["docker", "compose", "exec", "api", "python", "scripts/create_admin.py", "--email", "$ADMIN_EMAIL"],
    ]
    admin = create_admin.parse_args(commands[-1][6:])
    assert admin.email == "$ADMIN_EMAIL"
    assert "for local development" in section
    assert "separately approved infrastructure for production" in " ".join(section.split())
    assert "ENVIRONMENT=development" in section and "ENVIRONMENT=development" in compose
