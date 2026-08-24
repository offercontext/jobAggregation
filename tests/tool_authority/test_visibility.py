from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from offerpilot.ai.tool_authority.visibility import (
    AuthorityApplicationVisibilityError,
    AuthorityApplicationVisibilityQuery,
    VisibleAuthorityApplication,
)
from offerpilot.db import init_database
from offerpilot.repositories.applications import ApplicationCreate, ApplicationsRepository


class _ExplodingBaseException(BaseException):
    pass


class _Cursor:
    def __init__(self, rows: object = (), *, failure: BaseException | None = None) -> None:
        self._rows = rows
        self._failure = failure

    def fetchall(self) -> object:
        if self._failure is not None:
            raise self._failure
        return self._rows


class _Connection:
    def __init__(self, rows: object = (), *, failure: BaseException | None = None) -> None:
        self.rows = rows
        self.failure = failure
        self.calls: list[tuple[str, dict[str, int]]] = []

    def execute(self, statement: str, parameters: dict[str, int]) -> _Cursor:
        self.calls.append((statement, parameters))
        return _Cursor(self.rows, failure=self.failure)


def _seed(path: Path):
    session_factory = init_database(path)
    applications = ApplicationsRepository(session_factory)
    active = applications.create(ApplicationCreate(company_name="Active", position_name="Role"))
    deleted = applications.create(ApplicationCreate(company_name="Deleted", position_name="Role"))
    applications.delete(deleted.id)
    return session_factory, active.id, deleted.id


def test_source_and_session_adapters_have_identical_bounded_results(tmp_path: Path) -> None:
    session_factory, active_id, deleted_id = _seed(tmp_path / "visibility.db")
    query = AuthorityApplicationVisibilityQuery()

    raw = sqlite3.connect(tmp_path / "visibility.db")
    try:
        with session_factory() as session:
            for application_id, expected in (
                (active_id, VisibleAuthorityApplication(active_id)),
                (deleted_id, None),
                (9223372036854775807, None),
            ):
                assert query.execute_on_source_connection(raw, application_id) == expected
                assert query.execute_on_session(session, application_id) == expected
    finally:
        raw.close()


@pytest.mark.parametrize("application_id", [True, 1.0, "1", 0, -1, 9223372036854775808])
def test_visibility_rejects_non_exact_positive_int64_before_sql(application_id: object) -> None:
    connection = _Connection(((1,),))

    with pytest.raises(AuthorityApplicationVisibilityError):
        AuthorityApplicationVisibilityQuery().execute_on_source_connection(  # type: ignore[arg-type]
            connection, application_id
        )

    assert connection.calls == []


@pytest.mark.parametrize(
    "rows",
    [
        ((1,), (1,)),
        ((1, 2),),
        ((True,),),
        ((2,),),
        ((0,),),
    ],
)
def test_visibility_decoder_rejects_ambiguous_or_malformed_results(rows: object) -> None:
    connection = _Connection(rows)

    with pytest.raises(AuthorityApplicationVisibilityError) as raised:
        AuthorityApplicationVisibilityQuery().execute_on_source_connection(connection, 1)  # type: ignore[arg-type]

    assert "1" not in str(raised.value)


def test_visibility_wraps_ordinary_failures_but_propagates_base_exception() -> None:
    ordinary = _Connection(failure=sqlite3.DatabaseError("private target 37"))
    with pytest.raises(AuthorityApplicationVisibilityError) as raised:
        AuthorityApplicationVisibilityQuery().execute_on_source_connection(ordinary, 37)  # type: ignore[arg-type]
    assert "37" not in str(raised.value)
    assert "private target" not in str(raised.value)
    assert raised.value.__cause__ is None

    fatal = _ExplodingBaseException()
    with pytest.raises(_ExplodingBaseException):
        AuthorityApplicationVisibilityQuery().execute_on_source_connection(  # type: ignore[arg-type]
            _Connection(failure=fatal), 37
        )
