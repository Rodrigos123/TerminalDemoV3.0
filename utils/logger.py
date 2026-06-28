from __future__ import annotations
from pathlib import Path
from typing import Dict, Any, List
import csv
from datetime import datetime, timezone

CSV_HEADER: List[str] = [
    "Type","Ticket","Symbol","Lots","Buy/sell","Open price","Close price",
    "Open time","Close time","Open date","Close date","Profit","Swap","Commission",
    "Net profit","T/P","S/L","Pips","Result","Trade duration (hours)","Magic number","Order comment","Account"
]

DELIM = "\t"


# ────────────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────────────

def _ensure_csv(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        with path.open("w", encoding="utf-8", newline="") as f:
            w = csv.writer(f, delimiter=DELIM)
            w.writerow(CSV_HEADER)
        return

    try:
        with path.open("r", encoding="utf-8", newline="") as f:
            first = f.readline()
        if not first:
            with path.open("w", encoding="utf-8", newline="") as f:
                w = csv.writer(f, delimiter=DELIM)
                w.writerow(CSV_HEADER)
    except Exception:
        with path.open("w", encoding="utf-8", newline="") as f:
            w = csv.writer(f, delimiter=DELIM)
            w.writerow(CSV_HEADER)


def _fmt_decimal(v: Any) -> str:
    if v is None:
        return ""
    try:
        f = float(v)
    except Exception:
        s = str(v).strip()
        return s
    s = f"{f:.10f}".rstrip("0").rstrip(".")
    return s if s else "0"


def _iso_to_date(val: str) -> str:
    s = (val or "").strip()
    if not s:
        return ""
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        return s[:10]
    try:
        s2 = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s2)
        return dt.date().isoformat()
    except Exception:
        return ""


def _parse_iso(val: str):
    s = (val or "").strip()
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _sanitize_row(row: Dict[str, Any]) -> Dict[str, Any]:
    cleaned: Dict[str, Any] = {}
    for k in CSV_HEADER:
        v = row.get(k, "")

        if k in ("Ticket", "Magic number"):
            v = "" if v is None else str(v).strip()

        elif k in (
            "Lots", "Open price", "Close price",
            "Profit", "Swap", "Commission", "Net profit",
            "Pips", "Trade duration (hours)"
        ):
            v = _fmt_decimal(v)

        else:
            v = "" if v is None else str(v)

        cleaned[k] = v

    return cleaned


