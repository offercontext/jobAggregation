from __future__ import annotations

import json
from pathlib import Path
from typing import Any


FIXTURES = Path(__file__).parents[1] / "fixtures" / "tool_authority"
BASELINE = "2427fa6"


def load_golden(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
