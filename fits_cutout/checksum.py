"""FITS 4.0 ``CHECKSUM`` / ``DATASUM`` verification (primary HDU).

The FITS checksum convention (FITS 4.0; the ``CHECKSUM``/``DATASUM``
proposal) signs the whole HDU with an unsigned 32-bit ones-complement
sum taken over big-endian words:

* ``DATASUM`` stores the sum of the *data segment*, including the zero
  padding that rounds the data array up to a 2880-byte boundary.
* ``CHECKSUM`` stores an ASCII-encoded value such that, when its own
  value field is replaced by ASCII zeroes (``0x30``), the sum over the
  padded header plus the data segment equals ``0xFFFFFFFF``.

A file therefore verifies exactly when (1) the stored ``DATASUM`` equals
the freshly summed data segment and (2) summing the HDU as it stands
(i.e. with the ``CHECKSUM`` card as written) folds to ``0xFFFFFFFF``.
Every byte of the header, the data array and both padding regions is
covered, so rewriting any protected card or pixel byte is detected.

Only the standard library is used.  The math is cross-checked against
the astropy reference implementation in the test suite.
"""

from __future__ import annotations

import array
import sys
from dataclasses import dataclass

from .fits import CARD_SIZE, FitsError

CHECKSUM_KEYWORD = b"CHECKSUM"
DATASUM_KEYWORD = b"DATASUM "

_MASK32 = 0xFFFFFFFF

# Byte values the CHECKSUM encoding must never produce (0x3A..0x40 and
# 0x5B..0x60): they could be mistaken for FITS syntax or quoting.  A
# legal 16-character CHECKSUM value therefore uses only digits, uppercase
# letters and lowercase letters.
_EXCLUDED_CHARS = frozenset(range(0x3A, 0x41)) | frozenset(range(0x5B, 0x61))
_LEGAL_VALUE_CHARS = frozenset(range(0x30, 0x7B)) - _EXCLUDED_CHARS


class ChecksumError(FitsError):
    """A required CHECKSUM/DATASUM card is missing, malformed, or mismatched."""


@dataclass(frozen=True)
class ChecksumCards:
    """Byte offsets (within the file) of the two checksum cards."""

    checksum_offset: int
    datasum_offset: int


def locate_cards(data: bytes, header_size: int) -> ChecksumCards:
    """Find the unique CHECKSUM/DATASUM cards in the padded primary header.

    Both keywords must each occur exactly once and be value cards
    (``= `` indicator present); this also guards against a duplicate card
    whose copy dropped the indicator.
    """
    checksum_offsets: list[int] = []
    datasum_offsets: list[int] = []
    pos = 0
    while pos < header_size:
        keyword = data[pos : pos + 8]
        if keyword == CHECKSUM_KEYWORD:
            checksum_offsets.append(pos)
        elif keyword == DATASUM_KEYWORD:
            datasum_offsets.append(pos)
        pos += CARD_SIZE
    # Report each keyword's own cardinality, CHECKSUM first: a duplicate
    # is a more precise diagnosis than the other card being absent.
    if len(checksum_offsets) > 1:
        raise ChecksumError(
            f"CHECKSUM card must be unique; found {len(checksum_offsets)} copies"
        )
    if len(datasum_offsets) > 1:
        raise ChecksumError(
            f"DATASUM card must be unique; found {len(datasum_offsets)} copies"
        )
    if not checksum_offsets:
        raise ChecksumError("integrity required but CHECKSUM card is missing")
    if not datasum_offsets:
        raise ChecksumError("integrity required but DATASUM card is missing")
    return ChecksumCards(checksum_offsets[0], datasum_offsets[0])


def verify(data: bytes, header_size: int) -> int:
    """Verify the CHECKSUM/DATASUM of the primary HDU in *data*.

    *header_size* is the 2880-rounded size of the primary header; the
    remainder of *data* is the (padded) data segment.  Returns the
    verified ``DATASUM`` value.

    The check runs on the raw uploaded bytes and strictly before cutout
    extraction; any mismatch raises :class:`ChecksumError` so the HTTP
    layer can answer 422 without touching the pixels.
    """
    cards = locate_cards(data, header_size)

    # Parse both stored values before comparing anything, so a malformed
    # card is reported as a format error rather than a sum mismatch.
    stored_datasum = _parse_datasum(data, cards.datasum_offset)
    _parse_checksum(data, cards.checksum_offset)

    data_sum = _word_sum(data[header_size:])
    if stored_datasum != data_sum:
        raise ChecksumError(
            "DATASUM mismatch: header records "
            f"{stored_datasum} but data segment sums to {data_sum}"
        )

    total = _fold(_word_sum(data[:header_size]) + data_sum)
    if total != _MASK32:
        raise ChecksumError(
            "CHECKSUM mismatch: a protected header, data or padding byte "
            "has changed"
        )
    return data_sum


