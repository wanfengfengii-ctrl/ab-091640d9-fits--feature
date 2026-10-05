"""HTTP API for the FITS cutout service (standard library only).

Endpoints
---------
GET  /health              -> 200 {"status": "ok"}
POST /api/fits/cutout     -> calibrated cutout of a FITS primary HDU

The cutout endpoint expects the raw FITS file as the request body with
``Content-Type: application/fits`` (at most 16 MiB) and the zero-based
window in the query string: ``?x=&y=&width=&height=``.  The window must
stay inside the image and contain at most 10 000 pixels.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import fits

MAX_FILE_BYTES = 16 * 1024 * 1024  # 16 MiB
MAX_CUTOUT_PIXELS = 10_000

# When rejecting an oversize upload we still drain the body (up to this
# cap) so well-behaved clients receive the 413 instead of a reset.
DRAIN_CAP_BYTES = 64 * 1024 * 1024

HEALTH_PATH = "/health"
CUTOUT_PATH = "/api/fits/cutout"

_INT_PARAM = re.compile(r"[+-]?\d+$")


class ParamError(ValueError):
    """A query parameter is missing, duplicated, or out of range."""


class BodyTooLarge(Exception):
    """The request body exceeds the 16 MiB limit."""


class IncompleteBody(Exception):
    """The request body could not be read as announced."""


def _int_param(params: dict[str, list[str]], name: str, minimum: int) -> int:
    raw = params.get(name)
    if raw is None or len(raw) != 1:
        raise ParamError(f"query parameter '{name}' is required exactly once")
    text = raw[0]
    if not _INT_PARAM.fullmatch(text):
        raise ParamError(
            f"query parameter '{name}' must be an integer, got {text!r}"
        )
    value = int(text)
    if value < minimum:
        raise ParamError(
            f"query parameter '{name}' must be >= {minimum}, got {value}"
        )
    return value


class Handler(BaseHTTPRequestHandler):
    server_version = "fits-cutout/1.0"

    # -- response helpers ------------------------------------------------

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, message: str) -> None:
        self._json(status, {"error": message})

    # -- routes ----------------------------------------------------------

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == HEALTH_PATH:
            self._json(200, {"status": "ok"})
        elif path == CUTOUT_PATH:
            self._error(405, "method not allowed: use POST")
        else:
            self._error(404, "not found")

    def do_PUT(self) -> None:
        self._method_not_allowed()

    def do_DELETE(self) -> None:
        self._method_not_allowed()

    def do_PATCH(self) -> None:
        self._method_not_allowed()

    def _method_not_allowed(self) -> None:
        path = urlparse(self.path).path
        if path in (HEALTH_PATH, CUTOUT_PATH):
            self._error(405, "method not allowed")
        else:
            self._error(404, "not found")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != CUTOUT_PATH:
            self._error(404, "not found")
            return
        self._cutout(parsed.query)

    # -- cutout handler ----------------------------------------------------

    def _cutout(self, query: str) -> None:
        params = parse_qs(query, keep_blank_values=True)
        try:
            x = _int_param(params, "x", 0)
            y = _int_param(params, "y", 0)
            width = _int_param(params, "width", 1)
            height = _int_param(params, "height", 1)
        except ParamError as exc:
            self._error(400, str(exc))
            return

        if width * height > MAX_CUTOUT_PIXELS:
            self._error(
                400,
                f"window of {width}x{height} has {width * height} pixels; "
                f"the limit is {MAX_CUTOUT_PIXELS}",
            )
            return

        content_type = (
            self.headers.get("Content-Type", "").split(";")[0].strip().lower()
        )
        if content_type != "application/fits":
            self._error(415, "Content-Type must be application/fits")
            return

        try:
            body = self._read_body()
        except BodyTooLarge:
            self._error(
                413,
                f"file exceeds the {MAX_FILE_BYTES}-byte (16 MiB) limit",
            )
            return
        except IncompleteBody as exc:
            self._error(400, str(exc))
            return

        digest = hashlib.sha256(body).hexdigest()

        try:
            image = fits.parse(body)
        except fits.FitsError as exc:
            self._error(422, f"invalid FITS file: {exc}")
            return

        if x + width > image.width or y + height > image.height:
            self._error(
                400,
                f"window x={x} y={y} width={width} height={height} exceeds "
                f"image bounds {image.width}x{image.height}",
            )
            return

        rows = fits.cutout_rows(image, x, y, width, height)
        self._json(
            200,
            {
                "x": x,
                "y": y,
                "width": width,
                "height": height,
                "sha256": digest,
                "pixels": rows,
            },
        )

    # -- request body ------------------------------------------------------

    def _read_body(self) -> bytes:
        transfer = self.headers.get("Transfer-Encoding", "").lower()
        if transfer == "chunked":
            return self._read_chunked()
        length_header = self.headers.get("Content-Length")
        if length_header is None:
            raise IncompleteBody("Content-Length header is required")
        try:
            length = int(length_header)
        except ValueError:
            raise IncompleteBody(
                f"invalid Content-Length: {length_header!r}"
            ) from None
        if length < 0:
            raise IncompleteBody(f"invalid Content-Length: {length_header!r}")
        if length > MAX_FILE_BYTES:
            self._drain(min(length, DRAIN_CAP_BYTES))
            raise BodyTooLarge()
        body = self.rfile.read(length)
        if len(body) != length:
            raise IncompleteBody("request body is shorter than Content-Length")
        return body

    def _drain(self, count: int) -> None:
        """Read and discard *count* bytes of request body, best effort."""
        remaining = count
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 1 << 20))
            if not chunk:
                break
            remaining -= len(chunk)

    def _read_chunked(self) -> bytes:
        chunks: list[bytes] = []
        total = 0
        too_large = False
        while True:
            line = self.rfile.readline(65536)
            size_text = line.split(b";")[0].strip()
            try:
                size = int(size_text, 16)
            except ValueError:
                raise IncompleteBody("malformed chunked body") from None
            if size == 0:
                while True:  # consume optional trailers up to a blank line
                    trailer = self.rfile.readline(65536)
                    if trailer in (b"\r\n", b"\n", b""):
                        break
                break
            total += size
            if total > MAX_FILE_BYTES:
                too_large = True
            if total > DRAIN_CAP_BYTES:
                raise BodyTooLarge()
            chunk = self.rfile.read(size)
            if len(chunk) != size:
                raise IncompleteBody("truncated chunked body")
            if not too_large:
                chunks.append(chunk)
            if self.rfile.read(2) != b"\r\n":
                raise IncompleteBody("malformed chunk terminator")
        if too_large:
            raise BodyTooLarge()
        return b"".join(chunks)


def main() -> None:
    port = int(os.environ.get("PORT", "8000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"fits-cutout listening on 0.0.0.0:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
