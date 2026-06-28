# Monitor_Estrategias.py — Simple, TSV fijo con encabezado estándar y filas entrecomilladas
from __future__ import annotations
import argparse, os, json, platform, time, sys
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List

# ────────────────────────────────────────────────────────────────────────────────
# Consola segura
# ────────────────────────────────────────────────────────────────────────────────
def _stdout_enc() -> str:
    enc = sys.stdout.encoding or os.environ.get("PYTHONIOENCODING") or "ascii"
    return enc.lower()

def _supports_utf() -> bool:
    return "utf" in _stdout_enc()

if _supports_utf():
    H_THICK, H_LIGHT, V, SEP, DOT = "═", "─", "│", " │ ", "•"
else:
    H_THICK, H_LIGHT, V, SEP, DOT = "=", "-", "|", " | ", "*"

def _sanitize(s: str) -> str:
    if not isinstance(s, str): s = str(s)
    try:
        s.encode(_stdout_enc(), errors="strict")
        return s
    except Exception:
        return s.encode("ascii", errors="ignore").decode("ascii")

def _safe_print(line: str) -> None:
    try:
        print(_sanitize(line), flush=True)
    except Exception:
        try:
            sys.stdout.write(_sanitize(line) + "\n")
        except Exception:
            pass

def _clear():
    try:
        os.system("cls" if platform.system().lower().startswith("win") else "clear")
    except Exception:
        pass

# ────────────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────────────
def _num(x, default=None):
    try:
        if x is None: return default
        return float(x)
    except Exception:
        try:
            return float(str(x).replace(",", "").strip())
        except Exception:
            return default

def _iso_parse(ts: str | None):
    if not ts: return None
    s = ts
    if s.endswith("Z"): s = s[:-1]
    if s.endswith("+00:00"): s = s[:-6]
    try:
        return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
    except Exception:
        return None

def _dur_hours(open_iso: str | None, close_iso: str | None) -> float:
    a = _iso_parse(open_iso); b = _iso_parse(close_iso)
    if not a or not b: return 0.0
    return max(0.0, (b - a).total_seconds() / 3600.0)

def _fmt_thousands0(x) -> str:
    n = _num(x)
    if n is None: return "0"
    return f"{int(round(n)):,}"

def _fmt_price0(x) -> str:
    n = _num(x)
    if n is None: return ""
    return f"{int(round(n)):,}"

def _fmt_lots6(x) -> str:
    n = _num(x)
    return "" if n is None else f"{n:.6f}"

def _fmt_pl6(x) -> str:
    n = _num(x, 0.0)
    # Presentación: separador de miles + 2 decimales
    return f"{n:,.2f}"

def _fmt_hms_from_ts(ts) -> str:
    """Formatea timestamp (epoch s/ms o ISO8601) a HH:MM:SS UTC."""
    try:
        if ts is None or ts == "":
            return "--:--:--"

        # Si viene como ISO string: '2026-01-23T18:36:36Z'
        if isinstance(ts, str) and ("T" in ts):
            s = ts.strip()
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            dtv = datetime.fromisoformat(s)
            if dtv.tzinfo is None:
                dtv = dtv.replace(tzinfo=timezone.utc)
            return dtv.astimezone(timezone.utc).strftime("%H:%M:%S")

        v = int(float(ts))
        if v > 10_000_000_000:  # ms -> s
            v //= 1000
        return datetime.fromtimestamp(v, tz=timezone.utc).strftime("%H:%M:%S")
    except Exception:
        return "--:--:--"


def _fmt_hms_from_ts_offset(ts, offset_hours: int) -> str:
    """HH:MM:SS aplicando offset fijo (ej: OKX UTC+8)."""
    try:
        if ts is None or ts == "":
            return "--:--:--"
        # Reusar parser base para obtener dt en UTC
        if isinstance(ts, str) and ("T" in ts):
            s = ts.strip()
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            dtv = datetime.fromisoformat(s)
            if dtv.tzinfo is None:
                dtv = dtv.replace(tzinfo=timezone.utc)
            dt_utc = dtv.astimezone(timezone.utc)
        else:
            v = int(float(ts))
            if v > 10_000_000_000:
                v //= 1000
            dt_utc = datetime.fromtimestamp(v, tz=timezone.utc)
        dt_off = dt_utc + timedelta(hours=int(offset_hours))
        return dt_off.strftime("%H:%M:%S")
    except Exception:
        return "--:--:--"


