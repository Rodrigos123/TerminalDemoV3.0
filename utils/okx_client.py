# C:\btc_test\TerminalDemoV2.0\utils\okx_client.py
from __future__ import annotations
import base64, hashlib, hmac, json, time
from dataclasses import dataclass
from typing import Any, Dict, Optional
from urllib.parse import urlencode
import requests

class ExchangeError(Exception):
    pass

@dataclass
class OKXResponse:
    code: str
    msg: str
    data: list

def _now_ms() -> int:
    return int(time.time() * 1000)

class OKXClient:
    """
    Cliente mínimo OKX v5 REST (solo endpoints necesarios).
    Compatible con cuenta demo (x-simulated-trading: 1).
    """
    def __init__(self, api_key: str, api_secret: str, passphrase: str,
                 base_url: str = "https://www.okx.com", simulated: bool = False,
                 http_debug: bool = False, timeout: float = 10.0) -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self.passphrase = passphrase
        self.base_url = base_url.rstrip("/")
        self.simulated = bool(simulated)
        self.http_debug = bool(http_debug)
        self.timeout = float(timeout)
        self.session = requests.Session()
        self._last_sign = {"ts":"", "sign":""}

    # ---------- firma ----------
    def _sign(self, method: str, path: str, body: Optional[dict] = None, params: Optional[dict] = None) -> Dict[str, str]:
        ts = "{:.3f}".format(_now_ms() / 1000.0)
        q = "" if not params else ("?" + urlencode(params, doseq=True))
        payload = ts + method.upper() + path + q + (json.dumps(body) if body else "")
        mac = hmac.new(self.api_secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256)
        sign = base64.b64encode(mac.digest()).decode("utf-8")
        return {"ts": ts, "sign": sign}

    def _headers(self, signed: bool) -> Dict[str, str]:
        h = {"Content-Type": "application/json", "Accept": "application/json"}
        if signed:
            h["OK-ACCESS-KEY"] = self.api_key
            h["OK-ACCESS-PASSPHRASE"] = self.passphrase
            h["OK-ACCESS-SIGN"] = self._last_sign["sign"]
            h["OK-ACCESS-TIMESTAMP"] = self._last_sign["ts"]
            if self.simulated:
                h["x-simulated-trading"] = "1"
        return h

    def _request(self, method: str, path: str, *, params: Optional[dict]=None, body: Optional[dict]=None, signed: bool=False) -> OKXResponse:
        url = self.base_url + path
        self._last_sign = {"ts":"", "sign":""}
        if signed:
            self._last_sign = self._sign(method, path, body, params)
        headers = self._headers(signed)
        if self.http_debug:
            print(f"[HTTP] {method} {url} params={params} body={body}")
        resp = self.session.request(method, url, params=params, json=body, headers=headers, timeout=self.timeout)
        if self.http_debug:
            print(f"[HTTP] <- {resp.status_code} {resp.text[:300]}")
        js = resp.json()
        code = str(js.get("code", ""))
        msg = str(js.get("msg", ""))
        data = js.get("data", [])
        if code not in ("0", "200"):
            raise ExchangeError(f"{code} {msg}")
        return OKXResponse(code=code, msg=msg, data=data)

    # ---------- market ----------
    def get_ticker(self, instId: str) -> Dict[str, Any]:
        res = self._request("GET", "/api/v5/market/ticker", params={"instId": instId}, signed=False)
        return {"code": res.code, "msg": res.msg, "data": res.data}

    def get_candles(self, instId: str, bar: str = "1m", limit: int = 200) -> Dict[str, Any]:
        params = {"instId": instId, "bar": bar, "limit": str(int(limit))}
        res = self._request("GET", "/api/v5/market/candles", params=params, signed=False)
        out = []
        # OKX retorna más reciente primero; devolvemos oldest->newest
        for it in reversed(res.data):
            ts, o, h, l, c = int(it[0]), float(it[1]), float(it[2]), float(it[3]), float(it[4])
            out.append({"ts": ts, "open": o, "high": h, "low": l, "close": c})
        return {"code": res.code, "msg": res.msg, "data": out}

    # ---------- account ----------
    def get_account_balance(self, ccy: Optional[str] = None) -> Dict[str, Any]:
        params = {}
        if ccy:
            params["ccy"] = ccy
        res = self._request("GET", "/api/v5/account/balance", params=params, signed=True)
        return {"code": res.code, "msg": res.msg, "data": res.data}

    # ---------- trading ----------
    def place_order(self, instId: str, side: str, sz: str,
                    ordType: str = "market", tdMode: str = "cash",
                    tgtCcy: Optional[str] = None, ccy: Optional[str] = None,
                    px: Optional[str] = None, clOrdId: Optional[str] = None) -> Dict[str, Any]:
        """
        POST /api/v5/trade/order
        - Spot BUY en USDT: usar tgtCcy='quote_ccy' y 'sz' en USDT.
        - Spot SELL: 'sz' en base (BTC).
        """
        body: Dict[str, Any] = {
            "instId": instId,
            "tdMode": tdMode,
            "side": side,
            "ordType": ordType,
            "sz": str(sz),
        }
        if clOrdId is not None:
            body["clOrdId"] = clOrdId
        if tgtCcy is not None:
            body["tgtCcy"] = tgtCcy
        if ccy is not None:
            body["ccy"] = ccy
        if px is not None:
            body["px"] = str(px)
        res = self._request("POST", "/api/v5/trade/order", body=body, signed=True)
        return {"code": res.code, "msg": res.msg, "data": res.data}

    def cancel_order(self, instId: str, ordId: str) -> Dict[str, Any]:
        body = {"instId": instId, "ordId": ordId}
        res = self._request("POST", "/api/v5/trade/cancel-order", body=body, signed=True)
        return {"code": res.code, "msg": res.msg, "data": res.data}

    def get_fills(self, instId: Optional[str] = None, ordId: Optional[str] = None, limit: int = 100) -> Dict[str, Any]:
        params: Dict[str, Any] = {}
        if instId:
            params["instId"] = instId
        if ordId:
            params["ordId"] = ordId
        params["limit"] = str(int(limit))
        res = self._request("GET", "/api/v5/trade/fills", params=params, signed=True)
        return {"code": res.code, "msg": res.msg, "data": res.data}
