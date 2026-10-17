"""Compare the database schema against the models.

A code deploy that lands before its migration is the most confusing failure this
app can produce: every query touching the new column fails, and the error
surfaces far from its cause — "The correction could not be saved." rather than
"column attendance_daily.sessions does not exist".

Run this in the deployment shell after any deploy::

    flask --app wsgi check-schema

The comparison itself is a pure function over two plain mappings, so it can be
tested without touching a real schema.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from flask import Flask


@dataclass
class SchemaReport:
    """What differs between the code and the database."""

    revision: str | None = None
    missing_tables: list[str] = field(default_factory=list)
    missing_columns: list[str] = field(default_factory=list)
    extra_columns: list[str] = field(default_factory=list)

    @property
    def is_stale(self) -> bool:
        """True when the code expects something the database does not have."""
        return bool(self.missing_tables or self.missing_columns)


def diff_schema(
    expected: dict[str, set[str]],
    actual: dict[str, set[str]],
) -> tuple[list[str], list[str], list[str]]:
    """Compare expected (models) against actual (database).

    Pure: no database access, so the rules are directly testable. Returns
    ``(missing_tables, missing_columns, extra_columns)`` where column entries are
    ``"table.column"``.
    """
    missing_tables: list[str] = []
    missing_columns: list[str] = []
    extra_columns: list[str] = []

    for table_name, columns in expected.items():
        if table_name not in actual:
            missing_tables.append(table_name)
            continue
        present = actual[table_name]
        for column in sorted(columns - present):
            missing_columns.append(f"{table_name}.{column}")
        for column in sorted(present - columns):
            extra_columns.append(f"{table_name}.{column}")

    return missing_tables, missing_columns, extra_columns


def _model_columns() -> dict[str, set[str]]:
    """Every mapped table and its column names, straight from the models."""
    from app.core.database import db

    return {
        table.name: {column.name for column in table.columns}
        for table in db.metadata.sorted_tables
    }


def _database_columns() -> dict[str, set[str]]:
    """Every real table and its columns, as the database reports them."""
    from sqlalchemy import inspect

    from app.core.database import db

    inspector = inspect(db.engine)
    return {
        table_name: {
            column["name"] for column in inspector.get_columns(table_name)
        }
        for table_name in inspector.get_table_names()
    }


def current_revision() -> str | None:
    """The Alembic revision the database reports, or ``None`` if unmigrated."""
    from sqlalchemy import text

    from app.core.database import db

    try:
        row = db.session.execute(text("SELECT version_num FROM alembic_version"))
        return row.scalar_one_or_none()
    except Exception:  # noqa: BLE001 - no table means no migrations have run
        db.session.rollback()
        return None


def check_schema(app: Flask) -> SchemaReport:
    """Compare the live database against the models."""
    missing_tables, missing_columns, extra_columns = diff_schema(
        _model_columns(), _database_columns()
    )
    return SchemaReport(
        revision=current_revision(),
        missing_tables=missing_tables,
        missing_columns=missing_columns,
        extra_columns=extra_columns,
    )
