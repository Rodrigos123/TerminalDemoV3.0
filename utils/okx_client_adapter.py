# utils/okx_client_adapter.py
# -*- coding: utf-8 -*-
"""
OkxClientAdapter
- Adapta tu OKXClient (demo/real) a una interfaz estable:
    get_candles(inst_id, bar, limit) -> List[Row]
    get_ticker(inst_id) -> dict normalizado (intenta exponer 'last')
- Prueba múltiples firmas y normaliza respuestas (dict con 'data', listas, etc.).
- Respeta flag 'verbose' para logs.
"""

from __future__ import annotations
from typing import Any, Callable, Optional, List, Dict, Tuple


class OkxClientAdapter:
    def __init__(self, client: Any, verbose: bool = False) -> None:
        self._c = client
        self._verbose = bool(verbose)
        self._candles_fn = self._resolve([
            "get_candles", "market_candles", "fetch_candles",
            "getKlines", "klines", "candles", "kline"
        ])
        self._ticker_fn = self._resolve([
            "get_ticker", "market_ticker", "fetch_ticker",
            "ticker", "getTicker"
        ])
        if self._candles_fn is None:
            raise AttributeError("El cliente OKX no tiene método de velas compatible.")
        if self._ticker_fn is None:
            raise AttributeError("El cliente OKX no tiene método de ticker compatible.")
        self._logged_candles_once = False
        self._logged_ticker_once = False

    def _resolve(self, names: List[str]) -> Optional[Callable]:
        for n in names:
            fn = getattr(self._c, n, None)
            if callable(fn):
                return fn
        return None

    @staticmethod
    def _normalize_response_rows(resp: Any) -> List[Any]:
        if resp is None:
            return []
        if isinstance(resp, (list, tuple)):
            return list(resp)
        if isinstance(resp, dict):
            data = resp.get("data")
            if isinstance(data, (list, tuple)):
                return list(data)
            for key in ("result", "items", "rows"):
                val = resp.get(key)
                if isinstance(val, (list, tuple)):
                    return list(val)
            return []
        data = getattr(resp, "data", None)
        if isinstance(data, (list, tuple)):
            return list(data)
        return []

    @staticmethod
    def _normalize_ticker(resp: Any) -> Dict[str, Any]:
        if resp is None:
            return {}
        if isinstance(resp, dict):
            return resp
        if isinstance(resp, (list, tuple)):
            if not resp:
                return {}
            first = resp[0]
            if isinstance(first, dict):
                return first
            try:
                return {"last": float(first)}
            except Exception:
                return {}
        d: Dict[str, Any] = {}
        for k in ("last", "lastPx", "lastPrice", "close", "px", "c", "bestAsk", "bestBid", "askPx", "bidPx"):
            v = getattr(resp, k, None)
            if v is not None:
                d[k] = v
        return d

    def get_candles(self, inst_id: str, bar: str, limit: int) -> List[Any]:
        fn = self._candles_fn
        tries: List[Tuple[tuple, dict]] = [
            ((inst_id, bar, limit), {}),
            ((), {"inst_id": inst_id, "bar": bar, "limit": limit}),
            ((), {"instId": inst_id, "bar": bar, "limit": limit}),
            ((), {"instrument": inst_id, "bar": bar, "limit": limit}),
            ((), {"symbol": inst_id, "timeframe": bar, "limit": limit}),
            ((), {"instId": inst_id, "granularity": self._bar_to_granularity(bar), "limit": limit}),
            ((inst_id,), {"bar": bar, "limit": limit}),
            ((inst_id,), {"timeframe": bar, "limit": limit}),
        ]
        last_err: Optional[Exception] = None
        for args, kwargs in tries:
            try:
                resp = fn(*args, **kwargs)
                rows = self._normalize_response_rows(resp)
                if self._verbose and not self._logged_candles_once:
                    print(f"[ADAPTER][CANDLES] call={fn.__name__}{args or ''}{kwargs or ''} -> type={type(resp).__name__} rows={len(rows)}", flush=True)
                    self._logged_candles_once = True
                return rows
            except Exception as e:
                last_err = e
                continue
        raise last_err if last_err else RuntimeError("No se pudo invocar get_candles en el cliente OKX.")

    def get_ticker(self, inst_id: str) -> Dict[str, Any]:
        fn = self._ticker_fn
        tries: List[Tuple[tuple, dict]] = [
            ((inst_id,), {}), ((), {"inst_id": inst_id}), ((), {"instId": inst_id}),
            ((), {"instrument": inst_id}), ((), {"symbol": inst_id}),
        ]
        last_err: Optional[Exception] = None
        for args, kwargs in tries:
            try:
                resp = fn(*args, **kwargs)
                tick = self._normalize_ticker(resp)
                if not tick and isinstance(resp, dict) and isinstance(resp.get("data"), (list, tuple)) and resp["data"]:
                    first = resp["data"][0]
                    if isinstance(first, dict):
                        tick = first
                if self._verbose and not self._logged_ticker_once:
                    print(f"[ADAPTER][TICKER] call={fn.__name__}{args or ''}{kwargs or ''} -> type={type(resp).__name__} keys={list(tick.keys()) if isinstance(tick, dict) else 'n/a'}", flush=True)
                    self._logged_ticker_once = True
                return tick
            except Exception as e:
                last_err = e
                continue
        raise last_err if last_err else RuntimeError("No se pudo invocar get_ticker en el cliente OKX.")

    @staticmethod
    def _bar_to_granularity(bar: str) -> int:
        mapping = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1D": 86400, "1d": 86400, "1day": 86400}
        return mapping.get(bar, 60)

    @property
    def base_url(self):
        return getattr(self._c, "base_url", None)

    @property
    def simulated(self):
        return getattr(self._c, "simulated", None)

    def is_authenticated(self) -> bool:
        # En demo, puedes devolver True si quieres
        return True
