from __future__ import annotations
from pathlib import Path
from typing import Dict, Any
from datetime import datetime, timezone

from utils.common import write_json_atomic, read_json

def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00","Z")

def write_account_and_positions(client, monitor_dir: Path, auth_ok: bool = True) -> None:
    """Escribe account.json y calcula UPL en tiempo real usando tickers."""
    bal_usdt = 0.0
    equity_total = 0.0
    upl_total = 0.0
    try:
        if auth_ok:
            acc = client.get_account_balance("USDT")
            data = (acc.get("data") or [{}])[0]
            equity_total = float(data.get("totalEq", 0.0) or 0.0)
            for d in (data.get("details") or []):
                if (d.get("ccy") or "").upper() == "USDT":
                    bal_usdt = float(d.get("cashBal", 0.0) or 0.0)
                    break
    except Exception:
        pass
    try:
        opens = read_json(monitor_dir / "open_positions.json", {"positions": []})
        for p in (opens.get("positions") or []):
            sym = p.get("symbol","")
            side = (p.get("side") or "").lower()
            lots = float(p.get("lots", 0.0) or 0.0)
            oprice = float(p.get("open_price", 0.0) or 0.0)
            if sym and lots and oprice:
                try:
                    t = client.get_ticker(sym)
                    d = (t.get("data") or [{}])[0]
                    last = float(d.get("last", d.get("lastPx", 0.0)))
                    if side == "buy":
                        upl_total += (last - oprice) * lots
                except Exception:
                    pass
    except Exception:
        pass

    account = {"ts": _now_iso(), "balance_usdt": bal_usdt, "equity_total": equity_total, "upl_total": upl_total}
    write_json_atomic(monitor_dir / "account.json", account)
