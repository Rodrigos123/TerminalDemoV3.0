from __future__ import annotations
import time, json
from pathlib import Path
from typing import Any, Dict, List, Optional

from utils.data_feed import DataFeed
from utils.engine_execution import get_shared_engine

# ─────────────────────────────────────────────
# CONFIG GENERAL DE LA ESTRATEGIA (EDITABLE)
# ─────────────────────────────────────────────
SYMBOL = "BTC-USDT"        # ← Cambiar por el símbolo de la estrategia
TIMEFRAME = "15m"           # ← Cambiar por el TF lógico principal ("1m","5m","1h",etc.)
MAGIC_NUMBER = 25100001    # ← Cambiar por el MAGIC de la estrategia

# Porcentaje de balance a arriesgar por operación (ejecución real)
RISK_PCT_BALANCE = 0.01

# Riesgo fijo en backtest o si no se puede leer equity
FIXED_RISK_USDT = 100.0

BASE_DIR = Path(__file__).resolve().parent.parent
MONITOR_DIR = BASE_DIR / "monitor"


class StrategyClass:
    """
    PLANTILLA BASE DE ESTRATEGIA PARA EL TERMINAL / BACKTESTER

    Estructura general:
    - __init__: parámetros, wiring con DataFeed y ExecutionEngine, rehidratación de estado.
    - run / _loop_once: loop principal por vela nueva cerrada.
    - _normalize_candles: normaliza el formato de velas desde DataFeed.
    - _on_bar_close: ORDEN LÓGICO PRINCIPAL, separado en:
        1) Cálculo de indicadores
        2) Lógica de entrada
        3) Lógica de salida
        4) Cálculo de SL / TP
        5) Ejecución de órdenes

    Cambios v2.0.1:
    - On Bar Open: la orden se ejecuta al OPEN de la vela siguiente (no al close).
    - TP opcional: si no existe, usar 0.0 (NUNCA None).
    - SL obligatorio: si SL inválido -> no abre.
    """

    def __init__(self, symbol: str, timeframe: str, magic: int, data_feed: DataFeed):
        # Comprobaciones de consistencia con el terminal
        if symbol != SYMBOL:
            print(f"[STRAT][WARN] SYMBOL mismatch: terminal={symbol} file={SYMBOL}", flush=True)
        if timeframe != TIMEFRAME:
            print(f"[STRAT][WARN] TF mismatch: terminal={timeframe} file={TIMEFRAME}", flush=True)
        if int(magic) != int(MAGIC_NUMBER):
            print(f"[STRAT][WARN] MAGIC mismatch: terminal={magic} file={MAGIC_NUMBER}", flush=True)

        self.symbol = symbol
        self.timeframe = timeframe
        self.magic = int(magic)
        self.data_feed = data_feed
        self.engine = get_shared_engine()

        # ─────────────────────────────────────────
        # PARÁMETROS DE INDICADORES (EDITABLES)
        # ─────────────────────────────────────────
        self.fast_len = 9
        self.slow_len = 21
        self.atr_period = 14

        # ─────────────────────────────────────────
        # SL / TP (EDITABLES)
        # ─────────────────────────────────────────
        # NOTE: values set to match pseudocode:
        # StopLossCoef1 = 1, ProfitTargetCoef1 = 0.5 (both in ATR units)
        self.atr_sl_mult = 1.0
        self.atr_tp_mult = 0.5  # TP opcional; si no aplica en una estrategia, se puede retornar None (se normaliza a 0.0)

        # ─────────────────────────────────────────
        # MONEY MANAGEMENT (EDITABLE)
        # ─────────────────────────────────────────
        self.risk_pct_balance = float(RISK_PCT_BALANCE)
        self.fixed_risk_usdt = float(FIXED_RISK_USDT)

        # ─────────────────────────────────────────
        # GESTIÓN DE TRADE (EDITABLES)
        # ─────────────────────────────────────────
        self.min_bars_in_trade = 2
        self.max_bars_in_trade = 80

        # ─────────────────────────────────────────
        # ESTADO INTERNO
        # ─────────────────────────────────────────
        self._last_bar_ts: Optional[int] = None
        self._has_position: bool = False
        self._bars_in_trade: int = 0

        self._rehydrate_from_monitor()

    # ─────────────────────────────────────────
    # LOOP PRINCIPAL
    # ─────────────────────────────────────────

    def run(self, stop_event) -> None:
        while not stop_event.is_set():
            try:
                self._loop_once()
            except Exception as e:
                print(f"[STRAT][{self.magic}][ERR] {e}", flush=True)
            time.sleep(1.0)
    def _loop_once(self) -> None:
        # Sincronizar _has_position con el estado real del engine antes de evaluar señales.
        # Necesario para detectar cierres externos (SL/TP ejecutados por el monitor).
        self._has_position = self._detect_has_position()

        candles_raw = self.data_feed.get_candles(self.symbol, self.timeframe, limit=350)
        if not candles_raw:
            return

        candles = self._normalize_candles(candles_raw)
        if len(candles) < 2:
            return

        # MT4/SQ mapping:
        #   Bar 0 = vela en formación (candles[-1])  -> solo gatillo
        #   Bar 1 = última vela cerrada (candles[-2]) -> evaluación
        current_open_ts = int(candles[-1]["ts"])

        # Primer ciclo: inicializa y espera la próxima vela nueva
        if self._last_bar_ts is None:
            self._last_bar_ts = current_open_ts
            return

        # Sin vela nueva -> no evaluar
        if current_open_ts == self._last_bar_ts:
            return

        # Vela cerrada (Bar 1)
        closed_bar = candles[-2]

        self._on_bar_close(closed_bar, candles)

        # Avanza el marcador al open de la nueva vela (Bar 0)
        self._last_bar_ts = current_open_ts

    # ─────────────────────────────────────────
    # NORMALIZACIÓN DE VELAS
    # ─────────────────────────────────────────

    def _normalize_candles(self, candles_raw: List[Any]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for c in candles_raw:
            if isinstance(c, dict):
                ts = int(c.get("ts") or c.get("time") or c.get("timestamp") or 0)
                # BLINDAJE: soportar ts en ms
                if ts > 10_000_000_000:
                    ts = ts // 1000
                o = float(c.get("open"))
                h = float(c.get("high"))
                l = float(c.get("low"))
                cl = float(c.get("close"))
                v = float(c.get("vol", 0.0))
            else:
                ts = int(c[0])
                # BLINDAJE: soportar ts en ms
                if ts > 10_000_000_000:
                    ts = ts // 1000
                o = float(c[1])
                h = float(c[2])
                l = float(c[3])
                cl = float(c[4])
                v = float(c[5]) if len(c) > 5 else 0.0

            out.append({"ts": ts, "open": o, "high": h, "low": l, "close": cl, "vol": v})
        return out

    # ─────────────────────────────────────────
    # ORDEN LÓGICO PRINCIPAL POR VELA CERRADA
    # ─────────────────────────────────────────

    def _on_bar_close(self, bar: Dict[str, Any], all_candles: List[Dict[str, Any]]) -> None:
        """
        Se llama una vez por cada vela cerrada nueva.

        ORDEN LÓGICO:
          1) Cálculo de series base
          2) Cálculo de indicadores
          3) Lógica de entrada
          4) Lógica de salida
          5) Cálculo de SL/TP
          6) Apertura / cierre de órdenes
        """
        # ✅ Indicadores: usar SOLO velas cerradas (excluye Bar 0)
        history = all_candles[:-1]

        closes = [float(c["close"]) for c in history]
        if len(closes) < max(self.slow_len, self.atr_period) + 2:
            return

        # 1) CÁLCULO DE INDICADORES
        ind = self._calculate_indicators(history, bar, closes)
        if ind is None:
            return

        # 2) LÓGICA DE ENTRADA
        long_entry_signal = self._check_entry_long(ind)

        # 3) LÓGICA DE SALIDA
        long_exit_signal = self._check_exit_long(ind)

        # 4) GESTIÓN DE POSICIÓN (OPEN/CLOSE)
        if not self._has_position:
            # Sin posición: sólo miramos señales de entrada
            if long_entry_signal:
                # 4.a) EJECUCIÓN ON BAR OPEN: entrada al OPEN de la vela siguiente
                next_open = self._get_next_open_for_bar(bar, all_candles)
                if next_open is None or next_open <= 0:
                    return

                # 4.b) CÁLCULO DE SL / TP PARA LA ENTRADA (SL obligatorio, TP opcional)
                sl, tp = self._calculate_sl_tp_long(ind, entry_price=float(next_open))

                # TP opcional: si no existe, usar 0.0 (NUNCA None)
                tp = 0.0 if tp is None else float(tp)

                # SL obligatorio
                if sl is None:
                    return
                sl = float(sl)

                # Validación coherencia LONG: SL debe ser menor al entry
                if sl <= 0 or sl >= float(next_open):
                    return

                self._open_long(entry_price=float(next_open), sl=sl, tp=tp, atr=ind["atr"])
        else:
            # Con posición abierta: contamos velas en trade y evaluamos salidas
            self._bars_in_trade += 1

            # 4.b) CIERRE POR SEÑAL DE SALIDA
            if long_exit_signal and self._bars_in_trade >= self.min_bars_in_trade:
                self._close_position(exit_type="SIGNAL_EXIT")

            # 4.c) CIERRE POR TIEMPO MÁXIMO EN TRADE
            elif self._bars_in_trade >= self.max_bars_in_trade:
                self._close_position(exit_type="TIME_EXIT")

    def _get_next_open_for_bar(self, bar: Dict[str, Any], all_candles: List[Dict[str, Any]]) -> Optional[float]:
        """
        Devuelve el OPEN de la vela siguiente a 'bar' (On Bar Open).
        Si 'bar' es la última vela disponible, retorna None.
        """
        ts = int(bar["ts"])
        for i in range(len(all_candles) - 1):
            if int(all_candles[i]["ts"]) == ts:
                return float(all_candles[i + 1]["open"])
        return None

    # ─────────────────────────────────────────
    # 1) CÁLCULO DE INDICADORES
    # ─────────────────────────────────────────

    def _calculate_indicators(
        self,
        all_candles: List[Dict[str, Any]],
        bar: Dict[str, Any],
        closes: List[float],
    ) -> Optional[Dict[str, Any]]:
        """
        Cálculo de indicadores sobre la última información disponible.

        Esta función agrupa todo el cálculo numérico:
          - ATR
          - Medias móviles (u otros indicadores futuros)
          - Series auxiliares

        Retornar dict con indicadores necesarios para las señales.
        """
        atr = self._calc_atr(all_candles, self.atr_period)
        if atr <= 0:
            return None

        fast = self._sma(closes, self.fast_len)
        slow = self._sma(closes, self.slow_len)
        if fast is None or slow is None:
            return None

        last_close = float(bar["close"])

        fast_prev = self._sma(closes[:-1], self.fast_len)
        slow_prev = self._sma(closes[:-1], self.slow_len)
        if fast_prev is None or slow_prev is None:
            return None

        # Prev close (Close[2] in SQ/MQL context) -> the close before the evaluated closed bar
        prev_close = None
        if len(closes) >= 2:
            prev_close = float(closes[-2])
        else:
            prev_close = None

        return {
            "atr": atr,
            "last_close": last_close,
            "prev_close": prev_close,
            "fast": fast,
            "slow": slow,
            "fast_prev": fast_prev,
            "slow_prev": slow_prev,
        }

    # ─────────────────────────────────────────
    # 2) LÓGICA DE ENTRADA
    # ─────────────────────────────────────────

    def _check_entry_long(self, ind: Dict[str, Any]) -> bool:
        """
        Lógica de entrada LONG basada en pseudocode:
        LongEntrySignal = (Close[1] > Close[2]);
        Además en pseudocode: require Not ShortEntrySignal (always false) and Not LongExitSignal.
        Implementamos exactamente la condición Close[1] > Close[2].
        """
        last = ind.get("last_close")
        prev = ind.get("prev_close")

        if last is None or prev is None:
            return False

        # LongEntrySignal
        if last > prev:
            # Ensure not LongExitSignal (which would be last < prev); redundant but kept for fidelity
            if not (last < prev):
                return True
        return False

    # ─────────────────────────────────────────
    # 3) LÓGICA DE SALIDA
    # ─────────────────────────────────────────

    def _check_exit_long(self, ind: Dict[str, Any]) -> bool:
        """
        Lógica de salida LONG basada en pseudocode:
        LongExitSignal = (Close[1] < Close[2]);
        """
        last = ind.get("last_close")
        prev = ind.get("prev_close")

        if last is None or prev is None:
            return False

        if last < prev:
            return True
        return False

    # ─────────────────────────────────────────
    # 4) CÁLCULO DE SL / TP
    # ─────────────────────────────────────────

    def _calculate_sl_tp_long(self, ind: Dict[str, Any], entry_price: float) -> (Optional[float], Optional[float]):
        """
        Cálculo de SL y TP para una entrada LONG según pseudocode:
          - SL = entry_price - StopLossCoef1 * ATR(14)
          - TP = entry_price + ProfitTargetCoef1 * ATR(14)
        En pseudocode: StopLossCoef1 = 1, ProfitTargetCoef1 = 0.5
        """
        atr = float(ind["atr"])
        if atr <= 0:
            # SL obligatorio: si no hay ATR válido, no se puede calcular SL
            return None, 0.0

        sl = float(entry_price) - (self.atr_sl_mult * atr)
        tp = float(entry_price) + (self.atr_tp_mult * atr)
        return sl, tp

    # ─────────────────────────────────────────
    # SMA / ATR (HELPERS DE INDICADORES)
    # ─────────────────────────────────────────

    def _sma(self, closes: List[float], length: int) -> Optional[float]:
        if len(closes) < length:
            return None
        return sum(closes[-length:]) / float(length)

    def _calc_atr(self, candles: List[Dict[str, Any]], period: int) -> float:
        if len(candles) <= period:
            return 0.0
        trs: List[float] = []
        prev_close = float(candles[0]["close"])
        for c in candles[1:]:
            h = float(c["high"])
            l = float(c["low"])
            tr = max(h - l, abs(h - prev_close), abs(l - prev_close))
            trs.append(tr)
            prev_close = float(c["close"])
        if len(trs) < period:
            return 0.0
        return sum(trs[-period:]) / float(period)

    # ─────────────────────────────────────────
    # MONEY MANAGEMENT / RIESGO
    # ─────────────────────────────────────────

    def _get_risk_usdt(self) -> float:
        eq = 0.0
        try:
            eq = float(self.engine.get_equity_usdt())
        except Exception:
            eq = 0.0

        if eq <= 0:
            return float(self.fixed_risk_usdt)
        return float(max(0.0, eq * self.risk_pct_balance))

    def _calc_lot_size_from_risk(self, entry_price: float, sl_price: float, risk_usdt: float) -> float:
        dist = abs(entry_price - sl_price)
        if dist <= 0:
            return 0.0
        return max(0.0, risk_usdt / dist)

    # ─────────────────────────────────────────
    # APERTURAS / CIERRES
    # ─────────────────────────────────────────

    def _open_long(self, entry_price: float, sl: float, tp: float, atr: float) -> None:
        """
        Envía orden LONG al engine.
        - entry_price: precio (simulado) de entrada (On Bar Open).
        - sl/tp: precios absolutos (tp puede ser 0.0 si no existe).
        """
        risk_usdt = self._get_risk_usdt()
        qty = self._calc_lot_size_from_risk(entry_price=entry_price, sl_price=sl, risk_usdt=risk_usdt)
        if qty <= 0:
            return

        print(
            f"[STRAT][{self.magic}] OPEN LONG: entry(open)={entry_price:.6f} sl={sl:.6f} tp={tp:.6f} "
            f"qty={qty:.8f} risk={risk_usdt:.2f}USDT atr={atr:.6f}",
            flush=True,
        )

        res = self.engine.process_open(
            magic=self.magic,
            symbol=self.symbol,
            side="buy",
            est_lots=qty,
            sl=sl,
            tp=tp,  # tp=0.0 permitido
        )
        if not res.ok:
            print(f"[STRAT][{self.magic}] OPEN error: {res.error}", flush=True)
            return

        print(f"[STRAT][{self.magic}] OPEN ok ordId={res.ordId}", flush=True)
        self._has_position = True
        self._bars_in_trade = 0

    def _close_position(self, exit_type: str) -> None:
        """
        Cierra la posición abierta (por señal o tiempo).
        """
        print(f"[STRAT][{self.magic}] CLOSE {exit_type}", flush=True)
        res = self.engine.process_close(
            magic=self.magic,
            ticket=None,
            exit_type=exit_type,
        )
        if not res.ok:
            print(f"[STRAT][{self.magic}] CLOSE error: {res.error}", flush=True)
            return
        print(f"[STRAT][{self.magic}] CLOSE ok ordId={res.ordId}", flush=True)
        self._has_position = False
        self._bars_in_trade = 0

    # ─────────────────────────────────────────
    # ESTADO / MONITOR
    # ─────────────────────────────────────────

    def _detect_has_position(self) -> bool:
        path = MONITOR_DIR / f"status_{self.magic}.json"
        if not path.exists():
            return False
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            status = str(data.get("status", "")).upper()
            lots = float(data.get("lots", 0.0))
            return status == "OPEN" and lots > 0.0
        except Exception:
            return False

    def _rehydrate_from_monitor(self) -> None:
        """
        Restaura estado interno básico desde el monitor.
        _bars_in_trade se estima desde open_time para que max_bars_in_trade
        funcione correctamente tras un reinicio del terminal.
        """
        self._has_position = self._detect_has_position()
        self._bars_in_trade = self._estimate_bars_in_trade()

    def _estimate_bars_in_trade(self) -> int:
        """
        Estima cuántas velas han pasado desde que se abrió la posición,
        leyendo open_time desde pos_{magic}.json.
        Devuelve 0 si no hay posición o si open_time no está disponible.
        """
        if not self._has_position:
            return 0
        try:
            pos_path = MONITOR_DIR / f"pos_{self.magic}.json"
            if not pos_path.exists():
                return 0
            data = json.loads(pos_path.read_text(encoding="utf-8"))
            open_time_str = data.get("open_time", "")
            if not open_time_str:
                return 0

            # Parsear open_time (formato ISO: "2026-06-28T12:34:56.789Z" o similar)
            from datetime import datetime, timezone
            open_time_str = open_time_str.replace("Z", "+00:00")
            open_dt = datetime.fromisoformat(open_time_str)
            if open_dt.tzinfo is None:
                open_dt = open_dt.replace(tzinfo=timezone.utc)

            now_dt = datetime.now(tz=timezone.utc)
            elapsed_sec = (now_dt - open_dt).total_seconds()

            # Duración en segundos por vela según el timeframe
            tf_map = {
                "1m": 60, "3m": 180, "5m": 300, "15m": 900,
                "30m": 1800, "1H": 3600, "2H": 7200, "4H": 14400,
                "6H": 21600, "12H": 43200, "1D": 86400, "1Dutc": 86400,
            }
            tf_sec = tf_map.get(self.timeframe, 900)
            estimated = max(0, int(elapsed_sec / tf_sec))
            print(
                f"[STRAT][{self.magic}] Rehidratando: open_time={open_time_str} "
                f"elapsed={elapsed_sec:.0f}s tf={self.timeframe}({tf_sec}s) "
                f"bars_estimadas={estimated}",
                flush=True,
            )
            return estimated
        except Exception as exc:
            print(f"[STRAT][{self.magic}][WARN] _estimate_bars_in_trade error: {exc}", flush=True)
            return 0