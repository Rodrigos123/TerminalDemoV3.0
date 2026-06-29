# utils/data_feed.py
# -*- coding: utf-8 -*-
"""
DataFeed único para estrategias (ubicado en /utils).
- No hace llamadas de red.
- Solo lee del caché compartido (utils.okx_data_cache).
- Mantiene API mínima y clara para consultar último precio y velas.

Uso en estrategias o en el terminal:
    from utils.data_feed import DataFeed
    df = DataFeed()
    px = df.get_last_price("BTC-USDT")
    candles = df.get_candles("BTC-USDT", "1m", 500)
"""

from __future__ import annotations

from typing import List, Optional, Tuple
import re
from utils.okx_data_cache import get_shared_cache

Candle = Tuple[int, float, float, float, float, float]  # ts_open, o, h, l, c, v

def _normalize_tf(tf: str) -> str:
    """Normaliza TF a formato del caché (ej: 'D1'->'1d', 'H4'->'4h')."""
    if tf is None:
        return ""
    t = str(tf).strip()
    if not t:
        return ""
    t = t.lower()

    aliases = {
        "d1": "1d",
        "1d": "1d",
        "day": "1d",
        "daily": "1d",
        "h1": "1h",
        "1h": "1h",
        "hour": "1h",
        "hourly": "1h",
        "h4": "4h",
        "4h": "4h",
        "m1": "1m",
        "1m": "1m",
        "min1": "1m",
        "minute": "1m",
        "15m": "15m",
        "m15": "15m",
        "5m": "5m",
        "m5": "5m",
        "30m": "30m",
        "m30": "30m",
    }
    if t in aliases:
        return aliases[t]

    m = re.match(r"^(\d+)m$", t)
    if m:
        n = int(m.group(1))
        if n == 60:
            return "1h"
        if n == 240:
            return "4h"
        return f"{n}m"

    return t


def _normalize_symbol(symbol: str) -> str:
    """Normaliza símbolo a formato del caché (ej: 'BTCUSDT'->'BTC-USDT')."""
    if symbol is None:
        return ""
    s = str(symbol).strip()
    if not s:
        return ""
    s = s.upper()

    if "-" in s:
        return s

    if s.endswith("USDT") and len(s) > 4:
        base = s[:-4]
        return f"{base}-USDT"

    return s


class DataFeed:
    """
    Único punto de lectura de datos de mercado para las estrategias.
    """

    def __init__(self) -> None:
        self._cache = get_shared_cache()

    def get_last_price(self, symbol: str) -> Optional[float]:
        """
        Último precio conocido para el símbolo (o None).
        """
        symbol_n = _normalize_symbol(symbol)
        return self._cache.get_last_price(symbol_n)

    def get_last_price_ts(self, symbol: str) -> Optional[int]:
        """
        Timestamp (ms) de la última actualización de precio (o None).
        """
        symbol_n = _normalize_symbol(symbol)
        return self._cache.get_last_price_ts(symbol_n)

    def get_candles(self, symbol: str, tf: str, limit: int) -> List[Candle]:
        """
        Devuelve hasta 'limit' velas más recientes como lista (ts, o, h, l, c, v).
        """
        symbol_n = _normalize_symbol(symbol)
        tf_n = _normalize_tf(tf)
        return self._cache.get_candles(symbol_n, tf_n, limit)