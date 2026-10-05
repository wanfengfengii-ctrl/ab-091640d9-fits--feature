"""Unit tests for the strict FITS parser and cutout extraction."""

from __future__ import annotations

import unittest
from decimal import Decimal

from fits_cutout import fits
from tests import fits_build


def make_image(**kwargs) -> fits.FitsImage:
    return fits.parse(fits_build.build_fits(**kwargs))


class FormatDecimalTests(unittest.TestCase):
    def test_integer_has_no_decimal_point(self):
        self.assertEqual(fits.format_decimal(Decimal("42")), "42")

    def test_trailing_zeros_stripped(self):
        self.assertEqual(fits.format_decimal(Decimal("1.500")), "1.5")
        self.assertEqual(fits.format_decimal(Decimal("2.00")), "2")

    def test_no_exponent_for_large_magnitudes(self):
        self.assertEqual(fits.format_decimal(Decimal("1E+5")), "100000")
        self.assertEqual(fits.format_decimal(Decimal("1.23E+7")), "12300000")

    def test_no_exponent_for_small_magnitudes(self):
        self.assertEqual(fits.format_decimal(Decimal("1E-5")), "0.00001")

    def test_negative_zero_normalised(self):
        self.assertEqual(fits.format_decimal(Decimal("-0.00")), "0")

    def test_negative_value(self):
        self.assertEqual(fits.format_decimal(Decimal("-3.25")), "-3.25")


class ParseValidTests(unittest.TestCase):
    def test_bitpix16_minimal(self):
        img = make_image(bitpix=16, width=2, height=2, pixels=[1, -2, 3, -4])
        self.assertEqual(img.bitpix, 16)
        self.assertEqual((img.width, img.height), (2, 2))
        self.assertEqual(img.pixels, (1, -2, 3, -4))
        self.assertIsNone(img.blank)
        self.assertEqual(img.bscale, Decimal(1))
        self.assertEqual(img.bzero, Decimal(0))

    def test_bitpix32_extremes(self):
        img = make_image(
            bitpix=32, width=2, height=1, pixels=[-2147483648, 2147483647]
        )
        self.assertEqual(img.pixels, (-2147483648, 2147483647))

    def test_calibration_keywords(self):
        img = make_image(
            bitpix=16,
            width=1,
            height=1,
            pixels=[0],
            extra_cards=[
                fits_build.card("BLANK", -1),
                fits_build.raw_card("BSCALE  =                 0.25"),
                fits_build.raw_card("BZERO   =              1.0D+01"),
            ],
        )
        self.assertEqual(img.blank, -1)
        self.assertEqual(img.bscale, Decimal("0.25"))
        self.assertEqual(img.bzero, Decimal("1.0E+1"))

    def test_comment_after_value_is_ignored(self):
        img = make_image(
            bitpix=16,
            width=1,
            height=1,
            pixels=[0],
            extra_cards=[fits_build.raw_card("BSCALE  = 2.5 / scale factor")],
        )
        self.assertEqual(img.bscale, Decimal("2.5"))

    def test_pcount_gcount_defaults_accepted(self):
        img = make_image(
            bitpix=16,
            width=1,
            height=1,
            pixels=[0],
            extra_cards=[
                fits_build.card("PCOUNT", 0),
                fits_build.card("GCOUNT", 1),
            ],
        )
        self.assertEqual(img.width, 1)


