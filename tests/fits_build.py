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
    with_checksum: bool = False,
) -> bytes:
    """Assemble a complete single-HDU FITS file.

    When *with_checksum* is true, DATASUM and CHECKSUM cards are appended
    after *extra_cards* and filled following the FITS checksum keyword
    convention, so the resulting file passes integrity verification.
    """
    cards = [
        card("SIMPLE", True),
        card("BITPIX", bitpix),
        card("NAXIS", 2),
        card("NAXIS1", width),
        card("NAXIS2", height),
    ]
    cards.extend(extra_cards)
    code = "h" if bitpix == 16 else "i"
    data = struct.pack(f">{len(pixels)}{code}", *pixels)
    data += pad_data * (_round_up(len(data), BLOCK_SIZE) - len(data))
    if with_checksum:
        datasum = ones_complement_sum(data)
        cards.append(card("DATASUM", str(datasum)))
        # The convention requires this exact initial value: the encoded
        # checksum is computed with 16 ASCII zeros in the value field.
        cards.append(raw_card("CHECKSUM= '0000000000000000'"))
    cards.append(card("END"))
    header = b"".join(cards)
    header += pad_header * (_round_up(len(header), BLOCK_SIZE) - len(header))
    file_bytes = header + data + extra_tail
    if with_checksum:
        file_bytes = _fill_checksum(file_bytes)
    return file_bytes


def ones_complement_sum(data: bytes) -> int:
    """32-bit 1's complement checksum (big-endian words, end-around carry)."""
    total = 0
    for (word,) in struct.iter_unpack(">I", data):
        total += word
    while total > 0xFFFFFFFF:
        total = (total & 0xFFFFFFFF) + (total >> 32)
    return total


def checksum_encode(hdu_sum: int) -> str:
    """Encode an HDU checksum as the 16-character CHECKSUM value.

    Follows the encoding algorithm of the FITS checksum keyword
    convention: complement the sum, split each of the 4 bytes into 4
    nearly equal parts, offset them by ASCII '0', adjust the pairs until
    every character is alphanumeric, and rotate one place to the right.
    """
    value = ~hdu_sum & 0xFFFFFFFF
    quarters = []
    for shift in (24, 16, 8, 0):
        quotient, remainder = divmod((value >> shift) & 0xFF, 4)
        quarters.append([quotient + remainder, quotient, quotient, quotient])
    # A1 B1 C1 D1 A2 B2 C2 D2 A3 B3 C3 D3 A4 B4 C4 D4, offset by ASCII '0'
    seq = [
        quarters[byte][part] + 0x30 for part in range(4) for byte in range(4)
    ]
    # Adjust each pair (X1, X2) and (X3, X4) of every original byte until
    # both characters are alphanumeric; this conserves their sum.
    for first, second in (
        (0, 4), (8, 12), (1, 5), (9, 13), (2, 6), (10, 14), (3, 7), (11, 15)
    ):
        while not (_is_alnum(seq[first]) and _is_alnum(seq[second])):
            seq[first] += 1
            seq[second] -= 1
    seq = seq[-1:] + seq[:-1]  # rotate one place to the right
    return "".join(chr(value) for value in seq)


def _is_alnum(value: int) -> bool:
    return (
        0x30 <= value <= 0x39
        or 0x41 <= value <= 0x5A
        or 0x61 <= value <= 0x7A
    )


def _fill_checksum(file_bytes: bytes) -> bytes:
    """Replace the zeroed CHECKSUM placeholder with its encoded value."""
    encoded = checksum_encode(ones_complement_sum(file_bytes)).encode("ascii")
    marker = b"CHECKSUM= '0000000000000000'"
    at = file_bytes.find(marker)
    if at < 0:
        raise ValueError("CHECKSUM placeholder card not found")
    start = at + 11  # the value string starts in column 12 of the card
    return file_bytes[:start] + encoded + file_bytes[start + 16:]


def _round_up(n: int, m: int) -> int:
    return ((n + m - 1) // m) * m
