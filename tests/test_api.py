"""HTTP API tests: run the real server on an ephemeral port."""

from __future__ import annotations

import hashlib
import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from fits_cutout import server
from tests import fits_build


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)

    # -- helpers ---------------------------------------------------------

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def post(self, body: bytes, query: str, content_type="application/fits"):
        req = urllib.request.Request(
            self.url(f"/api/fits/cutout?{query}"),
            data=body,
            method="POST",
            headers={"Content-Type": content_type},
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def get(self, path: str):
        try:
            with urllib.request.urlopen(self.url(path), timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    # -- health ------------------------------------------------------------

    def test_health(self):
        status, payload = self.get("/health")
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"status": "ok"})

    # -- happy path ----------------------------------------------------------

    def test_cutout_success(self):
        pixels = list(range(12))
        pixels[6] = -32768  # row 1, col 2 -> BLANK
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
        status, payload = self.post(body, "x=1&y=1&width=3&height=2")
        self.assertEqual(status, 200)
        self.assertEqual(payload["width"], 3)
        self.assertEqual(payload["height"], 2)
        self.assertEqual(payload["sha256"], hashlib.sha256(body).hexdigest())
        self.assertEqual(
            payload["pixels"],
            [["10.5", None, "10.7"], ["10.9", "11", "11.1"]],
        )

    def test_bitpix32_defaults(self):
        body = fits_build.build_fits(32, 2, 2, [1, 2, 3, 4])
        status, payload = self.post(body, "x=0&y=0&width=2&height=2")
        self.assertEqual(status, 200)
        self.assertEqual(payload["pixels"], [["1", "2"], ["3", "4"]])

    # -- window validation ----------------------------------------------------

    def test_window_out_of_bounds(self):
        body = fits_build.build_fits(16, 4, 3, list(range(12)))
        status, payload = self.post(body, "x=3&y=0&width=2&height=1")
        self.assertEqual(status, 400)
        self.assertIn("bounds", payload["error"])

    def test_window_row_out_of_bounds(self):
        body = fits_build.build_fits(16, 4, 3, list(range(12)))
        status, _ = self.post(body, "x=0&y=2&width=1&height=2")
        self.assertEqual(status, 400)

    def test_pixel_limit(self):
        body = fits_build.build_fits(16, 4, 3, list(range(12)))
        status, payload = self.post(body, "x=0&y=0&width=101&height=100")
        self.assertEqual(status, 400)
        self.assertIn("pixels", payload["error"])

    def test_pixel_limit_boundary_ok(self):
        # 100x100 window = exactly 10 000 pixels must be accepted
        body = fits_build.build_fits(16, 100, 100, [7] * 10000)
        status, payload = self.post(body, "x=0&y=0&width=100&height=100")
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["pixels"]), 100)

    def test_negative_coordinate(self):
        body = fits_build.build_fits(16, 4, 3, list(range(12)))
        status, _ = self.post(body, "x=-1&y=0&width=1&height=1")
        self.assertEqual(status, 400)

    def test_zero_width(self):
        body = fits_build.build_fits(16, 4, 3, list(range(12)))
        status, _ = self.post(body, "x=0&y=0&width=0&height=1")
        self.assertEqual(status, 400)

    def test_non_integer_param(self):
        body = fits_build.build_fits(16, 4, 3, list(range(12)))
        status, _ = self.post(body, "x=0.5&y=0&width=1&height=1")
        self.assertEqual(status, 400)

    def test_missing_param(self):
        body = fits_build.build_fits(16, 4, 3, list(range(12)))
        status, _ = self.post(body, "x=0&y=0&width=1")
        self.assertEqual(status, 400)

    def test_duplicate_param(self):
        body = fits_build.build_fits(16, 4, 3, list(range(12)))
        status, _ = self.post(body, "x=0&x=1&y=0&width=1&height=1")
        self.assertEqual(status, 400)

    # -- request validation ---------------------------------------------------

    def test_wrong_content_type(self):
        body = fits_build.build_fits(16, 1, 1, [0])
        status, _ = self.post(body, "x=0&y=0&width=1&height=1", "image/png")
        self.assertEqual(status, 415)

    def test_oversize_body_rejected(self):
        body = b"\x00" * (16 * 1024 * 1024 + 2880)
        status, payload = self.post(body, "x=0&y=0&width=1&height=1")
        self.assertEqual(status, 413)
        self.assertIn("16 MiB", payload["error"])

    # -- FITS validation surfaced as 4xx ---------------------------------------

    def test_corrupt_file_is_422(self):
        body = fits_build.build_fits(16, 1, 1, [0]) + b"garbage-block".ljust(2880)
        status, payload = self.post(body, "x=0&y=0&width=1&height=1")
        self.assertEqual(status, 422)
        self.assertIn("trailing content", payload["error"])

    def test_truncated_file_is_422(self):
        body = fits_build.build_fits(16, 100, 100, [0] * 10000)[:3000]
        status, _ = self.post(body, "x=0&y=0&width=1&height=1")
        self.assertEqual(status, 422)

    def test_non_finite_calibration_is_422(self):
        body = fits_build.build_fits(
            16, 1, 1, [0], extra_cards=[fits_build.raw_card("BSCALE  = NaN")]
        )
        status, payload = self.post(body, "x=0&y=0&width=1&height=1")
        self.assertEqual(status, 422)
        self.assertIn("finite", payload["error"])

    def test_wrong_bitpix_is_422(self):
        body = fits_build.build_fits(16, 1, 1, [0])
        bad = body[:80] + fits_build.card("BITPIX", 64) + body[160:]
        status, _ = self.post(bad, "x=0&y=0&width=1&height=1")
        self.assertEqual(status, 422)

    # -- routing ----------------------------------------------------------------

    def test_get_on_cutout_is_405(self):
        status, _ = self.get("/api/fits/cutout")
        self.assertEqual(status, 405)

    def test_unknown_path_is_404(self):
        status, _ = self.get("/nope")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
