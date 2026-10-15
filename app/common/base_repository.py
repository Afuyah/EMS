"""The only base class repositories need.

Repositories are the only layer permitted to build SQL.  They never commit:
callers (controllers, views, jobs) own the transaction boundary, repositories
and services only ``flush``.
"""

from __future__ import annotations

from typing import Any, Generic, Iterable, Sequence, Type, TypeVar

from sqlalchemy import Select, func, select

from app.common.pagination import Page, build_page_info
from app.core.database import db

ModelT = TypeVar("ModelT")


class BaseRepository(Generic[ModelT]):
    """Thin CRUD surface over a single mapped model."""

    model: Type[ModelT]

    def __init__(self, model: Type[ModelT] | None = None) -> None:
        if model is not None:
            self.model = model
        if getattr(self, "model", None) is None:
            raise TypeError(f"{type(self).__name__} must declare a model")

    # ---- reads -----------------------------------------------------------
    def get(self, entity_id: Any) -> ModelT | None:
        """Fetch by primary key, or ``None``."""
        return db.session.get(self.model, entity_id)

    def get_or_none(self, **filters: Any) -> ModelT | None:
        """Fetch the first row matching all ``filters``."""
        stmt = self._filtered(select(self.model), filters).limit(1)
        return db.session.execute(stmt).scalar_one_or_none()

    def get_or_raise(self, entity_id: Any, *, not_found=None) -> ModelT:
        """Fetch by primary key, raising the caller's error when absent."""
        instance = self.get(entity_id)
        if instance is None:
            raise (not_found or LookupError(f"{self.model.__name__} {entity_id} not found"))
        return instance

    def list(self, **filters: Any) -> Sequence[ModelT]:
        """All rows matching ``filters``."""
        stmt = self._filtered(select(self.model), filters)
        return db.session.execute(stmt).scalars().all()

    def list_all(self) -> Sequence[ModelT]:
        """Every row (test/seed convenience)."""
        return db.session.execute(select(self.model)).scalars().all()

    def count(self, **filters: Any) -> int:
        """Row count matching ``filters``."""
        stmt = self._filtered(select(func.count()).select_from(self.model), filters)
        return int(db.session.execute(stmt).scalar_one() or 0)

    def exists(self, **filters: Any) -> bool:
        """Whether any row matches ``filters``."""
        stmt = self._filtered(select(self.model), filters).limit(1)
        return db.session.execute(stmt).scalar_one_or_none() is not None

    # ---- writes (flush only) --------------------------------------------
    def create(self, **values: Any) -> ModelT:
        """Instantiate, add and flush; never commits."""
        instance = self.model(**values)
        return self.add(instance)

    def add(self, instance: ModelT) -> ModelT:
        """Add an existing instance and flush."""
        db.session.add(instance)
        db.session.flush()
        return instance

    def update(self, instance: ModelT, **values: Any) -> ModelT:
        """Assign attributes and flush."""
        for key, value in values.items():
            setattr(instance, key, value)
        db.session.flush()
        return instance

    def delete(self, instance: ModelT) -> None:
        """Mark for deletion and flush (the caller commits)."""
        db.session.delete(instance)
        db.session.flush()

    # ---- helpers ---------------------------------------------------------
    def _filtered(self, stmt: Select, filters: dict[str, Any]) -> Select:
        """Apply ``key=value`` filters as ANDed equality predicates."""
        for key, value in filters.items():
            column = getattr(self.model, key, None)
            if column is None:
                raise AttributeError(
                    f"{self.model.__name__} has no column {key!r}"
                )
            stmt = stmt.where(column == value)
        return stmt

    def paginate(
        self,
        stmt: Select,
        *,
        page: int,
        per_page: int,
    ) -> Page:
        """Page a statement (one COUNT plus one SELECT) returning our ``Page``."""
        pagination = db.paginate(stmt, page=page, per_page=per_page, error_out=False)
        return Page(
            items=pagination.items,
            info=build_page_info(page, per_page, pagination.total),
        )

    @staticmethod
    def count_statement(stmt: Select) -> int:
        """Count the rows a statement would return without hydrating them."""
        return int(
            db.session.execute(
                select(func.count()).select_from(stmt.subquery())
            ).scalar_one()
            or 0
        )

    @staticmethod
    def rows(stmt: Select) -> list[dict[str, Any]]:
        """Execute a column-select and return plain dicts.

        The documented exception: read-heavy report/board aggregations return
        dicts instead of ORM models.
        """
        result = db.session.execute(stmt)
        return [dict(row) for row in result.mappings().all()]

    @staticmethod
    def scalar(stmt):
        """Execute a statement expected to yield one scalar."""
        return db.session.execute(stmt).scalar()

    @staticmethod
    def flush() -> None:
        """Flush pending changes without committing."""
        db.session.flush()
