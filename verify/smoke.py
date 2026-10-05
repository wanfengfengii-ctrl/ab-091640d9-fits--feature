#!/usr/bin/env python3
"""End-to-end smoke test against a running fits-cutout service.

Waits for /health, then exercises the cutout API with in-memory FITS
files (valid and deliberately corrupt) and checks exact responses.
Exits 0 only if every check passes.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "tests"))
import fits_build  # noqa: E402

BASE = os.environ.get("APP_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tests", "data")

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"[smoke] ok   {name}", flush=True)
    else:
        print(f"[smoke] FAIL {name} {detail}", flush=True)
        failures.append(name)


def wait_for_health(timeout: float = 60.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(BASE + "/health", timeout=2) as resp:
                if resp.status == 200:
                    print("[smoke] service is healthy", flush=True)
                    return
        except Exception:
            pass
        time.sleep(1)
    check("health", False, f"no healthy response from {BASE} within {timeout}s")
    raise SystemExit(1)


def post_cutout(body: bytes, query: str, content_type="application/fits"):
    req = urllib.request.Request(
        f"{BASE}/api/fits/cutout?{query}",
        data=body,
        method="POST",
        headers={"Content-Type": content_type},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def main() -> int:
    wait_for_health()

    # 1. BITPIX=16 with BLANK / BSCALE / BZERO ------------------------------
    pixels = list(range(12))
    pixels[6] = -32768  # row 1, col 2 hits BLANK
    body = fits_build.build_fits(
        16,
        4,
        3,
        pixels,
        extra_cards=[
            fits_build.card("BLANK", -32768),
            fits_build.raw_card("BSCALE  = 0.1"),
            fits_build.raw_card("BZERO   = 10"),
        ],
    )
    status, payload = post_cutout(body, "x=1&y=1&width=3&height=2")
    check("bitpix16 status", status == 200, f"got {status}: {payload}")
    check(
        "bitpix16 window size",
        payload.get("width") == 3 and payload.get("height") == 2,
        str(payload),
    )
    check(
        "bitpix16 sha256",
        payload.get("sha256") == hashlib.sha256(body).hexdigest(),
        str(payload.get("sha256")),
    )
    check(
        "bitpix16 pixels",
        payload.get("pixels")
        == [["10.5", None, "10.7"], ["10.9", "11", "11.1"]],
        str(payload.get("pixels")),
    )

    # 2. BITPIX=32, exact decimal rendering ---------------------------------
    body32 = fits_build.build_fits(
        32,
        2,
        2,
        [5, -250, 0, 123456],
        extra_cards=[fits_build.raw_card("BSCALE  = 1.000E-03")],
    )
    status, payload = post_cutout(body32, "x=0&y=0&width=2&height=2")
    check("bitpix32 status", status == 200, f"got {status}: {payload}")
    check(
        "bitpix32 pixels",
        payload.get("pixels") == [["0.005", "-0.25"], ["0", "123.456"]],
        str(payload.get("pixels")),
    )

    # 3. integrity=required: valid CHECKSUM/DATASUM --------------------------
    signed_pixels = list(range(12))
    signed_pixels[6] = -32768
    signed = fits_build.build_signed_fits(
        16,
        4,
        3,
        signed_pixels,
        extra_cards=[
            fits_build.card("BLANK", -32768),
            fits_build.raw_card("BSCALE  = 0.1"),
            fits_build.raw_card("BZERO   = 10"),
        ],
    )
    status, payload = post_cutout(
        signed, "x=1&y=1&width=3&height=2&integrity=required"
    )
    check("integrity valid status", status == 200, f"got {status}: {payload}")
    check(
        "integrity valid marker",
        payload.get("integrityVerified") is True,
        str(payload),
    )
    check(
        "integrity valid sha256 on uploaded bytes",
        payload.get("sha256") == hashlib.sha256(signed).hexdigest(),
        str(payload.get("sha256")),
    )
    check(
        "integrity valid pixels",
        payload.get("pixels")
        == [["10.5", None, "10.7"], ["10.9", "11", "11.1"]],
        str(payload.get("pixels")),
    )

    # 3b. integrity=required with an independent astropy-signed fixture ------
    with open(os.path.join(DATA_DIR, "checksum_int16.fits"), "rb") as handle:
        fixture = handle.read()
    status, payload = post_cutout(
        fixture, "x=1&y=1&width=3&height=2&integrity=required"
    )
    check(
        "integrity astropy fixture accepted",
        status == 200 and payload.get("integrityVerified") is True,
        f"got {status}: {payload}",
    )

    # 3c. integrity=required rejects every protected-byte / card problem ----
    unsigned = fits_build.build_fits(16, 4, 3, list(range(12)))
    status, payload = post_cutout(
        unsigned, "x=0&y=0&width=1&height=1&integrity=required"
    )
    check("integrity missing cards -> 422", status == 422, f"got {status}")
    check(
        "integrity failure has no pixels",
        "pixels" not in payload,
        str(payload),
    )

    tampered_pixel = bytearray(signed)
    tampered_pixel[2880 + 4] ^= 0xFF
    status, payload = post_cutout(
        bytes(tampered_pixel),
        "x=1&y=1&width=3&height=2&integrity=required",
    )
    check("integrity pixel tamper -> 422", status == 422, f"got {status}")
    check("integrity pixel tamper no pixels", "pixels" not in payload)

    tampered_header = bytearray(signed)
    bscale_at = bytes(signed).find(b"BSCALE  = 0.1")
    tampered_header[bscale_at + 13] = ord("9")
    status, payload = post_cutout(
        bytes(tampered_header),
        "x=1&y=1&width=3&height=2&integrity=required",
    )
    check("integrity header tamper -> 422", status == 422, f"got {status}")
    check("integrity header tamper no pixels", "pixels" not in payload)

    duplicate = fits_build.build_fits(
        16,
        4,
        3,
        list(range(12)),
        extra_cards=[
            fits_build.raw_card("CHECKSUM= '" + "a" * 16 + "'"),
            fits_build.raw_card("CHECKSUM= '" + "b" * 16 + "'"),
            fits_build.raw_card("DATASUM = '1'"),
        ],
    )
    status, _ = post_cutout(
        duplicate, "x=0&y=0&width=1&height=1&integrity=required"
    )
    check("integrity duplicate card -> 422", status == 422, f"got {status}")

    bad_datasum = bytearray(signed)
    ds_at = bytes(signed).find(b"DATASUM = '") + len("DATASUM = '")
    bad_datasum[ds_at] = ord("x")
    status, _ = post_cutout(
        bytes(bad_datasum),
        "x=1&y=1&width=3&height=2&integrity=required",
    )
    check("integrity malformed DATASUM -> 422", status == 422, f"got {status}")

    status, _ = post_cutout(
        signed, "x=0&y=0&width=1&height=1&integrity=optional"
    )
    check("integrity bad param value -> 400", status == 400, f"got {status}")

    # 3d. Regression: omitting integrity keeps the original behaviour -------
    status, payload = post_cutout(
        unsigned, "x=1&y=1&width=3&height=2"
    )
    check(
        "legacy unsigned request still 200",
        status == 200 and "integrityVerified" not in payload,
        f"got {status}: {payload}",
    )

    # 4. Error handling: every bad input must yield a 4xx --------------------
    valid = fits_build.build_fits(16, 4, 3, list(range(12)))
    status, _ = post_cutout(valid, "x=3&y=0&width=2&height=1")
    check("out-of-bounds window -> 4xx", 400 <= status < 500, f"got {status}")

    status, _ = post_cutout(valid, "x=0&y=0&width=101&height=100")
    check("window over 10000 pixels -> 4xx", 400 <= status < 500, f"got {status}")

    status, _ = post_cutout(valid, "x=0&y=0&width=1")
    check("missing parameter -> 4xx", 400 <= status < 500, f"got {status}")

    status, _ = post_cutout(valid, "x=0&y=0&width=1&height=1", "text/plain")
    check("wrong content type -> 4xx", 400 <= status < 500, f"got {status}")

    status, _ = post_cutout(valid[:3000], "x=0&y=0&width=1&height=1")
    check("truncated file -> 4xx", 400 <= status < 500, f"got {status}")

    status, _ = post_cutout(valid + b"\x00" * 2880, "x=0&y=0&width=1&height=1")
    check("trailing content -> 4xx", 400 <= status < 500, f"got {status}")

    nan_body = fits_build.build_fits(
        16, 1, 1, [0], extra_cards=[fits_build.raw_card("BSCALE  = NaN")]
    )
    status, _ = post_cutout(nan_body, "x=0&y=0&width=1&height=1")
    check("non-finite BSCALE -> 4xx", 400 <= status < 500, f"got {status}")

    if failures:
        print(f"[smoke] {len(failures)} check(s) FAILED", flush=True)
        return 1
    print("[smoke] all checks passed", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