def _get_broker_time_str(acct: Dict[str, Any]) -> str:
    """Devuelve HH:MM:SS (UTC) usando timestamp de OKX si existe."""
    if not acct:
        return "--:--:--"
    # Prioridad: epoch ms/s explícito -> ISO -> otros aliases
    for k in ("broker_ts_ms", "broker_ts", "server_ts", "ts", "time", "uTime"):
        if k in acct and acct.get(k) not in (None, ""):
            return _fmt_hms_from_ts(acct.get(k))
    return "--:--:--"

def _is_blank_dict(d: Dict[str, Any]) -> bool:
    if not d: return True
    for v in d.values():
        if isinstance(v, str):
            if v.strip(): return False
        elif v not in (None, ""):
            return False
    return True

# ────────────────────────────────────────────────────────────────────────────────
# IO (TSV fijo; soporta filas entrecomilladas y BOM)
# ────────────────────────────────────────────────────────────────────────────────
CSV_DELIM = "\t"  # SIEMPRE TAB

CSV_HEADER = [
    "Type","Ticket","Symbol","Lots","Buy/sell","Open price","Close price",
    "Open time","Close time","Open date","Close date","Profit","Swap",
    "Commission","Net profit","T/P","S/L","Pips","Result","Trade duration (hours)",
    "Magic number","Order comment","Account"
]

REQ_FIELDS = [
    "Close time","Ticket","Symbol","Lots","Buy/sell","Open price","Close price",
    "Net profit","Trade duration (hours)","Order comment"
]

def _read_json(path: Path) -> Dict[str, Any]:
    if not path.exists(): return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}

def _load_statuses(monitor_dir: Path) -> List[Dict[str, Any]]:
    out = []
    if not monitor_dir.exists(): return out
    for p in sorted(monitor_dir.glob("status_*.json")):
        try:
            obj = _read_json(p)
            if not isinstance(obj, dict) or not obj: continue
            try:
                magic = int(p.stem.split("_", 1)[1])
            except Exception:
                magic = obj.get("magic")
            obj.setdefault("magic", magic)
            out.append(obj)
        except Exception:
            continue
    return out

def _read_account(monitor_dir: Path) -> Dict[str, Any]:
    obj = _read_json(monitor_dir / "account.json")

    # Soportar formas viejas y nuevas de snapshot
    bal = _num(obj.get("balance_usdt"), None)
    if bal is None:
        bal = _num(obj.get("balance_total"), None)
    if bal is None:
        bal = _num(obj.get("Balance"), 0.0)

    eq = _num(obj.get("equity_total"), None)
    if eq is None:
        eq = _num(obj.get("equity_usdt"), None)
    if eq is None:
        eq = _num(obj.get("Equity"), 0.0)

    upl = _num(obj.get("upl_total"), None)
    if upl is None:
        upl = _num(obj.get("open_pl"), None)
    if upl is None:
        upl = _num(obj.get("UPL"), 0.0)

    # Hora broker: preferir epoch ms explícito (evita contaminaciones por strings).
    broker_ts = None
    broker_src = "-"
    for k in ("broker_ts_ms", "broker_ts", "server_ts", "time", "uTime"):
        if obj.get(k) not in (None, ""):
            broker_ts = obj.get(k)
            broker_src = k
            break
    # Fallback ISO (solo si parece ISO real)
    if broker_ts in (None, ""):
        ts_iso = obj.get("ts")
        if isinstance(ts_iso, str) and "T" in ts_iso:
            broker_ts = ts_iso
            broker_src = "ts"

    return {
        "balance_usdt": bal or 0.0,
        "equity_total": eq or 0.0,
        "upl_total": upl or 0.0,
        "account_name": obj.get("account_name") or obj.get("Account") or "OKX",
        "broker_ts": broker_ts,
        "broker_ts_source": broker_src,
    }

def _read_errors(monitor_dir: Path, limit: int = 5) -> List[Dict[str, Any]]:
    path = monitor_dir / "errors.log"
    if not path.exists(): return []
    out = []
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line: continue
            try:
                out.append(json.loads(line))
            except Exception:
                out.append({"raw": line})
    except Exception:
        pass
    return out[-limit:]