def _read_rows(path: Path) -> list[Dict[str, Any]]:
    _ensure_csv(path)
    rows: list[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        first = True
        for line in f:
            if first:
                first = False
                continue
            line = line.rstrip("\r\n")
            if not line:
                continue
            parts = line.split(DELIM)
            if not any(parts):
                continue
            d: Dict[str, Any] = {}
            for i, col in enumerate(CSV_HEADER):
                d[col] = parts[i] if i < len(parts) else ""
            rows.append(_sanitize_row(d))
    return rows


def _write_rows(path: Path, rows: list[Dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, delimiter=DELIM, lineterminator="\n")
        w.writerow(CSV_HEADER)
        for r in rows:
            clean = _sanitize_row(r)
            w.writerow([clean.get(k, "") for k in CSV_HEADER])


# ────────────────────────────────────────────────────────────────────────────────
# APERTURAS
# ────────────────────────────────────────────────────────────────────────────────

def log_open(csv_path: Path, fields: Dict[str, Any]) -> None:
    _ensure_csv(csv_path)
    rows = _read_rows(csv_path)

    ticket = str(fields.get("Ticket", "")).strip()
    if not ticket:
        return

    existing = None
    for r in rows:
        if str(r.get("Ticket", "")) == ticket:
            existing = r
            break

    if existing is None:
        existing = {k: "" for k in CSV_HEADER}
        rows.append(existing)

    side = str(fields.get("Side", "")).strip().lower()
    if not side:
        tval = str(fields.get("Type", "")).strip().lower()
        bs_field = str(fields.get("Buy/sell", "")).strip().lower()
        if tval in ("buy", "sell"):
            side = tval
        elif bs_field in ("buy", "sell"):
            side = bs_field
    bs = "Buy" if side == "buy" else ("Sell" if side == "sell" else "")

    open_time = str(fields.get("Open time", "") or "").strip()
    open_date = _iso_to_date(open_time)

    lots_val = fields.get("Open lots", None)
    if lots_val in ("", None):
        lots_val = fields.get("Lots", None)
    if lots_val in ("", None):
        lots_val = fields.get("Size", None)

    tp_val = fields.get("tp", fields.get("TP", ""))
    sl_val = fields.get("sl", fields.get("SL", ""))

    existing.update({
        "Type": fields.get("Type", existing.get("Type", "")),
        "Ticket": ticket,
        "Symbol": fields.get("Symbol", existing.get("Symbol", "")),
        "Lots": lots_val if lots_val is not None else existing.get("Lots", ""),
        "Buy/sell": bs or existing.get("Buy/sell", ""),
        "Open price": fields.get("Open price", existing.get("Open price", "")),
        "Open time": open_time or existing.get("Open time", ""),
        "Open date": open_date or existing.get("Open date", ""),
        "T/P": tp_val if tp_val is not None else existing.get("T/P", ""),
        "S/L": sl_val if sl_val is not None else existing.get("S/L", ""),
        "Magic number": fields.get("Magic", existing.get("Magic number", "")),
        "Order comment": fields.get("Comment", existing.get("Order comment", "")),
        "Account": fields.get("Account", existing.get("Account", "")),
    })

    _write_rows(csv_path, rows)


# ────────────────────────────────────────────────────────────────────────────────
# CIERRES
# ────────────────────────────────────────────────────────────────────────────────

def update_close(csv_path: Path, ticket: str, close_fields: Dict[str, Any]) -> None:
    _ensure_csv(csv_path)
    rows = _read_rows(csv_path)

    ticket = str(ticket or "").strip()
    row = None
    for r in rows:
        if str(r.get("Ticket", "")) == ticket and ticket:
            row = r
            break

    if row is None:
        row = {k: "" for k in CSV_HEADER}
        row["Ticket"] = ticket
        rows.append(row)

    close_time = str(close_fields.get("Close time", "") or "").strip()
    close_date = _iso_to_date(close_time)
    open_time = row.get("Open time", "")
    dt_open = _parse_iso(open_time)
    dt_close = _parse_iso(close_time)
    duration_hours = ""
    if dt_open and dt_close:
        delta = dt_close - dt_open
        duration_hours = f"{delta.total_seconds()/3600.0:.2f}"

    try:
        lots = float(row.get("Lots") or close_fields.get("Lots") or close_fields.get("Open lots") or 0.0)
    except Exception:
        lots = 0.0

    try:
        open_price = float(row.get("Open price") or close_fields.get("Open price") or 0.0)
    except Exception:
        open_price = 0.0

    try:
        close_price = float(close_fields.get("Close price") or row.get("Close price") or 0.0)
    except Exception:
        close_price = 0.0

    gross = (close_price - open_price) * lots

    try:
        fee_open = float(close_fields.get("fee_open_usdt") or 0.0)
    except Exception:
        fee_open = 0.0
    try:
        fee_close = float(close_fields.get("Close fee USDT") or 0.0)
    except Exception:
        fee_close = 0.0

    commission = fee_open + fee_close
    swap = 0.0

    np_explicit = close_fields.get("Net profit", None)
    if np_explicit not in (None, ""):
        try:
            net_profit = float(np_explicit)
        except Exception:
            net_profit = gross - commission + swap
    else:
        net_profit = gross - commission + swap

    # ── ExitType / Order comment normalizado ──
    exit_type = str(close_fields.get("ExitType") or "").strip()
    comment = str(close_fields.get("Comment") or "").strip()
    if not exit_type:
        # si viene Order comment desde el engine, lo usamos como ExitType
        exit_type = str(close_fields.get("Order comment") or "").strip()
    order_comment = exit_type or comment or row.get("Order comment", "")

    result = close_fields.get("Result", row.get("Result", ""))
    if not result:
        if net_profit > 0:
            result = "win"
        elif net_profit < 0:
            result = "loss"
        else:
            result = ""

    row.update({
        "Close price": close_price or row.get("Close price", ""),
        "Close time": close_time or row.get("Close time", ""),
        "Close date": close_date or row.get("Close date", ""),
        "Profit": gross,
        "Swap": swap,
        "Commission": commission,
        "Net profit": net_profit,
        "Result": result,
        "Trade duration (hours)": duration_hours or row.get("Trade duration (hours)", ""),
        "Magic number": close_fields.get("Magic", row.get("Magic number", "")),
        "Order comment": order_comment,
        "Account": close_fields.get("Account", row.get("Account", "")),
    })

    _write_rows(csv_path, rows)
