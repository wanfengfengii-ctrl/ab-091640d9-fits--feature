"""Helpers to build small in-memory FITS files for tests and smoke checks."""

from __future__ import annotations

import struct

BLOCK_SIZE = 2880


def card(keyword: str, value=None) -> bytes:
    """Build one 80-character header card."""
    if value is None:
        text = keyword  # value-less card, e.g. "END"
    elif isinstance(value, bool):
        text = f"{keyword:<8}= {'T' if value else 'F':>20}"
    elif isinstance(value, int):
        text = f"{keyword:<8}= {value:>20}"
    elif isinstance(value, float):
        text = f"{keyword:<8}= {value!r:>20}"
    elif isinstance(value, str):
        text = f"{keyword:<8}= '{value}'"
    else:
        raise TypeError(value)
    return text.ljust(80).encode("ascii")


def raw_card(text: str) -> bytes:
    """Build a card from literal text (for unusual value tokens)."""
    return text.ljust(80).encode("ascii")


def build_fits(
    bitpix: int,
    width: int,
    height: int,
    pixels,
    *,
    extra_cards=(),
    pad_header: bytes = b" ",
    pad_data: bytes = b"\x00",
    extra_tail: bytes = b"",
) -> bytes:
    """Assemble a complete single-HDU FITS file."""
    cards = [
        card("SIMPLE", True),
        card("BITPIX", bitpix),
        card("NAXIS", 2),
        card("NAXIS1", width),
        card("NAXIS2", height),
    ]
    cards.extend(extra_cards)
    cards.append(card("END"))
    header = b"".join(cards)
    header += pad_header * (_round_up(len(header), BLOCK_SIZE) - len(header))
    code = "h" if bitpix == 16 else "i"
    data = struct.pack(f">{len(pixels)}{code}", *pixels)
    data += pad_data * (_round_up(len(data), BLOCK_SIZE) - len(data))
    return header + data + extra_tail


def _round_up(n: int, m: int) -> int:
    return ((n + m - 1) // m) * m
