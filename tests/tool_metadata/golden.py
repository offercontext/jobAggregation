from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


_FIXTURES = Path(__file__).parents[1] / "fixtures" / "tool_metadata"

__all__ = ("load_asset", "canonical_bytes", "sha256")


def load_asset(name: str) -> Any:
    candidate = Path(name)
    if (
        type(name) is not str
        or not name
        or candidate.name != name
        or candidate.suffix != ".json"
    ):
        raise ValueError("asset name must be a JSON basename")
    return json.loads((_FIXTURES / name).read_bytes().decode("utf-8"))


def canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def sha256(raw: bytes) -> str:
    if type(raw) is not bytes:
        raise TypeError("raw must be bytes")
    return "sha256:" + hashlib.sha256(raw).hexdigest()
