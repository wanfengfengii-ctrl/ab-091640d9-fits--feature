"""Strict, minimal FITS primary-HDU parsing and cutout extraction.

Only what the quality-control pipeline needs is accepted:

* a single primary HDU (``SIMPLE = T``, no extensions, no trailing bytes),
* ``NAXIS = 2`` with ``BITPIX`` 16 or 32 (big-endian signed integers),
* 80-character printable-ASCII header cards terminated by an ``END`` card,
* header and data segments each padded to a 2880-byte boundary
  (spaces for the header, zero bytes for the data).

Anything else raises :class:`FitsError`, which the HTTP layer maps to a
4xx response.  Pixel calibration (``BSCALE``/``BZERO``) is applied with
:class:`decimal.Decimal` so results are exact and never rounded through
binary floating point.

The module also implements the verification half of the FITS checksum
keyword convention (CHECKSUM/DATASUM): :func:`verify_checksums` confirms
that the header, data and padding bytes of the primary HDU are intact by
accumulating 32-bit 1's complement checksums and comparing them with the
recorded keyword values.
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext

BLOCK_SIZE = 2880
CARD_SIZE = 80

# Keywords this service interprets; duplicates of these are rejected.
STRUCTURAL_KEYWORDS = frozenset(
    {
        "SIMPLE",
        "XTENSION",
        "BITPIX",
        "NAXIS",
        "NAXIS1",
        "NAXIS2",
        "PCOUNT",
        "GCOUNT",
        "BLANK",
        "BSCALE",
        "BZERO",
    }
)

_INT_TOKEN = re.compile(r"[+-]?\d+$")

# Decimal precision used when applying BSCALE/BZERO.  A raw pixel has at
# most 10 significant digits and a header value field at most ~70, so 100
# significant digits keeps every multiplication and addition exact.
CALIBRATION_PRECISION = 100


class FitsError(ValueError):
    """The uploaded file is not a valid or supported FITS primary HDU."""


@dataclass(frozen=True)
class FitsImage:
    """A parsed primary HDU ready for cutout extraction."""

    bitpix: int
    width: int  # NAXIS1, columns
    height: int  # NAXIS2, rows
    blank: int | None
    bscale: Decimal
    bzero: Decimal
    pixels: tuple[int, ...]  # raw signed integers, row-major order


def parse(data: bytes) -> FitsImage:
    """Parse and fully validate a FITS file contained in *data*."""
    if not data:
        raise FitsError("empty file")
    if len(data) % BLOCK_SIZE != 0:
        raise FitsError(
            f"file size {len(data)} is not a multiple of {BLOCK_SIZE} bytes"
        )

    cards, header_size = _read_header(data)
    values = _header_values(cards)

    bitpix = _required_int(values, "BITPIX")
    if bitpix not in (16, 32):
        raise FitsError(f"unsupported BITPIX={bitpix}; only 16 and 32 are accepted")

    naxis = _required_int(values, "NAXIS")
    if naxis != 2:
        raise FitsError(
            f"unsupported NAXIS={naxis}; only 2-dimensional images are accepted"
        )

    width = _required_int(values, "NAXIS1")
    height = _required_int(values, "NAXIS2")
    if width < 1 or height < 1:
        raise FitsError("NAXIS1 and NAXIS2 must be positive integers")

    pcount = _optional_int(values, "PCOUNT")
    if pcount not in (None, 0):
        raise FitsError("PCOUNT must be 0 for a primary image HDU")
    gcount = _optional_int(values, "GCOUNT")
    if gcount not in (None, 1):
        raise FitsError("GCOUNT must be 1 for a primary image HDU")

    blank = _optional_int(values, "BLANK")
    bscale = _optional_decimal(values, "BSCALE", Decimal(1))
    bzero = _optional_decimal(values, "BZERO", Decimal(0))

    bytes_per_pixel = bitpix // 8
    pixel_count = width * height
    data_len = pixel_count * bytes_per_pixel
    data_start = header_size
    data_end = data_start + data_len
    expected_size = data_start + _round_up(data_len, BLOCK_SIZE)

    if len(data) < data_end:
        raise FitsError(
            f"file is truncated: data array needs {data_len} bytes "
            f"but only {max(0, len(data) - data_start)} are present"
        )
    if len(data) != expected_size:
        raise FitsError(
            "unexpected trailing content: file must contain exactly one "
            "primary HDU (no extensions or extra bytes)"
        )
    if any(data[data_end:expected_size]):
        raise FitsError("data padding after the image array must be zero bytes")

    code = "h" if bitpix == 16 else "i"
    pixels = struct.unpack(
        f">{pixel_count}{code}", data[data_start:data_end]
    )
    return FitsImage(
        bitpix=bitpix,
        width=width,
        height=height,
        blank=blank,
        bscale=bscale,
        bzero=bzero,
        pixels=pixels,
    )


def cutout_rows(
    image: FitsImage, x: int, y: int, width: int, height: int
) -> list[list[str | None]]:
    """Extract a calibrated cutout as rows of decimal strings.

    Rows follow image row order (increasing y); each row lists columns in
    increasing x.  Pixels equal to ``BLANK`` become ``None`` (JSON null);
    every other pixel is ``BZERO + BSCALE * raw`` rendered by
    :func:`format_decimal`.
    """
    rows: list[list[str | None]] = []
    with localcontext() as ctx:
        ctx.prec = CALIBRATION_PRECISION
        for row in range(y, y + height):
            base = row * image.width
            line: list[str | None] = []
            for col in range(x, x + width):
                raw = image.pixels[base + col]
                if image.blank is not None and raw == image.blank:
                    line.append(None)
                else:
                    value = Decimal(raw) * image.bscale + image.bzero
                    line.append(format_decimal(value))
            rows.append(line)
    return rows


def format_decimal(value: Decimal) -> str:
    """Render *value* without exponent and without redundant trailing zeros."""
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if text in ("", "-0"):
        text = "0"
    return text


# ---------------------------------------------------------------------------
# integrity verification (FITS checksum keyword convention)

# The CHECKSUM value is a 16-character alphanumeric string; the DATASUM
# value is the decimal representation of the 32-bit data checksum.
_CHECKSUM_TOKEN = re.compile(r"[0-9A-Za-z]{16}$")
_DATASUM_TOKEN = re.compile(r"[0-9]+$")

# An intact HDU accumulates to 1's complement negative zero (all bits set).
_CHECKSUM_VERIFIED = 0xFFFFFFFF


def ones_complement_sum(data) -> int:
    """Return the 32-bit 1's complement checksum of *data*.

    The bytes are interpreted as unsigned 32-bit big-endian integers and
    accumulated with end-around carry, as specified by the FITS checksum
    keyword convention for the 2880-byte logical records of an HDU.
    *data* (bytes or memoryview) must be a multiple of 4 bytes long.
    """
    if len(data) % 4:
        raise FitsError("checksum input is not a multiple of 4 bytes")
    total = 0
    offset = 0
    size = len(data)
    while offset < size:
        count = min(BLOCK_SIZE // 4, (size - offset) // 4)
        total += sum(struct.unpack_from(f">{count}I", data, offset))
        offset += count * 4
    while total > 0xFFFFFFFF:
        total = (total & 0xFFFFFFFF) + (total >> 32)
    return total


def verify_checksums(data: bytes, image: FitsImage) -> None:
    """Verify the CHECKSUM and DATASUM keywords of a parsed HDU.

    *data* must be the exact bytes that produced *image* via
    :func:`parse`.  Both keywords must be present exactly once and in the
    format defined by the FITS checksum convention, and the recorded
    values must match the 1's complement checksums computed over the
    header, data and padding bytes of the HDU.  Any violation raises
    :class:`FitsError`.
    """
    cards, header_size = _read_header(data)
    checksums, datasums = _checksum_cards(cards)

    for name, values in (("CHECKSUM", checksums), ("DATASUM", datasums)):
        if not values:
            raise FitsError(f"{name} keyword is missing")
        if len(values) > 1:
            raise FitsError(f"duplicate {name} keyword in header")

    checksum = checksums[0]
    if checksum is None:
        raise FitsError("CHECKSUM value must be a character string")
    if not _CHECKSUM_TOKEN.fullmatch(checksum):
        raise FitsError(
            "CHECKSUM value must be a 16-character alphanumeric string"
        )

    datasum = datasums[0]
    if datasum is None:
        raise FitsError("DATASUM value must be a character string")
    token = datasum.strip()
    if not _DATASUM_TOKEN.fullmatch(token):
        raise FitsError(
            "DATASUM value must be a decimal string "
            "(the data unit checksum)"
        )
    datasum_value = int(token)
    if datasum_value > 0xFFFFFFFF:
        raise FitsError(
            f"DATASUM value {datasum_value} exceeds the 32-bit checksum range"
        )

    data_len = image.width * image.height * (image.bitpix // 8)
    data_start = header_size
    data_end = data_start + _round_up(data_len, BLOCK_SIZE)
    view = memoryview(data)
    header_sum = ones_complement_sum(view[:data_start])
    data_sum = ones_complement_sum(view[data_start:data_end])

    if datasum_value != data_sum:
        raise FitsError(
            f"DATASUM mismatch: keyword records {datasum_value} but the "
            f"data unit checksum computes to {data_sum}"
        )
    total = header_sum + data_sum
    total = (total & 0xFFFFFFFF) + (total >> 32)
    if total != _CHECKSUM_VERIFIED:
        raise FitsError(
            "CHECKSUM mismatch: the header, data, or padding bytes do not "
            "match the recorded checksum"
        )


# ---------------------------------------------------------------------------
# header parsing


def _round_up(value: int, multiple: int) -> int:
    return ((value + multiple - 1) // multiple) * multiple


def _read_header(data: bytes) -> tuple[list[bytes], int]:
    """Return (cards, padded header size); validates cards, END and padding."""
    cards: list[bytes] = []
    pos = 0
    while True:
        if pos + CARD_SIZE > len(data):
            raise FitsError("header is missing the END card")
        card = data[pos : pos + CARD_SIZE]
        for byte in card:
            if byte < 0x20 or byte > 0x7E:
                raise FitsError(
                    f"header card {len(cards)} contains non-printable "
                    f"byte 0x{byte:02X}"
                )
        cards.append(card)
        pos += CARD_SIZE
        if card[:8] == b"END     ":
            break
    header_size = _round_up(pos, BLOCK_SIZE)
    if header_size > len(data):
        raise FitsError("file is truncated inside the header padding")
    if data[pos:header_size].strip(b" "):
        raise FitsError("header padding after the END card must be space bytes")
    return cards, header_size


def _header_values(cards: list[bytes]) -> dict[str, str]:
    """Collect structural keyword tokens; reject non-primary/duplicate cards."""
    if cards[0][:8] != b"SIMPLE  ":
        raise FitsError("first header card must be SIMPLE (single primary HDU)")
    values: dict[str, str] = {}
    for card in cards:
        keyword = card[:8].decode("ascii").strip()
        if keyword in ("", "END", "COMMENT", "HISTORY"):
            continue
        if keyword == "XTENSION":
            raise FitsError(
                "XTENSION found: file must start with a primary HDU, "
                "not an extension"
            )
        if card[8:10] != b"= ":
            continue
        if keyword in STRUCTURAL_KEYWORDS:
            if keyword in values:
                raise FitsError(f"duplicate {keyword} keyword in header")
            values[keyword] = _value_token(card)
    if values.get("SIMPLE") != "T":
        raise FitsError("SIMPLE must be T")
    return values


def _value_token(card: bytes) -> str:
    """Extract the value token of a card, stopping at a comment '/'."""
    field = card[10:].decode("ascii")
    out: list[str] = []
    in_string = False
    i = 0
    while i < len(field):
        ch = field[i]
        if ch == "'":
            if in_string and i + 1 < len(field) and field[i + 1] == "'":
                i += 2  # escaped quote inside a string value
                continue
            in_string = not in_string
        elif ch == "/" and not in_string:
            break
        else:
            out.append(ch)
        i += 1
    return "".join(out).strip()


def _checksum_cards(cards: list[bytes]) -> tuple[list[str | None], list[str | None]]:
    """Collect the string values of CHECKSUM and DATASUM cards.

    Only value cards (``= `` in columns 9-10) are considered; each entry
    is the card's string value, or ``None`` if the value is not a
    properly quoted string.
    """
    checksums: list[str | None] = []
    datasums: list[str | None] = []
    for card in cards:
        keyword = card[:8].decode("ascii").strip()
        if keyword not in ("CHECKSUM", "DATASUM") or card[8:10] != b"= ":
            continue
        value = _quoted_string(card)
        if keyword == "CHECKSUM":
            checksums.append(value)
        else:
            datasums.append(value)
    return checksums, datasums


def _quoted_string(card: bytes) -> str | None:
    """Extract a quoted-string card value; None if not a string value.

    Doubled quotes inside the string are one literal quote; trailing
    blanks (insignificant in FITS strings) are stripped.  Anything but a
    comment after the closing quote makes the value invalid.
    """
    field = card[10:].decode("ascii")
    i = 0
    while i < len(field) and field[i] == " ":
        i += 1
    if i >= len(field) or field[i] != "'":
        return None
    i += 1
    out: list[str] = []
    while True:
        if i >= len(field):
            return None  # unterminated string
        ch = field[i]
        if ch == "'":
            if i + 1 < len(field) and field[i + 1] == "'":
                out.append("'")
                i += 2
                continue
            i += 1
            break
        out.append(ch)
        i += 1
    rest = field[i:].strip()
    if rest and not rest.startswith("/"):
        return None
    return "".join(out).rstrip()


def _to_int(token: str, name: str) -> int:
    if not _INT_TOKEN.fullmatch(token):
        raise FitsError(f"{name} value {token!r} is not an integer")
    return int(token)


def _required_int(values: dict[str, str], name: str) -> int:
    token = values.get(name)
    if token is None:
        raise FitsError(f"mandatory keyword {name} is missing")
    return _to_int(token, name)


def _optional_int(values: dict[str, str], name: str) -> int | None:
    token = values.get(name)
    return None if token is None else _to_int(token, name)


def _optional_decimal(
    values: dict[str, str], name: str, default: Decimal
) -> Decimal:
    token = values.get(name)
    if token is None:
        return default
    # FITS allows Fortran-style 'D' exponents; normalise before parsing.
    text = token.replace("D", "E").replace("d", "e")
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise FitsError(
            f"{name} value {token!r} is not a valid decimal number"
        ) from None
    if not value.is_finite():
        raise FitsError(f"{name} must be a finite number, got {token!r}")
    return value
