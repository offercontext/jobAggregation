from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from offerpilot.ai.tool_authority import AuthorityPhaseError
from offerpilot.ai.tool_runtime.catalog import ToolCatalog
from offerpilot.ai.tool_runtime.pipeline import execute_prepared, prepare_call
from offerpilot.ai.types import ToolCall
from tests.tool_authority.test_execution_claim import (
    ARGUMENTS_DIGEST,
    _issue,
    _setup,
)
from tests.tool_pipeline.test_pipeline import Recorder, _runtime, _spec


class _SemanticOverrideDict(dict[str, Any]):
    """Hashes like the approved dict but exposes a different executor value."""

    def __getitem__(self, key: str) -> Any:
        if key in {"id", "value"}:
            return 999
        return super().__getitem__(key)

    def get(self, key: str, default: Any = None) -> Any:
        if key in {"id", "value"}:
            return 999
        return super().get(key, default)


ReplacementFactory = Callable[[dict[str, Any]], dict[str, Any]]


@pytest.mark.parametrize(
    "replacement_factory",
    (dict, _SemanticOverrideDict),
    ids=("equal-copy", "semantic-override"),
)
def test_read_rejects_replaced_typed_args_before_executor(
    tmp_path: Path,
    replacement_factory: ReplacementFactory,
) -> None:
    calls = 0

    def executor(args: dict[str, Any], _context: object) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {"id": args["id"]}

    runtime = _runtime(tmp_path, Recorder())
    try:
        spec = _spec(executor=executor)
        call = ToolCall(id="read-replaced", name=spec.name, args='{"id":1}')
        result = prepare_call(
            ToolCatalog([spec], expected_names=(spec.name,)),
            runtime.context,
            call,
            call_identity=runtime.prepare_identity(call),
        )
        prepared = result.prepared
        read_identity = runtime.read_identity(prepared)
        replacement = replacement_factory(dict(prepared.typed_args))
        object.__setattr__(prepared, "typed_args", replacement)

        with pytest.raises(AuthorityPhaseError):
            execute_prepared(
                prepared,
                runtime.context,
                call_identity=read_identity,
            )
        assert calls == 0
    finally:
        runtime.close()


@pytest.mark.parametrize(
    "replacement_factory",
    (dict, _SemanticOverrideDict),
    ids=("equal-copy", "semantic-override"),
)
def test_claimed_write_rejects_replaced_typed_args_before_executor(
    tmp_path: Path,
    replacement_factory: ReplacementFactory,
) -> None:
    calls = 0

    def executor(args: dict[str, Any], _context: object) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {"value": args["value"]}

    factory, authority, context, pending, prepared, prepare_identity, sessions = _setup(
        tmp_path,
        executor,
    )
    try:
        with sessions() as session:
            claim, execute_identity = _issue(
                factory,
                authority,
                pending,
                prepared,
                prepare_identity,
                session,
            )
            replacement = replacement_factory(dict(prepared.typed_args))
            object.__setattr__(prepared, "typed_args", replacement)

            with pytest.raises(AuthorityPhaseError):
                execute_prepared(
                    prepared,
                    context.bind(session),
                    call_identity=execute_identity,
                    execution_claim=claim,
                    locked_effective_args_digest=ARGUMENTS_DIGEST,
                )
        assert calls == 0
    finally:
        factory.close()
