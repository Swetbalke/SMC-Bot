import unittest

import pandas as pd

from backtest import prepare_historical_candles, run_walk_forward_backtest
from config_manager import DEFAULTS


def candles(rows=100):
    close = [100.0 + (index % 7) for index in range(rows)]
    return pd.DataFrame({
        "time": pd.date_range("2026-01-01", periods=rows, freq="1min", tz="UTC"),
        "open": close,
        "high": [value + 1 for value in close],
        "low": [value - 1 for value in close],
        "close": close,
    })


class BacktestValidationTests(unittest.TestCase):
    def test_rejects_timeframe_gaps(self):
        data = candles(30).drop(index=10).reset_index(drop=True)
        with self.assertRaisesRegex(ValueError, "gaps"):
            prepare_historical_candles(data, "1m")

    def test_rejects_duplicate_timestamps(self):
        data = candles(30)
        data.loc[10, "time"] = data.loc[9, "time"]
        with self.assertRaisesRegex(ValueError, "duplicate"):
            prepare_historical_candles(data, "1m")

    def test_walk_forward_reports_both_segments(self):
        result = run_walk_forward_backtest(candles(100), DEFAULTS)
        self.assertIn("in_sample", result)
        self.assertIn("out_of_sample", result)
        self.assertEqual(result["split"]["in_sample_rows"], 70)


if __name__ == "__main__":
    unittest.main()