class ParseStructuralErrorTests(unittest.TestCase):
    def assert_fits_error(self, data: bytes, needle: str):
        with self.assertRaises(fits.FitsError) as ctx:
            fits.parse(data)
        self.assertIn(needle, str(ctx.exception))

    def test_empty_file(self):
        self.assert_fits_error(b"", "empty")

    def test_size_not_multiple_of_2880(self):
        data = fits_build.build_fits(16, 1, 1, [0])[:-1]
        self.assert_fits_error(data, "multiple of 2880")

    def test_missing_end_card(self):
        data = fits_build.build_fits(16, 1, 1, [0])
        # overwrite the END card with a comment card
        end_at = 5 * 80
        data = data[:end_at] + fits_build.card("COMMENT") + data[end_at + 80 :]
        with self.assertRaises(fits.FitsError):
            fits.parse(data)

    def test_non_printable_header_byte(self):
        data = bytearray(fits_build.build_fits(16, 1, 1, [0]))
        data[100] = 0x07
        self.assert_fits_error(bytes(data), "non-printable")

    def test_first_card_must_be_simple(self):
        data = fits_build.build_fits(16, 1, 1, [0])
        bad = fits_build.card("XTENSION", "IMAGE   ") + data[80:]
        self.assert_fits_error(bad, "primary HDU")

    def test_simple_must_be_true(self):
        data = fits_build.build_fits(16, 1, 1, [0])
        bad = fits_build.card("SIMPLE", False) + data[80:]
        self.assert_fits_error(bad, "SIMPLE must be T")

    def test_unsupported_bitpix(self):
        data = fits_build.build_fits(16, 1, 1, [0])
        bad = data[:80] + fits_build.card("BITPIX", 8) + data[160:]
        self.assert_fits_error(bad, "BITPIX=8")

    def test_unsupported_naxis(self):
        data = fits_build.build_fits(16, 1, 1, [0])
        bad = data[:160] + fits_build.card("NAXIS", 3) + data[240:]
        self.assert_fits_error(bad, "NAXIS=3")

    def test_missing_naxis1(self):
        cards = b"".join(
            [
                fits_build.card("SIMPLE", True),
                fits_build.card("BITPIX", 16),
                fits_build.card("NAXIS", 2),
                fits_build.card("NAXIS2", 1),
                fits_build.card("END"),
            ]
        )
        header = cards.ljust(2880, b" ")
        self.assert_fits_error(header + b"\x00" * 2880, "NAXIS1")

    def test_zero_axis_rejected(self):
        data = fits_build.build_fits(16, 1, 1, [0])
        bad = data[:240] + fits_build.card("NAXIS1", 0) + data[320:]
        self.assert_fits_error(bad, "positive")

    def test_truncated_data(self):
        data = fits_build.build_fits(16, 100, 100, [0] * 10000)
        # cut at a 2880 boundary so the alignment check passes but the
        # data array is incomplete
        self.assert_fits_error(data[: 2880 * 2], "truncated")

    def test_trailing_extension_rejected(self):
        data = fits_build.build_fits(16, 1, 1, [0], extra_tail=b"\x00" * 2880)
        self.assert_fits_error(data, "trailing content")

    def test_header_padding_must_be_spaces(self):
        data = fits_build.build_fits(16, 1, 1, [0], pad_header=b"\x00")
        self.assert_fits_error(data, "header padding")

    def test_data_padding_must_be_zero(self):
        data = fits_build.build_fits(16, 1, 1, [0], pad_data=b" ")
        self.assert_fits_error(data, "data padding")

    def test_duplicate_keyword_rejected(self):
        data = fits_build.build_fits(
            16, 1, 1, [0], extra_cards=[fits_build.card("BITPIX", 16)]
        )
        self.assert_fits_error(data, "duplicate BITPIX")

    def test_pcount_must_be_zero(self):
        data = fits_build.build_fits(
            16, 1, 1, [0], extra_cards=[fits_build.card("PCOUNT", 1)]
        )
        self.assert_fits_error(data, "PCOUNT")

    def test_gcount_must_be_one(self):
        data = fits_build.build_fits(
            16, 1, 1, [0], extra_cards=[fits_build.card("GCOUNT", 2)]
        )
        self.assert_fits_error(data, "GCOUNT")

    def test_blank_must_be_integer(self):
        data = fits_build.build_fits(
            16, 1, 1, [0], extra_cards=[fits_build.raw_card("BLANK   = 1.5")]
        )
        self.assert_fits_error(data, "BLANK")

    def test_bscale_must_be_numeric(self):
        data = fits_build.build_fits(
            16, 1, 1, [0], extra_cards=[fits_build.raw_card("BSCALE  = abc")]
        )
        self.assert_fits_error(data, "BSCALE")

    def test_non_finite_bscale_rejected(self):
        data = fits_build.build_fits(
            16, 1, 1, [0], extra_cards=[fits_build.raw_card("BSCALE  = Inf")]
        )
        self.assert_fits_error(data, "finite")

    def test_non_finite_bzero_rejected(self):
        data = fits_build.build_fits(
            16, 1, 1, [0], extra_cards=[fits_build.raw_card("BZERO   = NaN")]
        )
        self.assert_fits_error(data, "finite")


class CutoutTests(unittest.TestCase):
    def test_blank_pixels_become_none(self):
        img = make_image(
            bitpix=16,
            width=3,
            height=1,
            pixels=[1, -1, 3],
            extra_cards=[fits_build.card("BLANK", -1)],
        )
        self.assertEqual(fits.cutout_rows(img, 0, 0, 3, 1), [["1", None, "3"]])

    def test_calibration_is_exact(self):
        img = make_image(
            bitpix=16,
            width=3,
            height=1,
            pixels=[5, 10, 11],
            extra_cards=[
                fits_build.raw_card("BSCALE  = 0.1"),
                fits_build.raw_card("BZERO   = 10"),
            ],
        )
        self.assertEqual(
            fits.cutout_rows(img, 0, 0, 3, 1), [["10.5", "11", "11.1"]]
        )

    def test_no_exponent_no_trailing_zeros(self):
        img = make_image(
            bitpix=32,
            width=4,
            height=1,
            pixels=[5, -250, 0, 123456],
            extra_cards=[fits_build.raw_card("BSCALE  = 1.000E-03")],
        )
        self.assertEqual(
            fits.cutout_rows(img, 0, 0, 4, 1),
            [["0.005", "-0.25", "0", "123.456"]],
        )

    def test_row_order_and_window(self):
        # 4x3 image, rows 0..2, values 0..11
        img = make_image(bitpix=16, width=4, height=3, pixels=list(range(12)))
        rows = fits.cutout_rows(img, 1, 1, 3, 2)
        self.assertEqual(rows, [["5", "6", "7"], ["9", "10", "11"]])

    def test_bitpix32_large_values(self):
        img = make_image(bitpix=32, width=2, height=1, pixels=[2147483647, -1])
        self.assertEqual(fits.cutout_rows(img, 0, 0, 2, 1), [["2147483647", "-1"]])


if __name__ == "__main__":
    unittest.main()