def _normalize_fieldnames(fieldnames: List[str]) -> List[str]:
    out = []
    for i, f in enumerate(fieldnames or []):
        s = (f or "").strip()
        if i == 0 and s.startswith("\ufeff"):
            s = s.lstrip("\ufeff")
        out.append(s)
    return out

def _split_tsv_line(line: str) -> List[str]:
    s = line.rstrip("\r\n")
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        s = s[1:-1]
    return s.split(CSV_DELIM)

def _read_closed_from_csv(root: Path) -> List[Dict[str, Any]]:
    csv_path = root / "trade_log.csv"
    if not csv_path.exists(): return []

    try:
        raw = csv_path.read_text(encoding="utf-8", errors="replace").splitlines()
        if not raw: return []
        header = _normalize_fieldnames(_split_tsv_line(raw[0]))
        if any(req not in header for req in REQ_FIELDS):
            return []

        idx = {name: pos for pos, name in enumerate(header)}
        rows: List[Dict[str, Any]] = []
        for line in raw[1:]:
            if not line or not line.strip():
                continue
            parts = _split_tsv_line(line)
            row = {name: (parts[idx[name]] if idx[name] < len(parts) else "") for name in header}
            if _is_blank_dict(row):
                continue
            if str(row.get("Close time","")).strip():
                rows.append(row)

        rows.sort(key=lambda rr: rr.get("Close time",""), reverse=True)
        return rows
    except Exception:
        return []

# ────────────────────────────────────────────────────────────────────────────────
# Tablas
# ────────────────────────────────────────────────────────────────────────────────
def _fit_cell(text: str, width: int, align: str) -> str:
    s = "" if text is None else str(text)
    if len(s) <= width: return s
    return s[:width] if align == 'l' else s[-width:]

def table_block(title: str, cols: list[tuple[str,int,str]], rows: list[list[str]]) -> list[str]:
    widths = [w for _, w, _ in cols]
    head = []
    for (h, w, a) in cols:
        hh = _fit_cell(h, w, a)
        head.append(f"{hh:<{w}s}" if a == 'l' else f"{hh:>{w}s}")
    total = sum(widths) + len(SEP) * (len(cols) - 1)
    line = H_LIGHT * total
    out = [title, line, SEP.join(head), line]
    if not rows:
        out.append("(sin datos)")
    else:
        for r in rows:
            cells = []
            for (c, (w, a)) in zip(r, [(w, a) for _, w, a in cols]):
                cc = _fit_cell(c, w, a)
                cells.append(f"{cc:<{w}s}" if a == 'l' else f"{cc:>{w}s}")
            out.append(SEP.join(cells))
    out.append(line)
    return out

