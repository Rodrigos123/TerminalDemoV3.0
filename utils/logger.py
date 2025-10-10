from __future__ import annotations
from pathlib import Path
from typing import Dict, Any, List
import csv
from datetime import datetime

# Encabezado EXACTO (orden y nombres) que solicitaste
CSV_HEADER: List[str] = [
    "Type","Ticket","Symbol","Lots","Buy/sell","Open price","Close price",
    "Open time","Close time","Open date","Close date","Profit","Swap","Commission",
    "Net profit","T/P","S/L","Pips","Result","Trade duration (hours)","Magic number","Order comment","Account"
]

DELIM = "\t"

def _ensure_csv(path: Path) -> None:
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="") as f:
            csv.writer(f, delimiter=DELIM).writerow(CSV_HEADER)

def _iso_to_date(s: str) -> str:
    try:
        if not s:
            return ""
        ss = s
        if ss.endswith("Z"):
            ss = ss[:-1]
        if ss.endswith("+00:00"):
            ss = ss[:-6]
        dt = datetime.fromisoformat(ss)
        return dt.date().isoformat()
    except Exception:
        return s.split("T")[0] if "T" in s else s

def _fmt_buy_sell(side: str) -> str:
    s = (side or "").strip().lower()
    if s == "buy": return "Buy"
    if s == "sell": return "Sell"
    return ""

def _sanitize_row(row: Dict[str, Any]) -> Dict[str, Any]:
    # Devuelve SOLO las columnas del header en el orden exacto
    return {k: (row.get(k, "")) for k in CSV_HEADER}

def _is_blank_row(row: Dict[str, Any]) -> bool:
    # Considera “en blanco” si todos los campos están vacíos/espacios
    if not row: return True
    for k in CSV_HEADER:
        v = row.get(k, "")
        if isinstance(v, str):
            if v.strip() != "":
                return False
        elif v not in (None, ""):
            return False
    return True

def _compact_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    # Elimina filas completamente en blanco
    return [r for r in rows if not _is_blank_row(r)]

