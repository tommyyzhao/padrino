"""Pure SHA-256-based seeded RNG for deterministic game-engine choices.

Each draw expands `sha256(seed_bytes + counter.to_bytes(8, "big"))`, advancing
an internal counter so successive calls produce independent output. Avoids
Python's `random` and `secrets` modules entirely so behavior is fully
reproducible across machines, Python versions, and Python's PRNG internals.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from typing import TypeVar

T = TypeVar("T")

_BLOCK_SIZE = 32


class SeededRng:
    """Deterministic RNG seeded from a string or bytes."""

    __slots__ = ("_counter", "_seed")

    def __init__(self, seed: str | bytes) -> None:
        if isinstance(seed, str):
            self._seed: bytes = seed.encode("utf-8")
        else:
            self._seed = bytes(seed)
        self._counter: int = 0

    def next_bytes(self, n: int) -> bytes:
        """Return the next `n` deterministic bytes from the stream."""
        if n < 0:
            raise ValueError("next_bytes requires n >= 0")
        if n == 0:
            return b""
        out = bytearray()
        while len(out) < n:
            block = hashlib.sha256(self._seed + self._counter.to_bytes(8, "big")).digest()
            self._counter += 1
            out.extend(block)
        return bytes(out[:n])

    def randbelow(self, n: int) -> int:
        """Return a uniformly random int in `[0, n)` using rejection sampling."""
        if n <= 0:
            raise ValueError("randbelow requires n > 0")
        if n == 1:
            return 0
        bit_length = (n - 1).bit_length()
        byte_length = (bit_length + 7) // 8
        mask = (1 << bit_length) - 1
        while True:
            value = int.from_bytes(self.next_bytes(byte_length), "big") & mask
            if value < n:
                return value

    def shuffle(self, items: list[T]) -> list[T]:
        """Return a new list with `items` shuffled via Fisher-Yates."""
        result = list(items)
        for i in range(len(result) - 1, 0, -1):
            j = self.randbelow(i + 1)
            result[i], result[j] = result[j], result[i]
        return result

    def choice(self, items: Sequence[T]) -> T:
        """Return a uniformly random element from `items`."""
        if len(items) == 0:
            raise IndexError("cannot choose from an empty sequence")
        return items[self.randbelow(len(items))]
