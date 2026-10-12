import unittest

from bot import Bar, atr, ema, parse_bars, rsi


class IndicatorsTests(unittest.TestCase):
    def test_ema(self):
        self.assertEqual(ema([1.0, 2.0, 3.0], 3), [1.0, 1.5, 2.25])

    def test_rsi_rising_series(self):
        self.assertEqual(rsi([1, 2, 3, 4, 5, 6, 7, 8], 7), 100.0)

    def test_rsi_flat_series(self):
        self.assertEqual(rsi([2.0] * 10, 7), 50.0)

    def test_atr_uses_true_range(self):
        bars = [
            Bar("1", 10.0, 11.0, 9.0, 10.0),
            Bar("2", 12.0, 13.0, 11.5, 12.5),
        ]
        self.assertAlmostEqual(atr(bars, 14), 3.0)

    def test_parse_bars_discards_open_bar_and_sorts(self):
        payload = {"bars": [
            {"openTime": "2026-01-01T00:02:00Z", "open": 3, "high": 4, "low": 2, "close": 3, "isOpen": False},
            {"openTime": "2026-01-01T00:03:00Z", "open": 3, "high": 4, "low": 2, "close": 3, "isOpen": True},
            {"openTime": "2026-01-01T00:01:00Z", "open": 3, "high": 4, "low": 2, "close": 3, "isOpen": False},
        ]}
        bars = parse_bars(payload)
        self.assertEqual(len(bars), 2)
        self.assertEqual([bar.time for bar in bars], ["2026-01-01T00:01:00Z", "2026-01-01T00:02:00Z"])


if __name__ == "__main__":
    unittest.main()