def log_open(csv_path: Path, row: Dict[str, Any]) -> None:
    """
    Escribe APERTURA con encabezado estándar TSV.
    - Nunca escribe filas en blanco (si faltara Ticket o Symbol, no escribe).
    """
    _ensure_csv(csv_path)

    ticket = str(row.get("Ticket", "")).strip()
    symbol = str(row.get("Symbol", "")).strip()
    if not ticket or not symbol:
        return  # evita filas “vacías”

    lots = row.get("Open lots", row.get("Lots", ""))
    open_time = row.get("Open time", "")
    open_date = _iso_to_date(open_time)

    mapped = {
        "Type": row.get("Type", "DEMO"),
        "Ticket": ticket,
        "Symbol": symbol,
        "Lots": lots,
        "Buy/sell": _fmt_buy_sell(row.get("Side", "")),
        "Open price": row.get("Open price", ""),
        "Close price": "",
        "Open time": open_time,
        "Close time": "",
        "Open date": open_date,
        "Close date": "",
        "Profit": "",
        "Swap": "",
        "Commission": "",
        "Net profit": "",
        "T/P": row.get("tp", ""),
        "S/L": row.get("sl", ""),
        "Pips": "",
        "Result": "",
        "Trade duration (hours)": "",
        "Magic number": row.get("Magic", ""),
        "Order comment": row.get("Comment", ""),  # OPEN
        "Account": row.get("Account", ""),
    }

    with csv_path.open("a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_HEADER, delimiter=DELIM)
        w.writerow(_sanitize_row(mapped))

def update_close(csv_path: Path, ticket: str, close_fields: Dict[str, Any]) -> None:
    """
    Actualiza CIERRE:
      - Remueve filas en blanco existentes.
      - Calcula Profit (bruto), Commission, Net profit.
      - Calcula Trade duration (hours).
      - "Order comment" := ExitType ("Exit Signal" | "SL" | "TP") si viene; si no, se mantiene o "CLOSE".
    """
    _ensure_csv(csv_path)

    # Preparar datos de cierre
    close_time = str(close_fields.get("Close time", "")).strip()
    close_date = _iso_to_date(close_time)
    lots = close_fields.get("Open lots", close_fields.get("Lots", 0.0))
    try: lots = float(lots)
    except Exception: lots = 0.0

    open_price = float(close_fields.get("Open price", 0.0) or 0.0)
    close_price = float(close_fields.get("Close price", 0.0) or 0.0)

    fee_open = float(close_fields.get("fee_open_usdt", 0.0) or 0.0)
    fee_close = float(close_fields.get("Close fee USDT", 0.0) or 0.0)
    commission = fee_open + fee_close

    gross = close_fields.get("Gross USDT", None)
    if gross is None:
        gross = (close_price - open_price) * lots

    net = close_fields.get("Net USDT", None)
    if net is None:
        net = gross - commission

    result = close_fields.get("Result", "")
    tp = close_fields.get("tp", close_fields.get("T/P", ""))
    sl = close_fields.get("sl", close_fields.get("S/L", ""))
    exit_type = close_fields.get("ExitType", close_fields.get("Order comment", "CLOSE"))

    # Leer todo y normalizar (saltando filas en blanco)
    rows: List[Dict[str, Any]] = []
    found = False
    with csv_path.open("r", encoding="utf-8", newline="") as f:
        r = csv.DictReader(f, delimiter=DELIM)
        for row in r:
            # DictReader puede devolver dicts con None si la línea está vacía
            if row is None:
                continue
            row = _sanitize_row(row)
            if _is_blank_row(row):
                continue
            if (row.get("Ticket") == str(ticket)) and not row.get("Close time"):
                # completar campos de cierre
                row["Close price"] = f"{close_price}"
                row["Close time"] = close_time
                row["Close date"] = close_date
                row["Profit"] = f"{gross}"
                row["Swap"] = ""
                row["Commission"] = f"{commission}"
                row["Net profit"] = f"{net}"
                row["T/P"] = f"{tp}"
                row["S/L"] = f"{sl}"
                row["Pips"] = row.get("Pips","")
                row["Result"] = result
                # Order comment = tipo de salida
                row["Order comment"] = str(exit_type)
                # Duración en horas
                try:
                    ot = row.get("Open time","")
                    ss_o = ot[:-1] if ot.endswith("Z") else ot
                    if ss_o.endswith("+00:00"): ss_o = ss_o[:-6]
                    dt_o = datetime.fromisoformat(ss_o)
                    ss_c = close_time[:-1] if close_time.endswith("Z") else close_time
                    if ss_c.endswith("+00:00"): ss_c = ss_c[:-6]
                    dt_c = datetime.fromisoformat(ss_c)
                    hours = (dt_c - dt_o).total_seconds() / 3600.0
                    row["Trade duration (hours)"] = f"{hours:.2f}"
                except Exception:
                    pass
                found = True
            rows.append(row)

    if not found:
        # Caso borde: si no estaba la fila de apertura, escribimos una línea válida (no en blanco)
        mapped = {k:"" for k in CSV_HEADER}
        mapped.update({
            "Type": close_fields.get("Type","DEMO"),
            "Ticket": str(ticket),
            "Symbol": close_fields.get("Symbol",""),
            "Lots": f"{lots}",
            "Buy/sell": "Buy",
            "Open price": f"{open_price}",
            "Close price": f"{close_price}",
            "Open time": close_fields.get("Open time",""),
            "Close time": close_time,
            "Open date": _iso_to_date(close_fields.get("Open time","")),
            "Close date": close_date,
            "Profit": f"{gross}",
            "Swap": "",
            "Commission": f"{commission}",
            "Net profit": f"{net}",
            "T/P": f"{tp}",
            "S/L": f"{sl}",
            "Pips": "",
            "Result": result,
            "Trade duration (hours)": "",
            "Magic number": close_fields.get("Magic",""),
            "Order comment": str(exit_type),
            "Account": close_fields.get("Account",""),
        })
        if not _is_blank_row(mapped):
            rows.append(_sanitize_row(mapped))

    # Compactar filas (elimino posibles en blanco)
    rows = _compact_rows(rows)

    with csv_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_HEADER, delimiter=DELIM)
        w.writeheader()
        for r in rows:
            if not _is_blank_row(r):
                w.writerow(_sanitize_row(r))
