# utils/okx_data_cache.py
# -*- coding: utf-8 -*-
"""
Caché único y compartido de mercado para todo el sistema.
- Thread-safe.
- Una sola fuente de verdad para velas y último precio por símbolo.
- Sin funciones duplicadas ni modos alternos: esta es la API oficial.

Estructuras:
- Velas por clave (symbol, tf): deque de tuplas (ts_open_ms, open, high, low, close, volume)
- Último precio por símbolo: float + ts_ms de actualización

Uso:
    from utils.okx_data_cache import get_shared_cache

    cache = get_shared_cache()
    cache.put_last_price("BTC-USDT", 64321.5)
    cache.put_candle("BTC-USDT", "1m", (ts, o, h, l, c, v))
    candles = cache.get_candles("BTC-USDT", "1m", 500)
    px = cache.get_last_price("BTC-USDT")
"""

from __future__ import annotations

import threading
from collections import defaultdict, deque
from typing import Deque, Dict, List, Optional, Tuple

# Tipos y validaciones básicas
Candle = Tuple[int, float, float, float, float, float]  # ts_open_ms, o, h, l, c, v
_VALID_TFS = {"1m", "5m", "15m", "1h", "4h", "1d"}


class MarketDataCache:
    """
    Caché compartido por símbolo y TF. Thread-safe.
    Reglas:
      - put_candle reemplaza la vela si llega con el mismo ts que la última.
      - Ignora velas antiguas (ts < última).
      - get_candles devuelve lista (copia) para no exponer estructuras internas.
    """

    def __init__(self, maxlen_per_series: int = 5000) -> None:
        self._lock = threading.RLock()
        self._candles: Dict[Tuple[str, str], Deque[Candle]] = defaultdict(
            lambda: deque(maxlen=maxlen_per_series)
        )
        self._last_price: Dict[str, float] = {}
        self._last_ts: Dict[str, int] = {}

    # -------------------- Writers --------------------

    def put_candle(self, symbol: str, tf: str, candle: Candle) -> None:
        """
        Inserta o actualiza una vela (ts, o, h, l, c, v).
        Reemplaza si el ts coincide con la última; si es más nuevo, hace append; si es más viejo, lo ignora.
        """
        if tf not in _VALID_TFS:
            return
        key = (symbol, tf)
        with self._lock:
            buf = self._candles[key]
            if buf and candle[0] == buf[-1][0]:
                buf[-1] = candle
            elif not buf or candle[0] > buf[-1][0]:
                buf.append(candle)
            # si llega más viejo, se ignora

    def put_last_price(self, symbol: str, px: float, ts_ms: Optional[int] = None) -> None:
        """
        Actualiza el último precio de un símbolo y su timestamp (ms).

        Importante: el timestamp debe venir del broker/datos. Si ts_ms es None,
        NO se debe inventar con reloj del VPS, para no contaminar monitor/logs.
        """
        with self._lock:
            self._last_price[symbol] = float(px)
            if ts_ms is not None:
                self._last_ts[symbol] = int(ts_ms)

    # -------------------- Readers --------------------

    def get_last_price(self, symbol: str) -> Optional[float]:
        """
        Devuelve el último precio conocido (o None si no existe).
        """
        with self._lock:
            return self._last_price.get(symbol)

    def get_last_price_ts(self, symbol: str) -> Optional[int]:
        """
        Devuelve el ts_ms de la última actualización de precio (o None).
        """
        with self._lock:
            return self._last_ts.get(symbol)

    def get_candles(self, symbol: str, tf: str, limit: int) -> List[Candle]:
        """
        Devuelve hasta 'limit' velas más recientes como lista (copia).
        Si no hay velas o limit <= 0, devuelve [].
        """
        if limit <= 0:
            return []
        key = (symbol, tf)
        with self._lock:
            buf = self._candles.get(key)
            if not buf:
                return []
            if limit >= len(buf):
                return list(buf)
            # copy slice de las últimas 'limit'
            return list(buf)[-limit:]

    # -------------------- Maintenance --------------------

    def clear_symbol(self, symbol: str) -> None:
        """
        Borra todas las series (todas las TF) y último precio asociadas a un símbolo.
        """
        with self._lock:
            # eliminar series
            keys = [k for k in self._candles.keys() if k[0] == symbol]
            for k in keys:
                self._candles.pop(k, None)
            # eliminar último precio
            self._last_price.pop(symbol, None)
            self._last_ts.pop(symbol, None)

    def series_size(self, symbol: str, tf: str) -> int:
        """
        Cantidad de velas almacenadas para (symbol, tf).
        """
        key = (symbol, tf)
        with self._lock:
            buf = self._candles.get(key)
            return len(buf) if buf else 0


# -------------------- Singleton compartido --------------------

_shared_cache: Optional[MarketDataCache] = None
_shared_cache_lock = threading.Lock()


def get_shared_cache(maxlen_per_series: int = 5000) -> MarketDataCache:
    """
    Devuelve el caché compartido (singleton).
    """
    global _shared_cache
    if _shared_cache is None:
        with _shared_cache_lock:
            if _shared_cache is None:
                _shared_cache = MarketDataCache(maxlen_per_series=maxlen_per_series)
    return _shared_cache
