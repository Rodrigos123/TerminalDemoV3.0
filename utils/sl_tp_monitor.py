"""
sl_tp_monitor.py — Monitor de SL/TP para Terminal Python OKX
=============================================================
Loop independiente (thread daemon) que cada N segundos revisa todas
las posiciones abiertas en el ExecutionEngine y ejecuta el cierre
si el precio actual toca el SL o el TP.

Fuente de precio: DataFeed.get_last_price() (caché local).
No llama a OKX directamente en cada ciclo para evitar rate-limit.

Lógica de activación (LONG spot):
  - SL se toca si precio_actual <= sl_price
  - TP se toca si precio_actual >= tp_price

El cierre se delega a engine.process_close() con exit_type="SL" o "TP",
usando la misma orden market que el cierre por señal de estrategia.
"""

from __future__ import annotations

import threading
import time
from typing import Optional

from utils.data_feed import DataFeed
from utils.engine_execution import get_shared_engine


class SLTPMonitor:
    """
    Thread daemon que monitorea SL/TP de todas las posiciones abiertas.

    Parámetros
    ----------
    data_feed : DataFeed
        Instancia compartida del DataFeed para leer precio desde caché.
    interval_sec : float
        Segundos entre cada ciclo de comprobación (default: 5).
    verbose : bool
        Si True, imprime cada ciclo aunque no haya activaciones.
    """

    def __init__(
        self,
        data_feed: DataFeed,
        interval_sec: float = 5.0,
        verbose: bool = False,
    ) -> None:
        self._data_feed = data_feed
        self._interval = interval_sec
        self._verbose = verbose
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ── API pública ──────────────────────────────────────────────────────────

    def start(self) -> None:
        """Inicia el thread daemon del monitor."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name="SLTPMonitor",
            daemon=True,
        )
        self._thread.start()
        print("[SLTP] Monitor de SL/TP iniciado", flush=True)

    def stop(self) -> None:
        """Señala al thread que se detenga y espera a que termine."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=10)
        print("[SLTP] Monitor de SL/TP detenido", flush=True)

    # ── Loop interno ─────────────────────────────────────────────────────────

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._check_all()
            except Exception as exc:
                print(f"[SLTP][ERROR] Excepción en ciclo: {exc}", flush=True)
            self._stop_event.wait(timeout=self._interval)

    def _check_all(self) -> None:
        """
        Itera sobre todas las posiciones abiertas y evalúa SL/TP.
        Se hace una copia del dict para no bloquear el engine durante
        la iteración completa.
        """
        engine = get_shared_engine()

        # Copia segura sin mantener el lock durante toda la iteración
        with engine._lock:
            positions = list(engine._open_by_ticket.values())

        if not positions:
            if self._verbose:
                print("[SLTP] Sin posiciones abiertas", flush=True)
            return

        for rec in positions:
            self._check_position(rec)

    def _check_position(self, rec: dict) -> None:
        """Evalúa SL y TP para una posición concreta."""
        magic   = rec.get("magic")
        symbol  = rec.get("symbol", "")
        side    = rec.get("side", "buy")
        sl      = rec.get("sl")
        tp      = rec.get("tp")

        # Sin SL ni TP definidos → nada que monitorear
        if sl is None and tp is None:
            return

        # Obtener precio actual desde caché
        price = self._data_feed.get_last_price(symbol)
        if price is None or price <= 0:
            print(f"[SLTP][WARN] Sin precio en caché para {symbol} (magic={magic})", flush=True)
            return

        if self._verbose:
            print(
                f"[SLTP] magic={magic} {symbol} precio={price:.6f} "
                f"sl={sl} tp={tp}",
                flush=True,
            )

        # Evaluación para LONG (único lado soportado actualmente en spot OKX)
        if side == "buy":
            if sl is not None and price <= float(sl):
                print(
                    f"[SLTP] SL activado — magic={magic} {symbol} "
                    f"precio={price:.6f} <= sl={sl}",
                    flush=True,
                )
                self._execute_close(magic, "SL")
                return  # No evaluar TP si ya se cerró por SL

            if tp is not None and price >= float(tp):
                print(
                    f"[SLTP] TP activado — magic={magic} {symbol} "
                    f"precio={price:.6f} >= tp={tp}",
                    flush=True,
                )
                self._execute_close(magic, "TP")

        # Placeholder para SHORT cuando se implemente
        # elif side == "sell":
        #     ...

    def _execute_close(self, magic: int, exit_type: str) -> None:
        """
        Delega el cierre al ExecutionEngine.
        Usa la misma orden market que el cierre por señal de estrategia.
        """
        try:
            engine = get_shared_engine()
            result = engine.process_close(magic=magic, exit_type=exit_type)
            if result.ok:
                print(f"[SLTP] Cierre {exit_type} OK — magic={magic}", flush=True)
            else:
                print(
                    f"[SLTP][ERROR] Cierre {exit_type} FALLÓ — magic={magic} "
                    f"error={result.error}",
                    flush=True,
                )
        except Exception as exc:
            print(
                f"[SLTP][ERROR] Excepción al cerrar magic={magic} "
                f"exit_type={exit_type}: {exc}",
                flush=True,
            )
