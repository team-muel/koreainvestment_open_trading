"""KOSPI/KOSDAQ symbol master utilities."""

from __future__ import annotations

import csv
import zipfile
from dataclasses import dataclass
from datetime import date, datetime
from io import BytesIO
from pathlib import Path

import requests


MASTER_URLS = {
    "kospi": "https://new.real.download.dws.co.kr/common/master/kospi_code.mst.zip",
    "kosdaq": "https://new.real.download.dws.co.kr/common/master/kosdaq_code.mst.zip",
}


@dataclass(frozen=True)
class SymbolInfo:
    code: str
    name: str
    exchange: str
    exchange_name: str

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "name": self.name,
            "exchange": self.exchange,
            "exchange_name": self.exchange_name,
        }


class SymbolMaster:
    def __init__(self, master_dir: str | Path | None = None):
        root = Path(__file__).resolve().parents[1]
        self.master_dir = Path(master_dir) if master_dir else root / "data" / "symbol_master"
        self.master_dir.mkdir(parents=True, exist_ok=True)

    def csv_path(self, exchange: str) -> Path:
        return self.master_dir / f"{exchange}.csv"

    def needs_update(self) -> bool:
        for exchange in MASTER_URLS:
            path = self.csv_path(exchange)
            if not path.exists():
                return True
            if datetime.fromtimestamp(path.stat().st_mtime).date() < date.today():
                return True
        return False

    def status(self) -> dict:
        kospi = self.load("kospi")
        kosdaq = self.load("kosdaq")
        return {
            "kospi_count": len(kospi),
            "kosdaq_count": len(kosdaq),
            "total_count": len(kospi) + len(kosdaq),
            "needs_update": self.needs_update(),
            "master_dir": str(self.master_dir),
        }

    def load_all(self) -> list[SymbolInfo]:
        result: list[SymbolInfo] = []
        for exchange in MASTER_URLS:
            result.extend(self.load(exchange))
        return result

    def load(self, exchange: str) -> list[SymbolInfo]:
        path = self.csv_path(exchange)
        if not path.exists():
            return []
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            return [
                SymbolInfo(
                    code=row["code"],
                    name=row["name"],
                    exchange=row["exchange"],
                    exchange_name=row["exchange_name"],
                )
                for row in csv.DictReader(f)
                if row.get("code") and row.get("name")
            ]

    def collect(self, exchanges: list[str] | None = None) -> dict:
        exchanges = exchanges or list(MASTER_URLS)
        errors: list[str] = []
        counts: dict[str, int] = {}
        for exchange in exchanges:
            try:
                symbols = self.download(exchange)
                self.save(exchange, symbols)
                counts[exchange] = len(symbols)
            except Exception as exc:
                errors.append(f"{exchange}: {exc}")
                counts[exchange] = 0
        return {
            "success": not errors,
            "counts": counts,
            "total_count": sum(counts.values()),
            "errors": errors,
        }

    def download(self, exchange: str) -> list[SymbolInfo]:
        url = MASTER_URLS.get(exchange)
        if not url:
            raise ValueError(f"unsupported exchange: {exchange}")
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        content = response.content
        try:
            with zipfile.ZipFile(BytesIO(content)) as zf:
                first = zf.namelist()[0]
                content = zf.read(first)
        except zipfile.BadZipFile:
            pass
        return parse_kospi_kosdaq_mst(content, exchange)

    def save(self, exchange: str, symbols: list[SymbolInfo]) -> None:
        path = self.csv_path(exchange)
        with path.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["code", "name", "exchange", "exchange_name"])
            writer.writeheader()
            for symbol in symbols:
                writer.writerow(symbol.to_dict())


def parse_kospi_kosdaq_mst(content: bytes, exchange: str) -> list[SymbolInfo]:
    exchange_name = "KOSPI" if exchange == "kospi" else "KOSDAQ"
    symbols: list[SymbolInfo] = []
    for line in content.splitlines():
        if len(line) < 61:
            continue
        code = line[0:9].decode("euc-kr", errors="ignore").strip()
        name = line[21:61].decode("euc-kr", errors="ignore").strip()
        if len(code) == 6 and code.isdigit() and name:
            symbols.append(SymbolInfo(code=code, name=name, exchange=exchange, exchange_name=exchange_name))
    return symbols