def _quoted_value(card: bytes, name: str) -> bytes:
    """Return the exact bytes of a quoted string value on an 80-byte card.

    ``CHECKSUM`` and ``DATASUM`` are string keywords per the FITS
    checksum convention, e.g. ``CHECKSUM= 'abc...'``.  The card must use
    the ``= `` indicator, open the string at column 10, and close the
    quote; a trailing ``/ comment`` is allowed.  Returned bytes are the
    raw string contents *with* any internal trailing spaces.
    """
    if card[8:10] != b"= ":
        raise ChecksumError(
            f"{name} card is not a valid value card: columns 9-10 must be '= '"
        )
    opening = 10
    while opening < CARD_SIZE and card[opening] == 0x20:
        opening += 1
    if opening >= CARD_SIZE or card[opening] != 0x27:
        raise ChecksumError(
            f"{name} value must be a quoted string (opening apostrophe missing)"
        )
    end = card.find(b"'", opening + 1)
    if end == -1:
        raise ChecksumError(f"{name} value is missing the closing apostrophe")
    rest = card[end + 1 :].lstrip(b" ")
    if rest and rest[0] != ord("/"):
        raise ChecksumError(
            f"{name} card has unexpected characters after the quoted value"
        )
    return card[opening + 1 : end]


def _parse_datasum(data: bytes, offset: int) -> int:
    """Parse ``DATASUM = '<digits...>'`` into an unsigned 32-bit integer.

    The quoted value contains decimal digits and padding spaces only; it
    may not be empty, signed, or carry internal spaces between digits.
    """
    raw = _quoted_value(data[offset : offset + CARD_SIZE], "DATASUM")
    token = raw.strip(b" ")
    if not token or not token.isdigit():
        raise ChecksumError(
            f"DATASUM value {raw.decode('ascii', 'replace')!r} is not a valid "
            "unsigned decimal integer"
        )
    value = int(token)
    if value > _MASK32:
        raise ChecksumError(f"DATASUM value {value} exceeds the 32-bit range")
    return value


def _parse_checksum(data: bytes, offset: int) -> bytes:
    """Validate the CHECKSUM string value; return its 16 raw bytes."""
    raw = _quoted_value(data[offset : offset + CARD_SIZE], "CHECKSUM")
    if len(raw) != 16:
        raise ChecksumError(
            f"CHECKSUM value must be exactly 16 characters, got {len(raw)}"
        )
    for byte in raw:
        if byte not in _LEGAL_VALUE_CHARS:
            raise ChecksumError(
                "CHECKSUM value contains an illegal character "
                f"0x{byte:02X}; only unpunctuated ASCII letters and digits "
                "are allowed"
            )
    return raw


def _fold(value: int) -> int:
    """Fold a possibly-wide integer into a 32-bit ones-complement sum."""
    while value >> 32:
        value = (value & _MASK32) + (value >> 32)
    return value & _MASK32


def _word_sum(block: bytes) -> int:
    """Unsigned ones-complement sum of a 4-byte-aligned big-endian block."""
    words = array.array("I")
    if words.itemsize != 4:  # pragma no cover - 'I' is 4 bytes per the ABI
        raise RuntimeError("platform 'unsigned int' is not 32 bits wide")
    words.frombytes(block)
    if sys.byteorder == "little":
        words.byteswap()
    return _fold(sum(words))


# ---------------------------------------------------------------------------
# CHECKSUM encoding (used by tests / tooling to construct valid files)
# ---------------------------------------------------------------------------


def encode_byte(value: int) -> list[int]:
    """Encode one checksum byte into four printable ASCII bytes."""
    quotient = value // 4 + ord("0")
    remainder = value % 4
    chars = [quotient + remainder, quotient, quotient, quotient]
    changed = True
    while changed:
        changed = False
        for forbidden in _EXCLUDED_CHARS:
            for j in (0, 2):
                if chars[j] == forbidden or chars[j + 1] == forbidden:
                    chars[j] += 1
                    chars[j + 1] -= 1
                    changed = True
    return chars


def encode_checksum(value: int) -> str:
    """Render a 32-bit checksum as the 16-character CHECKSUM string."""
    value &= _MASK32
    grouped = [0] * 16
    for i in range(4):
        byte = (value >> ((3 - i) * 8)) & 0xFF
        encoded = encode_byte(byte)
        for j in range(4):
            grouped[4 * j + i] = encoded[j]
    rotated = bytes(grouped[(i + 15) % 16] for i in range(16))
    return rotated.decode("ascii")


def calculate_datasum(data_segment: bytes) -> int:
    """Return the DATASUM (ones-complement sum) of a padded data segment."""
    return _word_sum(data_segment)


def compute_signature(header_with_zero_checksum: bytes, data_segment: bytes) -> str:
    """Return the CHECKSUM string for an assembled HDU.

    *header_with_zero_checksum* must be the padded header with the
    CHECKSUM value field already set to sixteen ``0`` characters;
    *data_segment* is the padded data block.
    """
    header_sum = _word_sum(header_with_zero_checksum)
    data_sum = _word_sum(data_segment)
    total = _fold(header_sum + data_sum)
    return encode_checksum(_MASK32 ^ total)