# ────────────────────────────────────────────────────────────────────────────────
# Render
# ────────────────────────────────────────────────────────────────────────────────
def render(root: Path, closed_n=10, err_n=5) -> List[str]:
    mon = root / "monitor"

    statuses = _load_statuses(mon)
    acct = _read_account(mon)
    errs = _read_errors(mon, limit=err_n)

    # Leemos open_positions.json como fuente ÚNICA para P/L abierto
    open_pos = _read_json(mon / "open_positions.json")
    positions = open_pos.get("positions") or []

    totals_by_symbol: Dict[str, float] = {}
    upl_by_symbol: Dict[str, float] = {}
    upl_by_magic: Dict[int, float] = {}
    upl_total_calc = 0.0

    for p in positions:
        try:
            sym = p.get("symbol", "")
            if not sym:
                continue
            lots = _num(p.get("lots"), 0.0) or 0.0
            pl   = _num(p.get("open_pl"), 0.0) or 0.0
            mag_raw = p.get("magic")
            magic_i = int(mag_raw) if mag_raw not in (None, "") else None
        except Exception:
            continue

        totals_by_symbol[sym] = totals_by_symbol.get(sym, 0.0) + lots
        upl_by_symbol[sym]    = upl_by_symbol.get(sym, 0.0) + pl
        upl_total_calc       += pl
        if magic_i is not None:
            upl_by_magic[magic_i] = upl_by_magic.get(magic_i, 0.0) + pl

    out: List[str] = []
    out += [
        H_THICK * 100,
        "Terminal OKX - Demo / Cash Spot",
        H_THICK * 100,
        f"Cuenta: {acct.get('account_name') or 'OKX'}",
        # OKX reporta su hora de sistema en UTC+8; mostramos ambas para evitar
        # confusión con el huso del VPS.
        f"Hora OKX: {_get_broker_time_str(acct)} UTC | {_fmt_hms_from_ts_offset(acct.get('broker_ts'), 8)} UTC+8 ({acct.get('broker_ts_source','-')})",
        # Open P/L total = suma de todos los open_pl de las posiciones
        f"Saldo USDT: {_fmt_thousands0(acct['balance_usdt'])}   {V}   Patrimonio: {_fmt_thousands0(acct['equity_total'])}   {V}   Open P/L: {upl_total_calc:+.2f}",
        H_LIGHT * 100,
    ]

    # Lotes Abiertos: TOTAL LOTS + OPEN P/L por símbolo (coherente con header)
    out += [
        "Lotes Abiertos",
        H_LIGHT * 42,
        f"{'SYMBOL':<14}{SEP}{'TOTAL LOTS':>12}{SEP}{'OPEN P/L':>12}",
        H_LIGHT * 42,
    ]

    if totals_by_symbol:
        for sym in sorted(totals_by_symbol.keys()):
            lots = totals_by_symbol.get(sym, 0.0) or 0.0
            upl_sym = upl_by_symbol.get(sym, 0.0) or 0.0
            out.append(
                f"{sym:<14}{SEP}{_fmt_lots6(lots):>12}{SEP}{_fmt_pl6(upl_sym):>12}"
            )
    else:
        out.append("(sin posiciones abiertas)")
    out.append(H_LIGHT * 42)

    # Estrategias: OPEN P/L por estrategia desde upl_by_magic (no desde status_*.json)
    # Importante: NO usar hora del VPS. Si no hay timestamp de broker disponible,
    # dejamos now_iso vacío y la duración se mostrará en blanco.
    now_iso = (acct.get('ts') or open_pos.get('ts') or "")
    strat_rows: List[List[str]] = []
    for st in sorted(statuses, key=lambda x: (int(x.get("magic") or 0))):
        try:
            magic = st.get("magic", "")
            # Display consistente (preferir *_disp si existe)
            sym = (st.get("symbol_disp") or st.get("symbol") or "").strip()
            tf  = (st.get("tf_disp") or st.get("tf") or "-").strip()
            status_raw = (st.get("status") or "").strip().upper()
            data_ok = bool(st.get("data_ok", True))
            last_eval_ts = st.get("last_eval_ts")
            # WAITING solo antes de primera evaluación
            if status_raw in ("OPEN", "OPEN_ERROR", "DATA_ERROR", "ERROR"):
                status = status_raw
            elif not last_eval_ts:
                status = "WAITING"
            elif not data_ok:
                status = "DATA_ERROR"
            else:
                status = "FLAT"
            sl = _fmt_price0(st.get("sl"))
            tp = _fmt_price0(st.get("tp"))

            lots_val = _num(st.get("lots"), 0.0)

            try:
                magic_i = int(magic)
            except Exception:
                magic_i = None

            if status == "OPEN" and lots_val and lots_val != 0.0:
                open_lots = _fmt_lots6(lots_val)
                pl_val = upl_by_magic.get(magic_i, 0.0) if magic_i is not None else 0.0
                open_pl = _fmt_pl6(pl_val)
            else:
                # FLAT o sin lots: campos vacíos
                open_lots = ""
                open_pl   = ""

            open_time = st.get("open_time", "") or ""
            dur = f"{_dur_hours(open_time, now_iso):.2f}h" if (status == "OPEN" and now_iso) else ""
            strat_rows.append([ str(magic), sym, tf, status, sl, tp, open_lots, open_pl, open_time, dur ])
        except Exception:
            continue

    strat_cols = [
        ("MAGIC", 8, 'r'), ("SYMBOL", 10, 'l'), ("TF", 5, 'l'), ("STATUS", 7, 'l'),
        ("SL", 10, 'r'), ("TP", 10, 'r'), ("OPEN LOT", 11, 'r'), ("OPEN P/L", 11, 'r'),
        ("OPEN TIME", 19, 'l'), ("DURACION", 9, 'r')
    ]
    out += table_block("ESTRATEGIAS", strat_cols, strat_rows)

    # Cerradas desde CSV
    closed_all = _read_closed_from_csv(root)

    total_close_pl_hist = 0.0
    for r in closed_all:
        total_close_pl_hist += _num(r.get("Net profit"), 0.0) or 0.0

    out.append(f"Close P/L (total): {_fmt_thousands0(total_close_pl_hist)}")
    out.append(H_LIGHT * 100)

    closed = closed_all[:closed_n] if closed_n > 0 else closed_all
    closed_rows: List[List[str]] = []
    total_close_pl_shown = 0.0
    for r in closed:
        try:
            net = _num(r.get("Net profit"), 0.0) or 0.0
            total_close_pl_shown += net

            lots_val = r.get("Lots")
            if not lots_val:
                lots_val = r.get("Size")

            exit_comment = r.get("Order comment") or r.get("Comment", "")

            closed_rows.append([
                r.get("Close time",""),
                r.get("Ticket",""),
                r.get("Symbol",""),
                str(r.get("Magic number","")),
                (r.get("Buy/sell","")[:1].upper() or "B"),
                _fmt_lots6(lots_val),
                _fmt_price0(r.get("Open price")),
                _fmt_price0(r.get("Close price")),
                _fmt_thousands0(net),
                f"{_num(r.get('Trade duration (hours)'), 0.0) or 0.0:.2f}h",
                exit_comment,
            ])
        except Exception:
            continue

    closed_cols = [
        ("CLOSE TIME", 19, 'l'),
        ("TICKET", 19, 'l'),
        ("SYMBOL", 10, 'l'),
        ("MAGIC", 8, 'r'),
        ("S", 1, 'l'),
        ("LOTS", 11, 'r'),
        ("OPEN@PRICE", 11, 'r'),
        ("CLOSE@PRICE", 12, 'r'),
        ("NET", 10, 'r'),
        ("DUR", 7, 'r'),
        ("EXIT", 11, 'l'),
    ]
    closed_block = table_block("OPERACIONES CERRADAS", closed_cols, closed_rows)

    if closed_rows:
        widths = [w for _, w, _ in closed_cols]
        sep_len = len(SEP)
        net_idx = 8
        prefix_spaces = " " * (sum(widths[:net_idx]) + sep_len * net_idx)
        closed_block.insert(-1, f"{prefix_spaces}TOTAL CLOSE P/L (mostradas): {_fmt_thousands0(total_close_pl_shown)}")

    out += closed_block

    # Errores
    out += ["ERRORES", H_LIGHT * 100]
    errs = errs[-5:] if errs else []
    if not errs:
        out.append("  (sin errores)")
    else:
        for e in errs:
            if isinstance(e, dict):
                ts = e.get("ts", "")
                mod = e.get("module", "")
                mag = e.get("magic", "")
                msg = e.get("msg", "")
                out.append(f"  {DOT} {ts} | module={mod} | magic={mag} | {msg}")
            else:
                out.append(f"  {DOT} {e}")
    out.append(H_THICK * 100)
    return out

# ────────────────────────────────────────────────────────────────────────────────
# Main
# ────────────────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=str, default=os.getcwd(), help="Raíz del proyecto")
    ap.add_argument("--closed", type=int, default=10, help="Cantidad de cerradas a mostrar")
    ap.add_argument("--errors", type=int, default=5, help="Cantidad de errores a mostrar")
    ap.add_argument("--watch", type=int, default=15, help="Refrescar cada N segundos (0 = una vez)")
    args = ap.parse_args()

    root = Path(args.root)
    interval = max(0, int(args.watch or 0))
    if interval <= 0:
        for line in render(root, closed_n=args.closed, err_n=args.errors):
            _safe_print(line)
    else:
        while True:
            _clear()
            try:
                for line in render(root, closed_n=args.closed, err_n=args.errors):
                    _safe_print(line)
            except Exception as e:
                _safe_print(f"[MONITOR ERROR] {e}")
            time.sleep(interval)

if __name__ == "__main__":
    main()