"""Pagination value objects and helpers.

The repository layer returns :class:`Page` objects; controllers translate them
into the ``meta`` block of the response envelope.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Generic, Iterable, Mapping, Sequence, TypeVar

T = TypeVar("T")

DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 200


@dataclass(frozen=True)
class PageInfo:
    """Serialisable pagination metadata."""

    page: int
    per_page: int
    total: int
    pages: int
    has_prev: bool
    has_next: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "page": self.page,
            "per_page": self.per_page,
            "total": self.total,
            "pages": self.pages,
            "has_prev": self.has_prev,
            "has_next": self.has_next,
        }


@dataclass
class Page(Generic[T]):
    """A slice of results plus its :class:`PageInfo`."""

    items: Sequence[T] = field(default_factory=list)
    info: PageInfo = field(
        default_factory=lambda: PageInfo(1, DEFAULT_PAGE_SIZE, 0, 0, False, False)
    )

    @property
    def total(self) -> int:
        return self.info.total

    def to_dict(self) -> dict[str, Any]:
        return self.info.to_dict()


def normalise_page_args(
    page: Any = 1,
    per_page: Any = None,
    *,
    default_size: int = DEFAULT_PAGE_SIZE,
    max_size: int = MAX_PAGE_SIZE,
) -> tuple[int, int]:
    """Coerce loose query-string values into a safe ``(page, per_page)``."""
    try:
        page_number = int(page)
    except (TypeError, ValueError):
        page_number = 1
    page_number = max(1, page_number)

    if per_page in (None, ""):
        size = default_size
    else:
        try:
            size = int(per_page)
        except (TypeError, ValueError):
            size = default_size
    size = max(1, min(size, max_size))
    return page_number, size


def build_page_info(page: int, per_page: int, total: int) -> PageInfo:
    """Compute derived pagination fields."""
    pages = math.ceil(total / per_page) if per_page else 0
    return PageInfo(
        page=page,
        per_page=per_page,
        total=total,
        pages=pages,
        has_prev=page > 1,
        has_next=page < pages,
    )


def paginate(
    items: Iterable[T],
    *,
    page: int = 1,
    per_page: int = DEFAULT_PAGE_SIZE,
    total: int | None = None,
) -> Page[T]:
    """Wrap already-sliced ``items`` in a :class:`Page`."""
    materialised = list(items)
    resolved_total = total if total is not None else len(materialised)
    return Page(items=materialised, info=build_page_info(page, per_page, resolved_total))


def page_from_query(query, *, page: int, per_page: int) -> Page:
    """Run a SQLAlchemy ``select()`` as a paginated query (single COUNT)."""
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)
    return Page(
        items=pagination.items,
        info=build_page_info(page, per_page, pagination.total),
    )


def page_of_dicts(
    rows: Sequence[Mapping[str, Any]],
    *,
    page: int,
    per_page: int,
    total: int,
) -> Page[Mapping[str, Any]]:
    """Page already-aggregated dict rows that were counted separately."""
    return Page(items=list(rows), info=build_page_info(page, per_page, total))
