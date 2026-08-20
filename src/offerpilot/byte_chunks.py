from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Protocol

DEFAULT_DIGEST_CHUNK_BYTES = 4096


class _ByteDigest(Protocol):
    def update(self, data: bytes, /) -> None:
        ...


def iter_fixed_byte_chunks(
    value: str | bytes,
    *,
    chunk_size: int = DEFAULT_DIGEST_CHUNK_BYTES,
) -> Iterator[bytes]:
    if type(chunk_size) is not int or chunk_size <= 0:
        raise ValueError("chunk size must be a positive integer")
    if type(value) is str:
        encoded = value.encode("utf-8")
    elif type(value) is bytes:
        encoded = value
    else:
        raise TypeError("digest input must be text or bytes")

    for offset in range(0, len(encoded), chunk_size):
        chunk = encoded[offset : offset + chunk_size]
        if len(chunk) > chunk_size:
            raise RuntimeError("digest byte chunk exceeds configured limit")
        yield chunk


def update_digest_in_chunks(
    digest: _ByteDigest,
    value: str | bytes,
    *,
    budget_check: Callable[[], None] | None = None,
    chunk_size: int = DEFAULT_DIGEST_CHUNK_BYTES,
) -> None:
    for chunk in iter_fixed_byte_chunks(value, chunk_size=chunk_size):
        if budget_check is not None:
            budget_check()
        digest.update(chunk)
        if budget_check is not None:
            budget_check()
