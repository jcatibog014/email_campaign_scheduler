"""The Alembic migrations make exactly the tables that models.py describes."""
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect

from oncue import create_app, db
from oncue.db import Base


def test_upgrade_on_an_empty_database_matches_the_models(tmp_path):
    app = create_app({"TESTING": True, "DATABASE": str(tmp_path / "fresh.sqlite"), "DATABASE_URL": None})
    with app.app_context():
        db.upgrade_db()
        db.upgrade_db()  # running it again changes nothing
        with db.engine().connect() as connection:
            context = MigrationContext.configure(connection, opts={"compare_type": True})
            differences = compare_metadata(context, Base.metadata)
            tables = set(inspect(connection).get_table_names())
    # Nothing left for "alembic revision --autogenerate" to write: the
    # migrations and the models agree. If this fails after changing
    # models.py, make a new migration (see AGENTS.md).
    assert differences == []
    assert {"workspaces", "users", "rate_limits", "alembic_version"} <= tables


def test_tables_that_must_never_reuse_a_number_keep_autoincrement(tmp_path):
    """Batch migrations copy a SQLite table and can lose AUTOINCREMENT. Then a new demo got a
    deleted demo's number, and the old demo's replay job (same key) stopped the new one's replay."""
    app = create_app({"TESTING": True, "DATABASE": str(tmp_path / "numbers.sqlite"), "DATABASE_URL": None})
    with app.app_context():
        db.upgrade_db()
        with db.engine().connect() as connection:
            made = dict(connection.exec_driver_sql("SELECT name, sql FROM sqlite_master WHERE type = 'table'").all())
    wanted = [table.name for table in Base.metadata.sorted_tables if table.dialect_kwargs.get("sqlite_autoincrement")]
    assert {"workspaces", "users"} <= set(wanted)
    for name in wanted:
        assert "AUTOINCREMENT" in made[name].upper(), name


def test_upgrade_db_command(tmp_path):
    app = create_app({"TESTING": True, "DATABASE": str(tmp_path / "cli.sqlite"), "DATABASE_URL": None})
    result = app.test_cli_runner().invoke(args=["upgrade-db"])
    assert result.exit_code == 0, result.output
    assert "up to date" in result.output
