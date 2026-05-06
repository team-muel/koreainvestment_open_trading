import os
import sys
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(root, "strategy_builder"))
sys.path.insert(0, root)

from strategy_builder.core.symbol_master import SymbolInfo, SymbolMaster, parse_kospi_kosdaq_mst
from strategy_builder.core.universe_scanner import KRXUniverseScanner, UniverseFilterConfig


class UniverseScannerTests(unittest.TestCase):
    def test_parse_kospi_kosdaq_master_extracts_code_and_name(self):
        line = b"005930   " + b"KR7005930003" + "삼성전자".encode("euc-kr").ljust(40) + b" " * 20

        symbols = parse_kospi_kosdaq_mst(line, "kospi")

        self.assertEqual(len(symbols), 1)
        self.assertEqual(symbols[0].code, "005930")
        self.assertEqual(symbols[0].name, "삼성전자")
        self.assertEqual(symbols[0].exchange, "kospi")

    def test_parse_kospi_kosdaq_master_skips_non_six_digit_issue_codes(self):
        line = b"F70100026" + b"KR5701000261" + "한투글로벌넥스트웨이브1(A)".encode("euc-kr").ljust(40) + b" " * 20

        self.assertEqual(parse_kospi_kosdaq_mst(line, "kospi"), [])

    def test_scan_filters_names_and_ranks_by_trading_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            master = SymbolMaster(tmp)
            master.save("kospi", [
                SymbolInfo("005930", "삼성전자", "kospi", "KOSPI"),
                SymbolInfo("069500", "KODEX 200 ETF", "kospi", "KOSPI"),
                SymbolInfo("000660", "SK하이닉스", "kospi", "KOSPI"),
            ])
            master.save("kosdaq", [])
            scanner = KRXUniverseScanner(master=master)

            def daily_prices(symbol, days, env_dv):
                if symbol == "005930":
                    close, volume = 80000, 100000
                elif symbol == "000660":
                    close, volume = 170000, 200000
                else:
                    close, volume = 10000, 1000
                return pd.DataFrame({
                    "date": [f"202601{i:02d}" for i in range(1, 22)],
                    "open": [close] * 21,
                    "high": [close] * 21,
                    "low": [close] * 21,
                    "close": [close] * 21,
                    "volume": [volume] * 21,
                })

            with patch("strategy_builder.core.universe_scanner.data_fetcher.get_daily_prices", side_effect=daily_prices):
                result = scanner.scan(UniverseFilterConfig(
                    min_price=2000,
                    min_avg_trading_value=3_000_000_000,
                    min_last_trading_value=5_000_000_000,
                    min_prev_change_pct=0,
                    min_relative_volume=0,
                    max_scan_symbols=10,
                    watchlist_limit=10,
                    request_delay=0,
                    use_volume_rank=False,
                ))

        self.assertEqual(result["eligible_name_count"], 2)
        self.assertEqual(result["candidate_count"], 2)
        self.assertEqual(result["symbols_for_ict_start"], ["000660", "005930"])

    def test_scan_uses_volume_rank_order_before_daily_checks(self):
        with tempfile.TemporaryDirectory() as tmp:
            master = SymbolMaster(tmp)
            master.save("kospi", [
                SymbolInfo("005930", "삼성전자", "kospi", "KOSPI"),
                SymbolInfo("000660", "SK하이닉스", "kospi", "KOSPI"),
            ])
            master.save("kosdaq", [])
            scanner = KRXUniverseScanner(master=master)
            called: list[str] = []

            def daily_prices(symbol, days, env_dv):
                called.append(symbol)
                return pd.DataFrame({
                    "date": ["20260101"] * 21,
                    "open": [100000] * 21,
                    "high": [100000] * 21,
                    "low": [100000] * 21,
                    "close": [100000] * 21,
                    "volume": [100000] * 21,
                })

            rank = pd.DataFrame({"code": ["000660", "005930"]})
            with (
                patch("strategy_builder.core.universe_scanner.data_fetcher.get_daily_prices", side_effect=daily_prices),
                patch("strategy_builder.core.universe_scanner.data_fetcher.get_volume_rank", return_value=rank),
            ):
                scanner.scan(UniverseFilterConfig(
                    max_scan_symbols=2,
                    watchlist_limit=2,
                    request_delay=0,
                    min_prev_change_pct=0,
                    min_relative_volume=0,
                ))

        self.assertEqual(called, ["000660", "005930"])


if __name__ == "__main__":
    unittest.main()
