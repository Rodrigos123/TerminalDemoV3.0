from __future__ import annotations
from pathlib import Path
from typing import Dict, Any
from datetime import datetime, timezone

from utils.common import write_json_atomic, read_json, broker_now_ms


def _ms_to_iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _get_broker_ts_ms(client, fallback_symbols=None) -> int | None:
    """Intenta obtener timestamp del servidor OKX (ms) desde ticker. Devuelve None si falla."""
    syms = list(fallback_symbols or [])
    # fallback razonable
    for s in ["BTC-USDT", "ETH-USDT"]:
        if s not in syms:
            syms.append(s)

    for sym in syms:
        try:
            t = client.get_ticker(sym)
            d = (t.get("data") or [{}])[0]
            ts = d.get("ts") or d.get("timestamp")
            if ts is None:
                continue
            ms = int(float(ts))
            # OKX ticker ts viene en ms
            if ms < 10_000_000_000:  # por si viene en segundos
                ms *= 1000
            return ms
        except Exception:
            continue
    return None


def write_account_and_positions(client, monitor_dir: Path, auth_ok: bool = True) -> None:
    """
    Escribe:
      - monitor/account.json  → balance_usdt, equity_total, upl_total
      - monitor/open_positions.json → añade open_pl por posición y upl_by_symbol

    Open P/L (UPL) se calcula SIEMPRE desde open_positions.json + tickers OKX:
      upl_total = sum_s ( last_s - open_price_s ) * lots_s   (para BUY)
    """
    bal_usdt = 0.0
    equity_total = 0.0

    # 1) Datos de cuenta (balance / equity) desde OKX
    if auth_ok:
        try:
            acc = client.get_account_balance(None)
            data = (acc.get("data") or [{}])[0]

            # Patrimonio total
            try:
                equity_total = float(data.get("totalEq", 0.0) or 0.0)
            except Exception:
                equity_total = 0.0

            # Buscar saldo USDT
            details = data.get("details") or []
            for d in details:
                if (d.get("ccy") or "").upper() == "USDT":
                    try:
                        bal_usdt = float(d.get("cashBal", 0.0) or 0.0)
                    except Exception:
                        bal_usdt = 0.0
                    break
        except Exception:
            # Si falla la llamada a cuenta, dejamos 0 y seguimos con UPL
            pass

    # 1b) Leer open_positions.json temprano para obtener 'symbols' como fallback de tiempo broker
    op_path = monitor_dir / "open_positions.json"
    op_obj: Dict[str, Any] = read_json(op_path, default={"positions": [], "totals_by_symbol": {}, "symbols": []})
    positions = op_obj.get("positions") or []
    totals_by_symbol = op_obj.get("totals_by_symbol") or {}
    symbols = op_obj.get("symbols") or []

    # 1c) Timestamp broker (OKX) en ms.
    # Regla: NUNCA usar hora VPS. Si no hay broker time, queda None.
    broker_ts_ms = None
    if auth_ok:
        try:
            # Prioridad: última vela M1 desde caché -> ticker ts -> snapshot
            broker_ts_ms = broker_now_ms(client=client, monitor_dir=monitor_dir, fallback_symbols=symbols)
        except Exception:
            broker_ts_ms = None

    # Si falla el ticker, intentamos tomar 'ts' desde la respuesta de account balance (si existe).
    if broker_ts_ms is None and auth_ok:
        try:
            _acc = locals().get('acc') or client.get_account_balance(None)
            _data0 = (_acc.get('data') or [{}])[0]
            _ts = _data0.get('ts') or _data0.get('uTime') or _data0.get('timestamp')
            if _ts is not None:
                broker_ts_ms = int(float(_ts))
                if broker_ts_ms < 10_000_000_000:
                    broker_ts_ms *= 1000
        except Exception:
            broker_ts_ms = None

    broker_ts_iso = _ms_to_iso(broker_ts_ms) if broker_ts_ms is not None else ""

    # 2) Open P/L (UPL) a partir de open_positions.json

    upl_total = 0.0
    upl_by_symbol: Dict[str, float] = {}

    if auth_ok and positions:
        for p in positions:
            try:
                sym = p.get("symbol", "")
                side = (p.get("side", "") or "").lower()
                lots = float(p.get("lots", 0.0) or 0.0)
                oprice = float(p.get("open_price", 0.0) or 0.0)
            except Exception:
                continue

            if not sym or lots <= 0.0 or oprice <= 0.0:
                continue

            # Último precio desde OKX
            try:
                t = client.get_ticker(sym)
                d = (t.get("data") or [{}])[0]
                last = float(d.get("last", d.get("lastPx", 0.0)) or 0.0)
            except Exception:
                continue
            if last <= 0.0:
                continue

            # Solo trabajamos con spot "largos" (buy). Si en el futuro hay sells, se ajusta aquí.
            if side == "sell":
                pl = (oprice - last) * lots
            else:
                pl = (last - oprice) * lots

            # Guardamos P/L por posición (para quien quiera usarlo)
            p["open_pl"] = pl

            # Acumulados
            upl_total += pl
            upl_by_symbol[sym] = upl_by_symbol.get(sym, 0.0) + pl

    # 3) Actualizar open_positions.json enriquecido
    new_open_positions = {
        "ts": broker_ts_iso,
        "broker_ts_ms": broker_ts_ms,
        "positions": positions,
        "totals_by_symbol": totals_by_symbol,
        "symbols": symbols,
        "upl_by_symbol": upl_by_symbol,
        "upl_total": upl_total,
    }
    write_json_atomic(op_path, new_open_positions)

    # 4) Escribir account.json coherente con lo anterior
    account = {
        "ts": broker_ts_iso,
        "broker_ts_ms": broker_ts_ms,
        "balance_usdt": float(bal_usdt),
        "equity_total": float(equity_total),
        "upl_total": float(upl_total),
    }
    write_json_atomic(monitor_dir / "account.json", account)