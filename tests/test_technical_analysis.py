import unittest

from bot import Bar
from technical_analysis import analyze_timeframe, candlestick_patterns, find_swings, format_analysis


def make_bars(values):
    bars = []
    for index, value in enumerate(values):
        bars.append(Bar(
            time=f"2026-01-01T00:{index:02d}:00Z",
            open=float(value) - 0.05,
            high=float(value) + 0.20,
            low=float(value) - 0.20,
            close=float(value),
        ))
    return bars


class MarketStructureTests(unittest.TestCase):
    def test_confirmed_swing_highs_and_lows(self):
        bars = make_bars([10, 14, 11, 13, 9, 12, 8])
        pivots = find_swings(bars, left=1, right=1)
        highs = [pivot for pivot in pivots if pivot.kind == "high"]
        lows = [pivot for pivot in pivots if pivot.kind == "low"]
        self.assertEqual([pivot.index for pivot in highs], [1, 3, 5])
        self.assertEqual([pivot.index for pivot in lows], [2, 4])

    def test_bullish_engulfing_is_detected(self):
        bars = [
            Bar("1", 10.0, 10.2, 9.4, 9.5),
            Bar("2", 9.4, 10.4, 9.3, 10.3),
        ]
        names = [pattern.name for pattern in candlestick_patterns(bars)]
        self.assertIn("ابتلاع شرائي", names)

    def test_inside_bar_is_detected(self):
        bars = [
            Bar("1", 10.0, 11.0, 9.0, 10.5),
            Bar("2", 10.4, 10.8, 9.5, 10.1),
        ]
        names = [pattern.name for pattern in candlestick_patterns(bars)]
        self.assertIn("شمعة داخلية", names)

    def test_breakout_compares_close_to_last_confirmed_swing(self):
        bars = make_bars([100, 101, 102, 103, 102, 101, 102, 103, 104, 103,
                          102, 103, 104, 105, 104, 103, 104, 105, 106, 105,
                          104, 105, 106, 107, 106, 105, 106, 107, 108, 109])
        view = analyze_timeframe(bars)
        self.assertEqual(view["breakout_direction"], "bullish")
        self.assertIn("قمة مؤكدة", view["breakout"])

    def test_analysis_report_contains_multitimeframe_suggestion(self):
        bars = make_bars([100 + index * 0.2 for index in range(40)])
        quote = {"bid": 108.0, "ask": 108.1, "spread": 0.1,
                 "marketState": "open", "stale": False, "quoteAgeSeconds": 1}
        report = format_analysis("XAUUSD", quote, bars, bars, bars, None,
                                  0.5, 60, lambda value: f"{value:.2f}")
        self.assertIn("15m", report)
        self.assertIn("الاقتراح", report)
        self.assertIn("لا يوجد محفز دخول", report)


if __name__ == "__main__":
    unittest.main()
