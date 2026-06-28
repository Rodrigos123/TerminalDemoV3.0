from __future__ import annotations
"""
────────────────────────────────────────────────────────────
 WsRouter  (USO INTERNO)
────────────────────────────────────────────────────────────
Recibe mensajes del WebSocket privado (canal `orders`) y:

  • Normaliza el payload de OKX.
  • Loguea un resumen legible en consola.
  • Llama a todos los handlers registrados (por ejemplo,
    OrderExecutor.on_order_event).

⚠️ IMPORTANTE:
  • WsRouter NO escribe trade_log.csv.
  • WsRouter NO actualiza los JSON de monitor.
  • Las estrategias nunca deben usar WsRouter directamente.

La fuente de verdad de posiciones, P/L y logs sigue siendo
ExecutionEngine, que llama a OKXClient.get_fills() y mantiene
trade_log.csv + monitor/.

En esta versión del terminal los parámetros opcionales
`status_store` y `trade_logger` están reservados para
usos futuros y normalmente se pasan como None; WsRouter
no modifica por sí mismo el estado del monitor ni los
logs de trades.
────────────────────────────────────────────────────────────
"""

import threading
from typing import Any, Callable, Dict, List, Optional

class WsRouter:
    def __init__(
        self,
        status_store: Any = None,
        trade_logger: Any = None,
        extra_handlers: Optional[List[Callable[[Dict[str, Any]], None]]] = None,
        verbose: bool = False,
    ) -> None:
        self.status_store = status_store
        self.trade_logger = trade_logger
        self.verbose = bool(verbose)

        self._handlers: List[Callable[[Dict[str, Any]], None]] = []
        if extra_handlers:
            self._handlers.extend(extra_handlers)

        self._lock = threading.Lock()

    # ───────────────── Registro de handlers ─────────────────

    def add_handler(self, handler: Callable[[Dict[str, Any]], None]) -> None:
        """
        Registra un handler que será llamado con cada evento
        de orden normalizado. Normalmente:
            executor.on_order_event
        """
        if not callable(handler):
            return
        with self._lock:
            self._handlers.append(handler)

    # ───────────────── Entrada desde OkxPrivateWS ─────────────────

    def handle(self, msg: Dict[str, Any]) -> None:
        """
        Entrada principal llamada por OkxPrivateWS.on_event.

        Espera mensajes de OKX del estilo:
            {
              "arg": {...},
              "event": "update",
              "data": [ {...}, {...} ]
            }
        Filtra sólo canal `orders`.
        """
        try:
            arg = msg.get("arg") or {}
            channel = arg.get("channel")
            if channel != "orders":
                return

            data_list = msg.get("data") or []
            for raw_evt in data_list:
                evt = self._normalize_order_event(raw_evt)
                self._log_order_event(evt)
                self._dispatch(evt)
        except Exception as e:
            print(f"[WS-ROUTER][ERROR] handle failed: {e} msg={msg}", flush=True)

    # ───────────────── Normalización ─────────────────

    def _normalize_order_event(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        """
        Convierte el payload de OKX en un dict más simple.
        No intenta cubrir todos los campos, sólo lo básico
        para debug / posibles usos internos.
        """
        return {
            "instId": raw.get("instId"),
            "ordId": raw.get("ordId"),
            "clOrdId": raw.get("clOrdId"),
            "state": raw.get("state"),
            "px": raw.get("px") or raw.get("avgPx"),
            "sz": raw.get("sz") or raw.get("accFillSz"),
            "fee": raw.get("fee"),
            "pnl": raw.get("pnl"),
            "uTime": raw.get("uTime"),
            "cTime": raw.get("cTime"),
            "side": raw.get("side"),
            "tdMode": raw.get("tdMode"),
            "raw": raw,
        }

    # ───────────────── Logging ─────────────────

    def _log_order_event(self, evt: Dict[str, Any]) -> None:
        if not self.verbose:
            return
        print(
            "[WS-ROUTER] ord={ord} inst={inst} state={st} px={px} sz={sz} ts={ts}".format(
                ord=evt.get("ordId"),
                inst=evt.get("instId"),
                st=evt.get("state"),
                px=evt.get("px"),
                sz=evt.get("sz"),
                ts=evt.get("uTime") or evt.get("cTime"),
            ),
            flush=True,
        )

    # ───────────────── Dispatch a handlers ─────────────────

    def _dispatch(self, evt: Dict[str, Any]) -> None:
        with self._lock:
            handlers = list(self._handlers)
        for h in handlers:
            try:
                h(evt)
            except Exception as e:
                print(f"[WS-ROUTER][ERROR] handler failed: {e}", flush=True)
