"""Bitset primitives.

The v4 store used Python ints as bitsets, which is the right *representation*
and the wrong *access pattern*. Three costs were quadratic:

    ingest         table[k] |= 1 << i           allocates an i-bit integer per
                                                 record per indexed key, so
                                                 ingesting n records costs
                                                 O(n^2 / 64) word operations.

    materialise    [r for i, r in enumerate(records) if bitmap >> i & 1]
                                                 `bitmap >> i` allocates a new
                                                 big integer on every iteration:
                                                 O(n^2 / 64).

    count          bin(bitmap).count("1")        builds an n-character string
                                                 per call.

All three are replaced here. The representation stays a Python int, because
`a & b` on two big integers is a C-speed word-wise AND and nothing in pure
Python beats it. What changes is how bitmaps are *built* and *consumed*:

    build          postings accumulate as plain lists (O(1) append) and are
                   converted to a bitmap once, lazily, via a bytearray:
                   O(n/8 + k) instead of O(k * n/64).

    consume        set bits are extracted byte-at-a-time from a single
                   to_bytes() call: O(n/8 + k).

    count          int.bit_count(), which is a C intrinsic on 3.10+.

Every function here is pure and total. There is no partially-built state that
a caller can observe.
"""

from __future__ import annotations

from typing import Iterable, Sequence

__all__ = [
    "count", "indices", "from_indices", "first_index", "is_empty",
    "universe", "iter_masked",
]


def count(bitmap: int) -> int:
    """Population count. O(n/64) in C rather than O(n) in Python."""
    if bitmap < 0:
        raise ValueError("bitmaps are non-negative; got a negative int")
    return bitmap.bit_count()


def is_empty(bitmap: int) -> bool:
    return bitmap == 0


def universe(n: int) -> int:
    """All-ones bitmap over n slots."""
    if n < 0:
        raise ValueError("universe size must be non-negative")
    return (1 << n) - 1


def indices(bitmap: int) -> list[int]:
    """Positions of set bits, ascending.

    One to_bytes() allocation, then byte-local bit extraction. Skipping zero
    bytes wholesale is what makes sparse bitmaps cheap: a bitmap with k bits
    set over n slots costs O(n/8) byte scans plus O(k) bit extractions, not
    O(n) big-integer shifts.
    """
    if bitmap == 0:
        return []
    if bitmap < 0:
        raise ValueError("bitmaps are non-negative; got a negative int")
    raw = bitmap.to_bytes((bitmap.bit_length() + 7) // 8, "little")
    out: list[int] = []
    append = out.append
    for byte_i, byte in enumerate(raw):
        if not byte:
            continue
        base = byte_i << 3
        while byte:
            lsb = byte & -byte
            append(base + lsb.bit_length() - 1)
            byte ^= lsb
    return out


def first_index(bitmap: int) -> int | None:
    """Lowest set bit, or None. O(1)-ish: one bit_length on the LSB."""
    if bitmap == 0:
        return None
    lsb = bitmap & -bitmap
    return lsb.bit_length() - 1


def from_indices(idx: Iterable[int], n_bits: int) -> int:
    """Build a bitmap from positions.

    A bytearray is mutable and indexed in O(1), so this is O(n/8 + k). The
    naive `sum(1 << i for i in idx)` is O(k * n/64) and is the single line
    that made the previous ingest path quadratic.
    """
    ba = bytearray((n_bits + 7) // 8)
    for i in idx:
        if i < 0 or i >= n_bits:
            raise IndexError(f"bit {i} outside universe of {n_bits}")
        ba[i >> 3] |= 1 << (i & 7)
    return int.from_bytes(bytes(ba), "little")


def iter_masked(bitmap: int, seq: Sequence):
    """Yield seq[i] for each set bit i. Skips the list build in `indices`."""
    for i in indices(bitmap):
        if i < len(seq):
            yield seq[i]
