"""Database handle and shared column mixins.

Imported by ``app/__init__.py`` and by every feature's models/repository.  Kept
in ``core`` so models can import it without importing the application factory
(which would be circular).
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from datetime import datetime
from typing import Iterator

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import DateTime, func
from sqlalchemy.orm import mapped_column

from app.core.time import utcnow

db = SQLAlchemy()

_CAMEL_BOUNDARY = re.compile(r"(?<!^)(?=[A-Z])")


def table_name_for(model_name: str) -> str:
    """``LeaveRequestHistory`` -> ``leave_request_history``."""
    return _CAMEL_BOUNDARY.sub("_", model_name).lower()


class TimestampMixin:
    """Adds UTC ``created_at`` / ``updated_at`` columns.

    ``default=utcnow`` populates the value Python-side so it is available
    immediately after ``flush()``; the server default covers rows written
    outside the ORM.
    """

    created_at = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        server_default=func.now(),
    )
    updated_at = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
        server_default=func.now(),
    )


class CreatedAtMixin:
    """Append-only rows only need ``created_at``."""

    created_at = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        server_default=func.now(),
    )


@contextmanager
def transaction() -> Iterator[None]:
    """Commit on success, roll back on failure.

    Callers (controllers, views, jobs) own the transaction boundary; services
    only flush.  This helper exists so that boundary is explicit and uniform.
    """
    try:
        yield
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise


def touched(instance, *attrs: str) -> None:
    """Flag specific columns dirty so ``onupdate`` fires reliably."""
    from sqlalchemy.orm.attributes import flag_modified

    for attr in attrs:
        flag_modified(instance, attr)
