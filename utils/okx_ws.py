# utils/okx_ws.py
# -*- coding: utf-8 -*-
"""
OkxPrivateWS
- WS privado OKX (v5) para eventos de órdenes.
- Demo URL: wss://wspap.okx.com:8443/ws/v5/private?brokerId=9999
- Login WS + suscripción 'orders' (instType configurable).
- Reintentos y keepalive: ahora 'ping' de aplicación en TEXTO (no JSON).
"""

from __future__ import annotations

import json
import threading
import time
import hmac
import hashlib
import base64
from typing import Callable, Optional, List, Dict, Any


def _now_iso_ts() -> str:
    return str(time.time())


def _ws_urls(simulated: bool, base_domain: Optional[str]) -> str:
    # Demo requiere brokerId=9999
    if simulated:
        return "wss://wspap.okx.com:8443/ws/v5/private?brokerId=9999"
    return "wss://ws.okx.com:8443/ws/v5/private"


def _make_sign(api_secret: str, timestamp: str, method: str = "GET", path: str = "/users/self/verify") -> str:
    msg = f"{timestamp}{method}{path}".encode("utf-8")
    secret = api_secret.encode("utf-8")
    digest = hmac.new(secret, msg, hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def _extract_client_creds(client: Any) -> Dict[str, Any]:
    return {
        "api_key": getattr(client, "api_key", None),
        "api_secret": getattr(client, "api_secret", None),
        "passphrase": getattr(client, "passphrase", None),
        "simulated": bool(getattr(client, "simulated", True)),
        "base_url": getattr(client, "base_url", "https://www.okx.com"),
    }


def _maybe_print(verbose: bool, msg: str) -> None:
    if verbose:
        print(msg, flush=True)


class OkxPrivateWS(threading.Thread):
    def __init__(
        self,
        client: Any,
        on_event: Callable[[dict], None],
        *,
        retry_sec: int = 5,
        verbose: bool = False,
        name: str = "OkxPrivateWS",
        daemon: bool = True,
    ) -> None:
        super().__init__(name=name, daemon=daemon)
        self._client = client
        self._on_event = on_event
        self._retry_sec = max(1, int(retry_sec))
        self._verbose = bool(verbose)
        self._stop = threading.Event()
        self._ws = None
        self._sub_inst_ids: Optional[List[str]] = None
        self._inst_type: Optional[str] = "ANY"
        self._logged_unavail = False

        # keepalive app-level
        self._ka_thread: Optional[threading.Thread] = None
        self._ka_stop = threading.Event()

    def subscribe_orders(self, inst_ids: Optional[List[str]], inst_type: Optional[str] = "ANY") -> None:
        self._sub_inst_ids = inst_ids
        if inst_type:
            self._inst_type = inst_type

    def stop(self) -> None:
        self._stop.set()
        self._stop_keepalive()
        try:
            if self._ws is not None:
                self._ws.close()
        except Exception:
            pass

    # ----------------- Internals -----------------

    def _try_import_websocket(self):
        try:
            import websocket  # type: ignore
            return websocket
        except Exception:
            if not self._logged_unavail:
                _maybe_print(self._verbose, "[WS][WARN] 'websocket-client' no instalado. WS deshabilitado.")
                self._logged_unavail = True
            return None

    def _login_payload(self, api_key: str, api_secret: str, passphrase: str) -> dict:
        ts = _now_iso_ts()
        sign = _make_sign(api_secret, ts)
        return {"op": "login", "args": [{"apiKey": api_key, "passphrase": passphrase, "timestamp": ts, "sign": sign}]}

    def _orders_sub_payload(self) -> dict:
        args = []
        if self._sub_inst_ids:
            for inst in self._sub_inst_ids:
                item = {"channel": "orders"}
                if self._inst_type:
                    item["instType"] = self._inst_type
                item["instId"] = inst
                args.append(item)
        else:
            item = {"channel": "orders"}
            if self._inst_type:
                item["instType"] = self._inst_type
            args.append(item)
        return {"op": "subscribe", "args": args}

    def _normalize_order_event(self, raw: dict) -> Optional[dict]:
        try:
            if raw.get("arg", {}).get("channel") != "orders":
                return None
            data = raw.get("data", [])
            if not data:
                return None
            d = data[0]
            def f(x: Any) -> Optional[float]:
                try: return float(x)
                except Exception: return None
            def i(x: Any) -> Optional[int]:
                try:
                    xi = int(x)
                    if xi < 10_000_000_000: xi *= 1000
                    return xi
                except Exception:
                    return None
            return {
                "type": "order",
                "instId": d.get("instId"),
                "ordId": d.get("ordId"),
                "clOrdId": d.get("clOrdId"),
                "state": d.get("state") or d.get("status"),
                "side": d.get("side"),
                "avgPx": f(d.get("avgPx") or d.get("avgPrice")),
                "fillPx": f(d.get("fillPx")),
                "fillSz": f(d.get("fillSz")),
                "fillTime": i(d.get("fillTime")),
                "accFillSz": f(d.get("accFillSz") or d.get("accFill")),
                "px": f(d.get("px") or d.get("price")),
                "sz": f(d.get("sz") or d.get("size")),
                "pnl": f(d.get("pnl")),
                "ts": i(d.get("uTime") or d.get("cTime") or d.get("ts")),
                "raw": d,
            }
        except Exception:
            return None

    # --------------------- Keepalive (app-level) ---------------------

    def _start_keepalive(self):
        self._ka_stop.clear()
        self._ka_thread = threading.Thread(target=self._keepalive_loop, name="OKX-WS-Keepalive", daemon=True)
        self._ka_thread.start()

    def _stop_keepalive(self):
        self._ka_stop.set()

    def _keepalive_loop(self):
        # Enviar "ping" (texto) cada ~15s mientras el WS esté abierto
        while not self._ka_stop.is_set() and not self._stop.is_set():
            try:
                if self._ws is not None:
                    # 'ping' en TEXTO (OKX v5 no acepta {"op":"ping"})
                    self._ws.send("ping")
                # 15s evita el cierre por inactividad a 30s
            except Exception:
                pass
            finally:
                for _ in range(150):  # 150 * 0.1s = 15s
                    if self._ka_stop.is_set() or self._stop.is_set():
                        break
                    time.sleep(0.1)

    # --------------------- Thread cycle ---------------------

    def _run_once(self) -> None:
        websocket = self._try_import_websocket()
        if websocket is None:
            time.sleep(self._retry_sec)
            return

        creds = _extract_client_creds(self._client)
        url = _ws_urls(simulated=creds["simulated"], base_domain=creds["base_url"])
        headers = []
        if creds["simulated"]:
            headers.append("x-simulated-trading: 1")

        wsapp = websocket.WebSocketApp(
            url,
            header=headers,
            on_message=lambda ws, msg: self._on_message(msg),
            on_open=lambda ws: self._on_open(ws, creds),
            on_error=lambda ws, err: self._on_error(err),
            on_close=lambda ws, *args: self._on_close(*args),
        )
        self._ws = wsapp

        _maybe_print(self._verbose, f"[WS][CONNECT] {url}")
        try:
            # ping_interval: ping de protocolo; OKX además agradece ping en texto (arriba)
            wsapp.run_forever(ping_interval=20, ping_timeout=10)
        except Exception as e:
            _maybe_print(self._verbose, f"[WS][RUN][ERROR] {e}")
        finally:
            self._ws = None
            self._stop_keepalive()

    # -------------- WebSocketApp callbacks --------------

    def _on_open(self, ws, creds: Dict[str, Any]) -> None:
        _maybe_print(self._verbose, "[WS][OPEN]")
        try:
            pay = self._login_payload(creds["api_key"], creds["api_secret"], creds["passphrase"])
            ws.send(json.dumps(pay))
            _maybe_print(self._verbose, "[WS][SEND] login")
        except Exception as e:
            _maybe_print(self._verbose, f"[WS][LOGIN][ERROR] {e}")

    def _on_message(self, msg: str) -> None:
        # OKX puede responder "pong" como TEXTO
        if isinstance(msg, str) and msg.strip().lower() == "pong":
            return  # silencio para no spamear

        try:
            data = json.loads(msg)
        except Exception:
            _maybe_print(self._verbose, f"[WS][RECV] {msg}")
            return

        # ack de login
        if isinstance(data, dict) and data.get("event") == "login":
            if data.get("code") == "0":
                _maybe_print(self._verbose, "[WS][AUTH] login OK")
                try:
                    sub = self._orders_sub_payload()
                    if self._ws:
                        self._ws.send(json.dumps(sub))
                        _maybe_print(self._verbose, f"[WS][SEND] subscribe orders instType={self._inst_type} ({'all' if not self._sub_inst_ids else ','.join(self._sub_inst_ids)})")
                    # iniciar keepalive de aplicación tras login OK
                    self._start_keepalive()
                except Exception as e:
                    _maybe_print(self._verbose, f"[WS][SUBSCRIBE][ERROR] {e}")
            else:
                _maybe_print(self._verbose, f"[WS][AUTH][FAIL] {data}")
            return

        # acks y eventos varios
        if isinstance(data, dict):
            ev = data.get("event")
            if ev == "subscribe":
                _maybe_print(self._verbose, f"[WS][SUBSCRIBED] {data.get('arg')}")
                return
            if ev == "channel-conn-count":
                return
            if ev == "error":
                _maybe_print(self._verbose, f"[WS][RECV][ERROR] {data}")
                return
            if ev == "pong":
                return  # pong JSON (por si acaso)

        # normalizar evento de órdenes
        evt = self._normalize_order_event(data)
        if evt:
            try:
                self._on_event(evt)
            except Exception:
                _maybe_print(self._verbose, "[WS][EVENT][ERROR] callback lanzó excepción")
        else:
            if self._verbose:
                ch = data.get("arg", {}).get("channel") if isinstance(data, dict) else None
                if ch != "orders":
                    _maybe_print(self._verbose, f"[WS][RECV] {data}")

    def _on_error(self, err: Any) -> None:
        _maybe_print(self._verbose, f"[WS][ERROR] {err}")

    def _on_close(self, *args) -> None:
        _maybe_print(self._verbose, "[WS][CLOSE]")

    # --------------------- Main loop ---------------------

    def run(self) -> None:
        while not self._stop.is_set():
            self._run_once()
            end = time.time() + self._retry_sec
            while time.time() < end and not self._stop.is_set():
                time.sleep(0.1)
