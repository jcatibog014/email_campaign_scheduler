"""Workspaces never reuse a deleted one's number again, on SQLite.

0001 made `workspaces` with AUTOINCREMENT (sqlite_autoincrement), but later
migrations changed the table in Alembic's batch mode, which on SQLite copies it
into a new table, and the copy lost AUTOINCREMENT. So a new demo could get a
deleted demo's number, and an old job's key with that number stopped the new
demo's replay. This copies the table once more, with AUTOINCREMENT. PostgreSQL
(Neon) never reuses numbers, so nothing happens there.

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-20 23:40:00
"""
from alembic import op

revision = '0010'
down_revision = '0009'
branch_labels = None
depends_on = None


def upgrade():
    if op.get_bind().dialect.name != "sqlite":
        return
    # Copying the rows in sets SQLite's counter to the highest number there now; from then on,
    # numbers only go up, even after the newest demo is deleted.
    with op.batch_alter_table("workspaces", recreate="always", table_kwargs={"sqlite_autoincrement": True}):
        pass


def downgrade():
    pass  # nothing to undo: the table is the same, it just never reuses a number
