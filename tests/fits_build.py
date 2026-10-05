"""Helpers to build small in-memory FITS files for tests and smoke checks."""

from __future__ import annotations

import struct

from fits_cutout import checksum

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


def build_signed_fits(
    bitpix: int,
    width: int,
    height: int,
    pixels,
    *,
    extra_cards=(),
):
    """Assemble a single-HDU FITS file with valid CHECKSUM/DATASUM cards.

    The cards follow the FITS checksum convention: CHECKSUM precedes
    DATASUM and both precede END, DATASUM is a quoted decimal string and
    CHECKSUM is the 16-character ASCII-encoded ones-complement signature
    that makes the whole HDU sum to ``0xFFFFFFFF``.
    """
    base = [
        card("SIMPLE", True),
        card("BITPIX", bitpix),
        card("NAXIS", 2),
        card("NAXIS1", width),
        card("NAXIS2", height),
    ]
    base.extend(extra_cards)

    code = "h" if bitpix == 16 else "i"
    data = struct.pack(f">{len(pixels)}{code}", *pixels)
    data += b"\x00" * (_round_up(len(data), BLOCK_SIZE) - len(data))
    datasum = checksum.calculate_datasum(data)
    datasum_text = str(datasum)
    if len(datasum_text) < 8:
        datasum_text = datasum_text.ljust(8)  # convention: pad short strings
    datasum_card = raw_card(f"DATASUM = '{datasum_text}'")
    zero_checksum_card = raw_card("CHECKSUM= '" + "0" * 16 + "'")

    preliminary = b"".join(base + [zero_checksum_card, datasum_card, card("END")])
    preliminary += b" " * (
        _round_up(len(preliminary), BLOCK_SIZE) - len(preliminary)
    )
    signature = checksum.compute_signature(preliminary, data)
    checksum_card = raw_card(f"CHECKSUM= '{signature}'")

    header = b"".join(base + [checksum_card, datasum_card, card("END")])
    header += b" " * (_round_up(len(header), BLOCK_SIZE) - len(header))
    return header + data


def _round_up(n: int, m: int) -> int:
    return ((n + m - 1) // m) * m
