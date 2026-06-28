# utils/data_ingestor.py
# -*- coding: utf-8 -*-
"""
DataIngestor: alimenta el caché de mercado usando TU OKXClient (auth/simulado).
- Usa OkxClientAdapter (robusto en firmas y shapes).
- Intervalo de sondeo por defecto: 60s (ajustado).
"""

from __future__ import annotations

import threading
import time
from typing import Iterable, List, Optional, Any, Union, Dict

from utils.okx_client_adapter import OkxClientAdapter
from utils.okx_data_cache import get_shared_cache, Candle

OKX_BAR_MAP = {
    "1m": "1m",
    "5m": "5m",
    "15m": "15m",
    "1h": "1H",
    "4h": "4H",
    # Importante (OKX): por defecto 1D abre en UTC+8 (HK). Para alinear con
    # StrategyQuant/MT4 en UTC, usamos el K-line UTC provisto por OKX.
    "1d": "1Dutc",
}

Number = Union[int, float]

def _to_float(x: Any, default: float = 0.0) -> float:
    try: return float(x)
    except Exception: return default

def _to_int(x: Any) -> Optional[int]:
    try: return int(x)
    except Exception: return None

def _parse_row_flex(row: Any) -> Optional[Candle]:
    if isinstance(row, dict):
        ts = row.get("ts") or row.get("t") or row.get("time") or row.get("timestamp")
        o = row.get("o") or row.get("open")
        h = row.get("h") or row.get("high")
        l = row.get("l") or row.get("low")
        c = row.get("c") or row.get("close") or row.get("last")
        v = row.get("vol") or row.get("volume") or row.get("v") or 0
        tsi = _to_int(ts)
        if tsi is None:
            tsecs = _to_int(row.get("t") or row.get("time"))
            if tsecs is not None and tsecs < 10_000_000_000:
                tsi = tsecs * 1000
        if tsi is None: return None
        return (int(tsi), _to_float(o), _to_float(h), _to_float(l), _to_float(c), _to_float(v))
    if isinstance(row, (list, tuple)) and len(row) >= 5:
        tsi = _to_int(row[0])
        if tsi is not None:
            o = _to_float(row[1]); h = _to_float(row[2]); l = _to_float(row[3]); c = _to_float(row[4])
            v = _to_float(row[5]) if len(row) > 5 else 0.0
            if tsi < 10_000_000_000: tsi *= 1000
            return (tsi, o, h, l, c, v)
        idx_ts = None
        for i, val in enumerate(row):
            vi = _to_int(val)
            if vi is None: continue
            if vi >= 10**11 or (1_000_000_000 <= vi <= 2_000_000_000):
                idx_ts = i; tsi = vi; break
        if idx_ts is not None:
            nums: List[float] = []
            for j, val in enumerate(row):
                if j == idx_ts: continue
                try: nums.append(float(val))
                except Exception: pass
            if len(nums) >= 4:
                o, h, l, c = nums[0], nums[1], nums[2], nums[3]
                v = nums[4] if len(nums) > 4 else 0.0
                if tsi < 10_000_000_000: tsi *= 1000
                return (tsi, o, h, l, c, v)
    return None

class DataIngestor(threading.Thread):
    def __init__(
        self,
        client,
        symbols: Iterable[str],
        tfs: Iterable[str],
        limit: int = 200,
        interval_sec: int = 60,   # <- 60s por defecto
        stagger_ms: int = 120,
        name: str = "DataIngestor",
        daemon: bool = True,
        verbose: bool = False,
    ) -> None:
        super().__init__(name=name, daemon=daemon)
        self._verbose = bool(verbose)
        self._client = OkxClientAdapter(client, verbose=self._verbose)
        self._symbols: List[str] = sorted({s for s in symbols if s})
        self._tfs: List[str] = sorted({tf for tf in tfs if tf in OKX_BAR_MAP})
        self._limit = max(1, min(int(limit), 300))
        self._interval = max(1, int(interval_sec))
        self._stagger = max(0, int(stagger_ms))
        self._stop = threading.Event()
        self._cache = get_shared_cache()
        self._logged_row_once = False

    def stop(self):
        self._stop.set()

    def _fetch_and_store_candles(self, symbol: str, tf: str) -> None:
        bar = OKX_BAR_MAP[tf]
        rows = self._client.get_candles(symbol, bar, self._limit)
        if isinstance(rows, dict) and "data" in rows:
            rows = rows["data"]
        if not isinstance(rows, (list, tuple)):
            if self._verbose:
                print(f"[INGESTOR][WARN] filas no lista: type={type(rows).__name__} value={rows}", flush=True)
            return
        if rows and not self._logged_row_once and self._verbose:
            print(f"[INGESTOR][INFO] primera fila ejemplo={rows[0]}", flush=True)
            self._logged_row_once = True

        candles: List[Candle] = []
        for r in rows:
            c = _parse_row_flex(r)
            if c: candles.append(c)
        if not candles: return
        candles.sort(key=lambda x: x[0])
        for c in candles:
            self._cache.put_candle(symbol, tf, c)

    def _fetch_and_store_last(self, symbol: str, tf_for_fallback: Optional[str] = None) -> None:
        tick = self._client.get_ticker(symbol)
        last = None
        if isinstance(tick, dict):
            for k in ("last", "lastPx", "lastPrice", "close", "c", "px", "bestAsk", "bestBid", "askPx", "bidPx"):
                if tick.get(k) is not None:
                    last = tick[k]; break
        else:
            for k in ("last", "lastPx", "lastPrice", "close", "c", "px", "bestAsk", "bestBid", "askPx", "bidPx"):
                val = getattr(tick, k, None)
                if val is not None:
                    last = val; break
        if last is None and tf_for_fallback:
            candles = self._cache.get_candles(symbol, tf_for_fallback, 1)
            if candles:
                last = candles[-1][4]
        if last is not None:
            self._cache.put_last_price(symbol, _to_float(last))

    def run(self) -> None:
        # Warmup
        try:
            for sym in self._symbols:
                for tf in self._tfs:
                    self._fetch_and_store_candles(sym, tf)
                self._fetch_and_store_last(sym, tf_for_fallback=(self._tfs[0] if self._tfs else None))
                if self._stagger:
                    time.sleep(self._stagger / 1000.0)
        except Exception as e:
            if self._verbose:
                print(f"[INGESTOR][WARMUP][ERROR] {e}", flush=True)

        while not self._stop.is_set():
            t0 = time.time()
            try:
                for sym in self._symbols:
                    for tf in self._tfs:
                        self._fetch_and_store_candles(sym, tf)
                    self._fetch_and_store_last(sym, tf_for_fallback=(self._tfs[0] if self._tfs else None))
                    if self._stagger:
                        time.sleep(self._stagger / 1000.0)
                if self._symbols and self._tfs and self._verbose:
                    sz = self._cache.series_size(self._symbols[0], self._tfs[0])
                    lp = self._cache.get_last_price(self._symbols[0])
                    print(f"[INGESTOR] symbols={len(self._symbols)} tfs={self._tfs} | series({self._symbols[0]},{self._tfs[0]})={sz} | last={lp}", flush=True)
            except Exception as e:
                if self._verbose:
                    print(f"[INGESTOR][ERROR] {e}", flush=True)

            elapsed = time.time() - t0
            wait = max(0.0, self._interval - elapsed)
            end = time.time() + wait
            while time.time() < end and not self._stop.is_set():
                time.sleep(0.1)
