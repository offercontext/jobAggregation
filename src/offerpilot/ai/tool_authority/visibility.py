from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Iterable

from sqlalchemy import text
from sqlalchemy.orm import Session


_MAX_INT64 = 9_223_372_036_854_775_807
_APPLICATION_VISIBILITY_SQL = """\
SELECT id
FROM applications
WHERE id = :application_id AND deleted_at IS NULL
LIMIT 2
"""


class AuthorityApplicationVisibilityError(RuntimeError):
    """The bounded visibility decision could not be established safely."""


@dataclass(frozen=True, slots=True)
class VisibleAuthorityApplication:
    application_id: int

    def __post_init__(self) -> None:
        _require_application_id(self.application_id)


@dataclass(frozen=True, slots=True)
class AuthorityApplicationVisibilityQuery:
    """Connection-neutral active-Application visibility query.

    Both adapters execute the same named-parameter SQL definition and pass
    their bounded rows through the same strict decoder.  The caller retains
    ownership of the connection, Session, and transaction snapshot.
    """

    def execute_on_source_connection(
        self,
        connection: sqlite3.Connection,
        application_id: int,
    ) -> VisibleAuthorityApplication | None:
        expected_id = _require_application_id(application_id)
        try:
            cursor = connection.execute(
                _APPLICATION_VISIBILITY_SQL,
                {"application_id": expected_id},
            )
            rows = cursor.fetchall()
            return _decode_visibility_rows(rows, expected_id=expected_id)
        except AuthorityApplicationVisibilityError:
            raise
        except Exception:
            raise AuthorityApplicationVisibilityError(
                "application visibility query failed"
            ) from None

    def execute_on_session(
        self,
        session: Session,
        application_id: int,
    ) -> VisibleAuthorityApplication | None:
        expected_id = _require_application_id(application_id)
        try:
            rows = session.execute(
                text(_APPLICATION_VISIBILITY_SQL),
                {"application_id": expected_id},
            ).all()
            return _decode_visibility_rows(rows, expected_id=expected_id)
        except AuthorityApplicationVisibilityError:
            raise
        except Exception:
            raise AuthorityApplicationVisibilityError(
                "application visibility query failed"
            ) from None


def _require_application_id(application_id: object) -> int:
    if type(application_id) is not int or not 0 < application_id <= _MAX_INT64:
        raise AuthorityApplicationVisibilityError("application visibility identity is invalid")
    return application_id


def _decode_visibility_rows(
    raw_rows: Iterable[Iterable[object]],
    *,
    expected_id: int,
) -> VisibleAuthorityApplication | None:
    try:
        rows: tuple[tuple[object, ...], ...] = tuple(tuple(row) for row in raw_rows)
    except Exception:
        raise AuthorityApplicationVisibilityError(
            "application visibility result is invalid"
        ) from None
    if not rows:
        return None
    if len(rows) != 1 or len(rows[0]) != 1:
        raise AuthorityApplicationVisibilityError("application visibility result is ambiguous")
    actual_id = rows[0][0]
    if type(actual_id) is not int or actual_id != expected_id:
        raise AuthorityApplicationVisibilityError("application visibility result is invalid")
    return VisibleAuthorityApplication(actual_id)


__all__ = [
    "AuthorityApplicationVisibilityError",
    "AuthorityApplicationVisibilityQuery",
    "VisibleAuthorityApplication",
]
