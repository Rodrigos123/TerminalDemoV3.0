from __future__ import annotations
"""
────────────────────────────────────────────────────────────
 OrderExecutor  (USO INTERNO)
────────────────────────────────────────────────────────────
Capa interna para gestión de órdenes y rate-limit.

Responsabilidades:
  • Aplicar un rate-limit global y por estrategia.
  • Llevar cierto estado interno de órdenes pendientes.
  • Recibir eventos del WebSocket (on_order_event) para
    saber cuándo una orden pasa de live → filled/canceled.

⚠️ IMPORTANTE:
  • Las ESTRATEGIAS NUNCA deben usar OrderExecutor directamente.
  • La ÚNICA API de ejecución para estrategias es:
        from utils.engine_execution import get_shared_engine
        engine = get_shared_engine()
        engine.process_open(...)
        engine.process_close(...)

Este módulo está pensado como infraestructura interna que
en el futuro puede ser usada por ExecutionEngine para
coordinación avanzada con el WS, sin que las estrategias
se enteren de los detalles.
────────────────────────────────────────────────────────────
"""

import threading
import time
from typing import Dict, Any, Optional

class OrderExecutor:
    def __init__(
        self,
        client,
        max_exposure_pct: float = 0.20,
        rate_global_per_sec: float = 5.0,
        rate_per_strategy_per_sec: float = 2.0,
        pause_when_pending: bool = True,
        verbose: bool = False,
    ) -> None:
        self.client = client
        self.max_exposure_pct = float(max_exposure_pct)
        self.rate_global_per_sec = float(rate_global_per_sec)
        self.rate_per_strategy_per_sec = float(rate_per_strategy_per_sec)
        self.pause_when_pending = bool(pause_when_pending)
        self.verbose = bool(verbose)

        # Rate limit: timestamps de últimos envíos globales y por magic
        self._lock = threading.Lock()
        self._last_global_ts: float = 0.0
        self._last_ts_by_magic: Dict[int, float] = {}

        # Estado mínimo de órdenes pendientes (por ordId)
        self._pending: Dict[str, Dict[str, Any]] = {}

    # ───────────────── Rate limiting interno ─────────────────

    def _wait_rate_limit(self, magic: Optional[int]) -> None:
        """
        Aplica un pequeño sleep para respetar el rate-limit global
        y por estrategia (magic).
        """
        with self._lock:
            now = time.time()
            # Global
            if self.rate_global_per_sec > 0:
                min_interval_g = 1.0 / self.rate_global_per_sec
                elapsed_g = now - self._last_global_ts
                if elapsed_g < min_interval_g:
                    wait = min_interval_g - elapsed_g
                    if wait > 0:
                        time.sleep(wait)
                        now = time.time()
                self._last_global_ts = now

            # Por estrategia (magic)
            if magic is not None and self.rate_per_strategy_per_sec > 0:
                min_interval_s = 1.0 / self.rate_per_strategy_per_sec
                last_s = self._last_ts_by_magic.get(magic, 0.0)
                elapsed_s = now - last_s
                if elapsed_s < min_interval_s:
                    wait = min_interval_s - elapsed_s
                    if wait > 0:
                        time.sleep(wait)
                        now = time.time()
                self._last_ts_by_magic[magic] = now

    # ───────────────── Consultas de estado ─────────────────

    def has_pending_for_magic(self, magic: int) -> bool:
        """Devuelve True si hay alguna orden pendiente asociada a este magic."""
        with self._lock:
            for info in self._pending.values():
                if info.get("magic") == magic and info.get("state") not in ("filled", "canceled"):
                    return True
        return False

    # ───────────────── Envío de órdenes ─────────────────

    def place_spot_order(
        self,
        *,
        instId: str,
        tdMode: str,
        side: str,
        sz: str,
        ordType: str = "market",
        clOrdId: Optional[str] = None,
        magic: Optional[int] = None,
        **extra,
    ) -> Dict[str, Any]:
        """
        Envía una orden al cliente OKX aplicando rate-limit.

        ⚠️ Uso interno (por capas superiores como ExecutionEngine).
        """
        self._wait_rate_limit(magic)
        if self.verbose:
            print(
                f"[ORDER_EXEC][SEND] instId={instId} side={side} sz={sz} "
                f"ordType={ordType} clOrdId={clOrdId}",
                flush=True,
            )
        res = self.client.place_order(
            instId=instId,
            side=side,
            tdMode=tdMode,
            sz=sz,
            ordType=ordType,
            clOrdId=clOrdId,
            **extra,
        )

        try:
            data = (res.get("data") or [{}])[0]
            ordId = str(data.get("ordId") or "")
            if ordId:
                with self._lock:
                    self._pending[ordId] = {
                        "magic": magic,
                        "instId": instId,
                        "state": data.get("state") or "live",
                        "raw": data,
                    }
            if self.verbose:
                print(f"[ORDER_EXEC][RESP] ordId={ordId} data={data}", flush=True)
        except Exception as e:
            if self.verbose:
                print(f"[ORDER_EXEC][WARN] No se pudo parsear respuesta: {e} res={res}", flush=True)
        return res

    # ───────────────── Eventos desde WsRouter ─────────────────

    def on_order_event(self, evt: Dict[str, Any]) -> None:
        """
        Handler de eventos de orden normalizados desde WsRouter.

        Se espera al menos:
          - evt["ordId"]
          - evt["state"]
          - evt["instId"]

        En esta versión sólo actualiza el estado interno de pendientes
        y puede usarse para debug / métricas; la lógica de P/L y monitor
        sigue residiendo en ExecutionEngine.
        """
        ordId = str(evt.get("ordId") or "")
        if not ordId:
            return
        state = evt.get("state") or ""
        with self._lock:
            info = self._pending.get(ordId)
            if info is None:
                # Registrar si no lo conocíamos aún
                self._pending[ordId] = {
                    "magic": evt.get("magic"),
                    "instId": evt.get("instId"),
                    "state": state,
                    "raw": evt,
                }
            else:
                info["state"] = state
                info["raw"] = evt
        if self.verbose:
            print(
                f"[ORDER_EXEC][EVT] ordId={ordId} instId={evt.get('instId')} state={state}",
                flush=True,
            )

# ───────────────── Instancia compartida ─────────────────

_shared_executor: Optional[OrderExecutor] = None
_shared_lock = threading.Lock()

def init_shared_executor(
    client,
    max_exposure_pct: float,
    rate_global_per_sec: float,
    rate_per_strategy_per_sec: float,
    pause_when_pending: bool,
    verbose: bool,
) -> OrderExecutor:
    global _shared_executor
    with _shared_lock:
        if _shared_executor is None:
            _shared_executor = OrderExecutor(
                client=client,
                max_exposure_pct=max_exposure_pct,
                rate_global_per_sec=rate_global_per_sec,
                rate_per_strategy_per_sec=rate_per_strategy_per_sec,
                pause_when_pending=pause_when_pending,
                verbose=verbose,
            )
    return _shared_executor

def get_shared_executor() -> OrderExecutor:
    if _shared_executor is None:
        raise RuntimeError("OrderExecutor no inicializado (llama init_shared_executor primero).")
    return _shared_executor
