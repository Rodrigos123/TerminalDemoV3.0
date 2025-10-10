# utils/okx_data_cache.py — cache de mercado y escritura CSV con ts ISO Z
from __future__ import annotations
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from utils.common import parse_ohlcv_row, format_ohlcv_csv_row, get_ohlcv_header

TF_TO_BAR = {
    "1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m", "30m": "30m",
    "1H": "1H", "4H": "4H", "6H": "6H", "12H": "12H",
    "1D": "1D", "1W": "1W", "1M": "1M",
    # acepta minúsculas comunes también
    "1h": "1H", "4h": "4H", "6h": "6H", "12h": "12H", "1d": "1D", "1w": "1W", "1mth": "1M",
}

class MarketDataCache:
    def __init__(self, client, data_dir: Path, persist: bool = True, max_keep: int = 5000) -> None:
        self.client = client
        self.data_dir = Path(data_dir)
        self.persist = bool(persist)
        self.max_keep = int(max_keep)
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def _file_path(self, symbol: str, timeframe: str) -> Path:
        sym = symbol.replace("/", "-")
        tf = timeframe
        return self.data_dir / f"{sym}_{tf}.csv"

    def _fetch(self, symbol: str, timeframe: str, limit: int = 300) -> List[Dict[str, Any]]:
        bar = TF_TO_BAR.get(timeframe, timeframe)
        res = self.client._request("GET", "/api/v5/market/candles", params={"instId": symbol, "bar": bar, "limit": limit}, signed=False)
        rows = []
        # OKX devuelve data ordenada desc (más reciente primero). Normalizamos a ascendente.
        for raw in reversed(res.data):
            rows.append(parse_ohlcv_row(raw))
        return rows

    def update_and_get_ohlcv(self, symbol: str, timeframe: str, limit: int = 300) -> List[Dict[str, Any]]:
        rows = self._fetch(symbol, timeframe, limit=limit)
        if self.persist:
            fp = self._file_path(symbol, timeframe)
            header = get_ohlcv_header()
            # Escribimos siempre ISO sin milisegundos, p.ej. 2025-10-04T13:46:54Z
            if not fp.exists():
                fp.write_text(header + "\n", encoding="utf-8")
            # Fusionar por ts_ms único
            existing = {}
            if fp.exists():
                lines = fp.read_text(encoding="utf-8").splitlines()
                for line in lines[1:]:
                    if not line.strip():
                        continue
                    parts = line.split(",")
                    if len(parts) < 7:  # ts_iso,ts_ms,open,high,low,close,volume
                        continue
                    try:
                        ts_ms = int(parts[1])
                        existing[ts_ms] = line
                    except Exception:
                        continue
            for parsed in rows:
                ts_ms = parsed.get("ts_ms")
                if ts_ms is None:
                    continue
                line = format_ohlcv_csv_row(parsed)  # -> ISO Z sin milisegundos
                existing[ts_ms] = line
            # limitar tamaño
            items = sorted(existing.items(), key=lambda kv: kv[0])[-self.max_keep:]
            with fp.open("w", encoding="utf-8") as f:
                f.write(header + "\n")
                for _, line in items:
                    f.write(line + "\n")
        return rows

    def last_close(self, symbol: str, timeframe: str) -> Optional[float]:
        rows = self._fetch(symbol, timeframe, limit=1)
        return rows[-1]["close"] if rows else None
