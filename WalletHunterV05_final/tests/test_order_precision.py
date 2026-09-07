import unittest
from decimal import Decimal

from core.order_precision import normalize_perp_price, normalize_perp_size


class OrderPrecisionTests(unittest.TestCase):
    def test_official_perp_examples_and_integer_exemption(self):
        for value, digits, expected in [
            (1234.5, 0, 1234.5), (1234.56, 0, 1234.6),
            (.001234, 0, .001234), (.0012345, 0, .001235),
            (.01234, 1, .01234), (.012345, 1, .01235),
            (123456, 5, 123456), (123456.7, 5, 123457),
            (79477.123, 5, 79477), (.12345678, 5, .1),
            (88.912345, 2, 88.912), (99999.9, 0, 100000),
            (.000001, 0, .000001), (1, 6, 1),
        ]:
            with self.subTest(value=value, digits=digits):
                self.assertEqual(normalize_perp_price(value, digits), expected)

    def test_rounded_prices_satisfy_both_exchange_limits(self):
        for digits in range(7):
            for price in [.000001, .00012345, .123456, 1.23456, 12.34567, 1234.567, 123456.78, 1000000]:
                try:
                    rounded = normalize_perp_price(price, digits)
                except ValueError:
                    continue  # Prices smaller than this asset's tick are rejected.
                value = Decimal(str(rounded)).normalize()
                if value != value.to_integral_value():
                    self.assertLessEqual(len(value.as_tuple().digits), 5)
                self.assertLessEqual(max(0, -value.as_tuple().exponent), 6 - digits)

    def test_size_always_truncates_and_never_increases_budget(self):
        for size, digits, expected in [(1.0019, 3, 1.001), (.00058, 5, .00058),
                                      (.000009, 5, 0), (999999.99999, 2, 999999.99),
                                      (2.999, 0, 2), (0, 6, 0)]:
            with self.subTest(size=size, digits=digits):
                self.assertEqual(normalize_perp_size(size, digits), expected)
                self.assertLessEqual(normalize_perp_size(size, digits), size)

    def test_invalid_or_subtick_prices_are_rejected(self):
        for price in [0, -1, "nan", "inf", "invalid", .00000001]:
            with self.subTest(price=price), self.assertRaises(ValueError):
                normalize_perp_price(price, 0)
        for size in [-1, float("nan"), float("inf")]:
            with self.assertRaises(ValueError):
                normalize_perp_size(size, 2)
        for digits in [-1, 7, 1.5]:
            with self.assertRaises(ValueError):
                normalize_perp_price(100, digits)


if __name__ == "__main__":
    unittest.main()
