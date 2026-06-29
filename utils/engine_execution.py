from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from datetime import datetime, timezone
import time, re, json, threading

from utils.common import write_json_atomic, append_jsonl, broker_now_iso
from utils.env_loader import env_get, env_float
from utils.logger import log_open, update_close
from utils.snapshots import write_account_and_positions


# ───────────────── Helpers internos ─────────────────

def _now_iso(client=None, monitor_dir: Path | None = None) -> str:
    # Hora del broker (NO usar reloj del VPS)
    return broker_now_iso(client=client, monitor_dir=monitor_dir)


def _safe_float(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except Exception:
        return float(default)


def _parse_pct(s: str) -> float:
    s = (s or "").strip()
    if s.endswith("%"):
        s = s[:-1]
    try:
        v = float(s)
    except Exception:
        return 0.0
    if v > 1.0:
        v /= 100.0
    return max(0.0, min(1.0, v))


def _fmt_sz(x: float) -> str:
    # Formateo “humano” (evitar notación científica)
    return f"{x:.10f}".rstrip("0").rstrip(".")


def _normalize_exit_type(exit_type: str) -> str:
    """
    Normaliza exit_type a uno de:
      - "SL"
      - "TP"
      - "Exit Signal"
    """
    s = (exit_type or "").strip().lower()
    if not s:
        return "Exit Signal"
    if "sl" in s or "stop" in s:
        return "SL"
    if "tp" in s or "take" in s or "profit" in s:
        return "TP"
    return "Exit Signal"


# ───────────────── Resultados públicos ─────────────────

@dataclass
class OpenResult:
    ok: bool
    ordId: Optional[str] = None
    error: Optional[str] = None


@dataclass
class CloseResult:
    ok: bool
    ordId: Optional[str] = None
    error: Optional[str] = None


# ───────────────── ExecutionEngine ─────────────────

class ExecutionEngine:
    """
    Motor de ejecución SPOT (cash) compartido por todas las estrategias.

    Responsabilidades:
      - Calcular tamaño de posición permitido (tope por exposición y riesgo).
      - Enviar órdenes spot a OKX (OKXClient.place_order).
      - Consolidar fills (OKXClient.get_fills) y comisiones.
      - Mantener consistencia de:
          * monitor/status_{MAGIC}.json
          * monitor/pos_{MAGIC}.json
          * monitor/open_positions.json (resumen global)
          * trade_log.csv (aperturas y cierres)
    """

    def __init__(
        self,
        client,
        monitor_dir: Path,
        trade_log: Path,
        account_name: str = "OKX",
        *,
        max_exposure_pct: Optional[float] = None,
    ) -> None:
        self.client = client
        self.monitor_dir = monitor_dir
        self.trade_log = trade_log
        self.account_name = account_name

        self._open_by_ticket: Dict[str, Dict[str, Any]] = {}
        self._open_by_magic: Dict[int, str] = {}

        if max_exposure_pct is not None:
            self.max_exposure_pct = max(0.0, min(1.0, float(max_exposure_pct)))
        else:
            self.max_exposure_pct = _parse_pct(env_get("MAX_EXPOSURE_PCT", "0.20"))
        self.usdt_haircut = env_float("USDT_HAIRCUT", 1.0)

        risk_env = env_get("MAX_RISK_PCT", env_get("RISK_PERCENT", None))
        if risk_env is not None:
            self.max_risk_pct = _parse_pct(risk_env)
        else:
            self.max_risk_pct = 0.0  # 0 => sin recorte adicional por riesgo

        self.monitor_dir.mkdir(parents=True, exist_ok=True)
        # Lock interno para proteger memoria + ficheros frente a múltiples hilos
        self._lock = threading.Lock()
        # Identificador único del boot (se usa para resetear estado persistido al reiniciar)
        self.boot_id = _now_iso(self.client, self.monitor_dir)

    # ────────── helpers de ficheros monitor/ ──────────

    def _pos_file(self, magic: int) -> Path:
        return self.monitor_dir / f"pos_{magic}.json"

    def _status_file(self, magic: int) -> Path:
        return self.monitor_dir / f"status_{magic}.json"

    def _read_pos(self, magic: int) -> Dict[str, Any]:
        p = self._pos_file(magic)
        if not p.exists():
            return {}
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _read_status(self, magic: int) -> Dict[str, Any]:
        p = self._status_file(magic)
        if not p.exists():
            return {}
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _write_pos(self, magic: int, obj: Dict[str, Any]) -> None:
        write_json_atomic(self._pos_file(magic), obj)

    def _write_status(self, magic: int, obj: Dict[str, Any]) -> None:
        prev = self._read_status(magic)
        if "tf" not in obj or not obj.get("tf"):
            obj["tf"] = prev.get("tf", obj.get("tf", ""))
        write_json_atomic(self._status_file(magic), obj)

    def _write_open_positions_snapshot(self) -> None:
        positions = []
        totals_by_symbol: Dict[str, float] = {}

        for ticket, rec in self._open_by_ticket.items():
            sym = rec.get("symbol", "")
            lots = float(rec.get("lots", 0.0) or 0.0)
            pos = {
                "ticket": ticket,
                "magic": rec.get("magic"),
                "symbol": sym,
                "side": rec.get("side", ""),
                "lots": lots,
                "open_price": rec.get("open_price"),
                "open_time": rec.get("open_time"),
                "sl": rec.get("sl"),
                "tp": rec.get("tp"),
            }
            positions.append(pos)
            if sym:
                totals_by_symbol[sym] = totals_by_symbol.get(sym, 0.0) + lots

        root = {
            "positions": positions,
            "totals_by_symbol": totals_by_symbol,
        }
        write_json_atomic(self.monitor_dir / "open_positions.json", root)

    # ────────── rehidratación desde monitor/ ──────────

    def rehydrate_from_files(self) -> int:
        """
        Rehidrata _open_by_ticket y _open_by_magic a partir de monitor/pos_*.json.
        Devuelve número de posiciones rehidratadas.
        """
        count = 0
        self._open_by_ticket.clear()
        self._open_by_magic.clear()
        for p in self.monitor_dir.glob("pos_*.json"):
            try:
                magic_str = re.findall(r"pos_(\d+)\.json", p.name)[0]
                magic = int(magic_str)
            except Exception:
                continue
            try:
                rec = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            ticket = str(rec.get("ticket") or "")
            if not ticket:
                continue
            self._open_by_ticket[ticket] = rec
            self._open_by_magic[magic] = ticket
            count += 1

        # Actualizar snapshot global para que el monitor parta coherente
        try:
            self._write_open_positions_snapshot()
        except Exception as e:
            append_jsonl(self.monitor_dir / "errors.log", {
                "ts": _now_iso(self.client, self.monitor_dir), "module": "engine", "msg": "rehydrate_open_positions_error", "error": str(e)
            })
        return count

    # ────────── helpers de equity / exposición ──────────

    def _get_equity_usdt(self) -> float:
        """
        Equity total en USDT (aprox) usando balances OKX.
        """
        bal = 0.0
        try:
            acc = self.client.get_account_balance()
            data = (acc.get("data") or [{}])[0]
            for d in data.get("details", []):
                ccy = (d.get("ccy") or "").upper()
                if ccy != "USDT":
                    continue
                bal = _safe_float(d.get("eq", d.get("cashBal", 0.0)))
                break
            if bal == 0.0:
                bal = _safe_float(data.get("totalEq"))
        except Exception:
            bal = 0.0
        return float(max(0.0, bal))

    def _max_usdt_allowed(self) -> float:
        bal = self._get_equity_usdt()
        return float(max(0.0, bal * self.max_exposure_pct * self.usdt_haircut))

    def get_equity_usdt(self) -> float:
        return self._get_equity_usdt()

    # ────────── helper para asegurar status inicial ──────────

    def ensure_status(self, magic: int, symbol: str, timeframe: str) -> None:
        st = self._read_status(magic)

        # Fuente de verdad para rehidratación al boot (sin tocar estrategias):
        # si existe monitor/pos_{MAGIC}.json, consideramos que hay operación abierta.
        # Esto evita que un reinicio del terminal (boot_id nuevo) "pise" el estado OPEN.
        pos = self._read_pos(magic)
        has_pos = bool(pos.get("ticket"))
        if has_pos:
            # Blindaje mínimo de campos esperados
            try:
                pos.setdefault("symbol", symbol)
            except Exception:
                pass

        if st:
            # 🔁 Si cambió el boot (reinicio del terminal)
            if st.get("boot_id") != self.boot_id:
                st["boot_id"] = self.boot_id

                if has_pos:
                    # ✅ Mantener OPEN si hay posición persistida
                    st["status"] = "OPEN"
                    st["lots"] = _safe_float(pos.get("lots", 0.0))
                    st["open_price"] = _safe_float(pos.get("open_price", 0.0))
                    st["open_time"] = pos.get("open_time")
                    st["sl"] = pos.get("sl")
                    st["tp"] = pos.get("tp")
                else:
                    # Sin posición persistida: estado limpio
                    st["status"] = "WAITING"
                    st["lots"] = 0.0
                    st["open_price"] = None
                    st["open_time"] = None
                    st["sl"] = None
                    st["tp"] = None

                # Diagnóstico / evaluación
                st["last_eval_ts"] = None
                st["eval_ok"] = None
                st["eval_err"] = ""
                st["data_ok"] = True

            # Blindaje de campos base
            st.setdefault("symbol", symbol)
            st.setdefault("tf", timeframe)
            st.setdefault("data_ok", True)
            st.setdefault("last_eval_ts", None)
            st.setdefault("eval_ok", None)
            st.setdefault("eval_err", "")
            st.setdefault("boot_id", self.boot_id)

        else:
            # Primera vez que se crea el status
            # Si hay pos persistida, partimos como OPEN para que la estrategia gestione.
            st = {
                "magic": magic,
                "symbol": symbol,
                "tf": timeframe,
                "boot_id": self.boot_id,

                "status": "OPEN" if has_pos else "WAITING",
                "lots": _safe_float(pos.get("lots", 0.0)) if has_pos else 0.0,
                "open_price": _safe_float(pos.get("open_price", 0.0)) if has_pos else None,
                "open_time": pos.get("open_time") if has_pos else None,
                "sl": pos.get("sl") if has_pos else None,
                "tp": pos.get("tp") if has_pos else None,

                # Diagnóstico
                "data_ok": True,
                "last_eval_ts": None,
                "eval_ok": None,
                "eval_err": "",
            }

        self._write_status(magic, st)


    # ────────── helpers para consolidar fills ──────────

    def _accum_fills_open(self, instId: str, ordId: str, retries: int = 6, sleep_s: float = 0.30) -> Tuple[float, float, float, float]:
        """
        Devuelve:
          base_qty, avg_px, fee_base_abs, fee_quote_usdt
        """
        base_qty = 0.0
        cost_usdt = 0.0
        fee_base_abs = 0.0
        fee_quote_usdt = 0.0

        for _ in range(max(1, retries)):
            try:
                fills = self.client.get_fills(instId=instId, ordId=ordId, limit=100).get("data", [])
                for f in fills:
                    if str(f.get("ordId")) != ordId:
                        continue
                    fillSz = _safe_float(f.get("fillSz", 0.0))
                    fillPx = _safe_float(f.get("fillPx", 0.0))
                    base_qty += fillSz
                    cost_usdt += fillSz * fillPx
                    fee = abs(_safe_float(f.get("fee", 0.0)))
                    feeCcy = (f.get("feeCcy") or "").upper()
                    if feeCcy == "USDT":
                        fee_quote_usdt += fee
                    else:
                        # Si la comisión viene en la base (BTC), la convertimos a USDT a precio medio
                        fee_base_abs += fee
                if base_qty > 0:
                    break
            except Exception as e:
                append_jsonl(self.monitor_dir / "errors.log", {
                    "ts": _now_iso(self.client, self.monitor_dir), "module": "engine", "msg": "fills_open_retry_error", "error": str(e)
                })
            time.sleep(sleep_s)
        avg_px = (cost_usdt / base_qty) if base_qty > 0 else 0.0
        return base_qty, avg_px, fee_base_abs, fee_quote_usdt

    def _accum_fills_close(self, instId: str, ordId: str, retries: int = 8, sleep_s: float = 0.30):
        qty_out = 0.0
        proceeds_usdt = 0.0
        fee_close_usdt = 0.0
        for _ in range(max(1, retries)):
            try:
                fills = self.client.get_fills(instId=instId, ordId=ordId, limit=100).get("data", [])
                for f in fills:
                    if str(f.get("ordId")) != ordId:
                        continue
                    fillSz = _safe_float(f.get("fillSz", 0.0))
                    fillPx = _safe_float(f.get("fillPx", 0.0))
                    qty_out += fillSz
                    proceeds_usdt += fillSz * fillPx
                    fee = abs(_safe_float(f.get("fee", 0.0)))
                    feeCcy = (f.get("feeCcy") or "").upper()
                    if feeCcy == "USDT":
                        fee_close_usdt += fee
                    else:
                        fee_close_usdt += fee * fillPx
                if qty_out > 0:
                    break
            except Exception as e:
                append_jsonl(self.monitor_dir / "errors.log", {
                    "ts": _now_iso(self.client, self.monitor_dir), "module": "engine", "msg": "fills_close_retry_error", "error": str(e)
                })
            time.sleep(sleep_s)
        avg_close = (proceeds_usdt / qty_out) if qty_out > 0 else 0.0
        return qty_out, proceeds_usdt, fee_close_usdt, avg_close

    # ────────── API pública: aperturas ──────────

    def process_open(
        self,
        *,
        magic: int,
        symbol: str,
        side: str,
        est_lots: Optional[float] = None,
        lots: Optional[float] = None,
        sl: Optional[float],
        tp: Optional[float],
    ) -> OpenResult:
        side = side.lower()
        assert side in ("buy", "sell"), "Only buy/sell supported"
        if side != "buy":
            return OpenResult(ok=False, error="Only BUY supported for spot opens")

        try:
            magic_i = int(magic)
        except Exception:
            return OpenResult(ok=False, error="Invalid magic")

        # Estrategia puede pasar lots o est_lots (estimado). Preferimos lots si viene.
        est_lots_val = None
        if lots is not None:
            est_lots_val = float(lots)
        elif est_lots is not None:
            est_lots_val = float(est_lots)
        else:
            return OpenResult(ok=False, error="No size provided")

        if est_lots_val <= 0:
            return OpenResult(ok=False, error="Invalid size")

        # Leer tf existente para no pisarlo con string vacío
        _prev_tf = self._read_status(magic_i).get("tf", "")
        self.ensure_status(magic_i, symbol, timeframe=_prev_tf)

        # Ticker para precio actual
        try:
            ticker = self.client.get_ticker(symbol)
            last = _safe_float((ticker.get("data") or [{}])[0].get("last", 0.0))
        except Exception as e:
            append_jsonl(self.monitor_dir / "errors.log", {
                "ts": _now_iso(self.client, self.monitor_dir), "module": "engine", "msg": "ticker_error", "error": str(e), "symbol": symbol
            })
            last = 0.0

        if last <= 0:
            return OpenResult(ok=False, error="Invalid last price")

        # ───── Límite por riesgo (MAX_RISK_PCT) ─────
        if self.max_risk_pct > 0 and sl is not None and sl > 0 and sl < last:
            # Riesgo aproximado en USDT = (last - sl) * est_lots
            risk_usdt = (last - sl) * est_lots_val
            equity = self._get_equity_usdt()
            max_risk_usdt = equity * self.max_risk_pct
            if risk_usdt > max_risk_usdt > 0:
                # Recorte proporcional del lote pedido por la estrategia
                factor = max_risk_usdt / risk_usdt
                new_lots = est_lots_val * factor
                print(f"[RISK][OPEN] Recorte lote por riesgo: {est_lots_val:.8f} -> {new_lots:.8f} (equity={equity:.2f} max_risk={max_risk_usdt:.2f})", flush=True)
                est_lots_val = new_lots
                if est_lots_val <= 0:
                    return OpenResult(ok=False, error="Risk limit shrank size to zero")

        # ───── Límite por exposición (MAX_EXPOSURE_PCT + haircut) ─────
        desired_usdt = est_lots_val * last
        max_usdt_allowed = self._max_usdt_allowed()
        if desired_usdt > max_usdt_allowed > 0:
            print(f"[EXPO][OPEN] Recorte por exposición: desired={desired_usdt:.2f} max_allowed={max_usdt_allowed:.2f}", flush=True)
            desired_usdt = max_usdt_allowed

        if desired_usdt <= 0:
            return OpenResult(ok=False, error="Exposure limit shrank notional to zero")

        usdt_to_spend = desired_usdt
        # Identificador local: NO depende del reloj del VPS (usa monotonic)
        clOrdId = f"c{magic_i}{time.monotonic_ns()}"
        print(f"[ORDER][OPEN][send] {symbol} usdt={usdt_to_spend:.6f} clOrdId={clOrdId}", flush=True)

        try:
            res = self.client.place_order(
                instId=symbol,
                side="buy",
                tdMode="cash",
                sz=_fmt_sz(usdt_to_spend),   # en modo cash con tgtCcy="quote_ccy" es tamaño en USDT
                tgtCcy="quote_ccy",
                ordType="market",
                clOrdId=clOrdId,
            )
            data = (res.get("data") or [{}])[0]
            ordId = str(data.get("ordId"))
        except Exception as e:
            append_jsonl(self.monitor_dir / "errors.log", {
                "ts": _now_iso(self.client, self.monitor_dir), "module": "engine", "msg": "open_order_failed",
                "error": str(e), "clOrdId": clOrdId, "symbol": symbol, "usdt": usdt_to_spend
            })
            try:
                prev_status = self._read_status(int(magic)) or {}
                status = {
                    "magic": int(magic),
                    "symbol": symbol,
                    "tf": prev_status.get("tf", prev_status.get("tf_disp", "")),
                    "status": "OPEN_ERROR",
                    "lots": 0.0,
                    "data_ok": True,
                    "last_eval_ts": _now_iso(self.client, self.monitor_dir),
                    "last_error": str(e),
                }
                self._write_status(int(magic), status)
            except Exception:
                pass
            return OpenResult(ok=False, error=str(e))

        base_gross, avg_px, fee_base_abs, fee_quote_usdt = self._accum_fills_open(symbol, ordId, retries=6, sleep_s=0.30)
        if base_gross <= 0:
            # Fills no confirmados tras todos los reintentos.
            # NO registrar posición con 0 lotes — dejaría estado corrupto.
            print(f"[ORDER][OPEN][fills] WARN fills_not_confirmed ordId={ordId} — posición NO registrada", flush=True)
            append_jsonl(self.monitor_dir / "errors.log", {
                "ts": _now_iso(self.client, self.monitor_dir), "module": "engine",
                "msg": "fills_not_confirmed", "ordId": ordId, "symbol": symbol,
            })
            return OpenResult(ok=False, error="fills_not_confirmed")
        fee_open_usdt = fee_quote_usdt + fee_base_abs * (avg_px if avg_px > 0 else last)
        print(f"[ORDER][OPEN][fills] gross={base_gross:.10f} avg_px={avg_px:.6f} fee_usdt={fee_open_usdt:.8f}", flush=True)

        # Zona crítica: memoria + JSON + CSV protegidos por lock
        with self._lock:
            ticket = ordId
            rec = {
                "ticket": ticket,
                "magic": int(magic),
                "symbol": symbol,
                "side": "buy",
                "lots": base_gross,
                "open_time": _now_iso(self.client, self.monitor_dir),
                "open_price": avg_px,
                "open_usdt": base_gross * avg_px,
                "fee_open_usdt": fee_open_usdt,
                "sl": float(sl) if sl is not None else None,
                "tp": float(tp) if tp is not None else None,
            }
            self._open_by_ticket[ticket] = rec
            self._open_by_magic[int(magic)] = ticket
            self._write_pos(int(magic), rec)

            prev_status = self._read_status(int(magic))
            status = {
                "magic": int(magic),
                "symbol": symbol,
                "tf": prev_status.get("tf", ""),
                "status": "OPEN",
                "lots": base_gross,
                "open_price": avg_px,
                "open_time": rec["open_time"],
                "sl": rec["sl"],
                "tp": rec["tp"],
            }
            self._write_status(int(magic), status)

            log_open(
                self.trade_log,
                {
                    "Ticket": ticket,
                    "Open time": rec["open_time"],
                    "Type": "BUY",
                    "Size": base_gross,
                    "Symbol": symbol,
                    "Open price": avg_px,
                    "SL": rec["sl"],
                    "TP": rec["tp"],
                    "Comment": "",
                    "Magic": magic,
                    "Account": self.account_name,
                },
            )

            try:
                self._write_open_positions_snapshot()
            except Exception as e:
                append_jsonl(self.monitor_dir / "errors.log", {
                    "ts": _now_iso(self.client, self.monitor_dir), "module": "engine", "msg": "open_positions_snapshot_error", "error": str(e)
                })
            try:
                write_account_and_positions(self.client, self.monitor_dir, auth_ok=True)
            except Exception as e:
                append_jsonl(self.monitor_dir / "errors.log", {
                    "ts": _now_iso(self.client, self.monitor_dir), "module": "engine", "msg": "account_snapshot_error", "error": str(e)
                })

            return OpenResult(ok=True, ordId=ordId)

    # ────────── API pública: cierres ──────────

    def process_close(
        self,
        *,
        magic: int,
        ticket: Optional[str] = None,
        exit_type: str = "Exit Signal",
    ) -> CloseResult:
        try:
            magic_i = int(magic)
        except Exception:
            return CloseResult(ok=False, error="Invalid magic")

        if ticket:
            pos = self._open_by_ticket.get(ticket) or {}
        else:
            t = self._open_by_magic.get(magic_i)
            pos = self._open_by_ticket.get(t) if t else {}
        if not pos:
            return CloseResult(ok=False, error="No open position for magic/ticket")

        symbol = pos["symbol"]
        qty = float(pos["lots"])
        if qty <= 0:
            # Posición con lotes inválidos — limpiar estado y memoria para no quedar bloqueado en OPEN.
            print(f"[ORDER][CLOSE][WARN] lots=0 para magic={magic_i} — limpiando estado a FLAT sin enviar orden", flush=True)
            with self._lock:
                ticket_bad = pos.get("ticket", "")
                self._open_by_ticket.pop(ticket_bad, None)
                self._open_by_magic.pop(magic_i, None)
                p = self._pos_file(magic_i)
                if p.exists():
                    p.unlink()
                prev_st = self._read_status(magic_i)
                self._write_status(magic_i, {
                    "magic": magic_i,
                    "symbol": symbol,
                    "tf": prev_st.get("tf", ""),
                    "status": "FLAT",
                    "lots": 0.0,
                })
            return CloseResult(ok=False, error="invalid_lot_size")

        # Normalizamos exit_type a SL / TP / Exit Signal
        exit_type_norm = _normalize_exit_type(exit_type)

        clOrdId = self._gen_clordid(magic_i)
        print(f"[ORDER][CLOSE][send] {symbol} qty={qty:.10f} clOrdId={clOrdId}", flush=True)
        try:
            res = self.client.place_order(
                instId=symbol,
                side="sell",
                tdMode="cash",
                sz=_fmt_sz(qty),
                ordType="market",
                clOrdId=clOrdId,
            )
            data = (res.get("data") or [{}])[0]
            ordId = str(data.get("ordId"))
        except Exception as e:
            append_jsonl(self.monitor_dir / "errors.log", {
                "ts": _now_iso(self.client, self.monitor_dir), "module": "engine", "msg": "close_order_failed",
                "error": str(e), "clOrdId": clOrdId, "symbol": symbol, "qty": qty
            })
            return CloseResult(ok=False, error=str(e))

        qty_out, proceeds_usdt, fee_close_usdt, avg_close = self._accum_fills_close(symbol, ordId, retries=8, sleep_s=0.30)
        if qty_out <= 0:
            avg_close = 0.0
        print(f"[ORDER][CLOSE][fills] qty_out={qty_out:.10f} avg_close={avg_close:.6f} fee_usdt={fee_close_usdt:.8f}", flush=True)

        # Zona crítica: memoria + JSON + CSV protegidos por lock
        with self._lock:
            ticket = pos["ticket"]
            pos["close_time"] = _now_iso(self.client, self.monitor_dir)
            pos["close_price"] = avg_close
            pos["close_usdt"] = proceeds_usdt
            pos["fee_close_usdt"] = fee_close_usdt
            pos["exit_type"] = exit_type_norm

            pnl_usdt = proceeds_usdt - pos["open_usdt"] - pos["fee_open_usdt"] - fee_close_usdt
            pos["pnl_usdt"] = pnl_usdt

            self._open_by_ticket.pop(ticket, None)
            self._open_by_magic.pop(magic_i, None)
            p = self._pos_file(magic_i)
            if p.exists():
                p.unlink()

            prev_status = self._read_status(magic_i)
            status = {
                "magic": magic_i,
                "symbol": symbol,
                "tf": prev_status.get("tf", ""),
                "status": "FLAT",
                "lots": 0.0,
            }
            self._write_status(magic_i, status)

            update_close(
                self.trade_log,
                ticket,
                {
                    "Close time": pos["close_time"],
                    "Close price": avg_close,
                    "Net profit": pnl_usdt,
                    "Magic": magic_i,
                    "Order comment": exit_type_norm,
                    "Account": self.account_name,
                    "fee_open_usdt": pos.get("fee_open_usdt", 0.0),
                    "Close fee USDT": fee_close_usdt,
                },
            )

            try:
                self._write_open_positions_snapshot()
            except Exception as e:
                append_jsonl(self.monitor_dir / "errors.log", {
                    "ts": _now_iso(self.client, self.monitor_dir), "module": "engine", "msg": "open_positions_snapshot_error", "error": str(e)
                })
            try:
                write_account_and_positions(self.client, self.monitor_dir, auth_ok=True)
            except Exception as e:
                append_jsonl(self.monitor_dir / "errors.log", {
                    "ts": _now_iso(self.client, self.monitor_dir), "module": "engine", "msg": "account_snapshot_error", "error": str(e)
                })

            return CloseResult(ok=True, ordId=ordId)

    # ────────── helpers varios ──────────

    def _gen_clordid(self, magic: int) -> str:
        # NO depende del reloj del VPS (usa monotonic)
        return f"c{magic}{time.monotonic_ns()}"


# ───────────────── Singleton compartido ─────────────────

_shared_engine: Optional[ExecutionEngine] = None
_shared_lock = threading.Lock()


def init_shared_engine(
    client,
    monitor_dir: Path,
    trade_log: Path,
    account_name: str,
    max_exposure_pct: Optional[float] = None,
) -> ExecutionEngine:
    global _shared_engine
    with _shared_lock:
        if _shared_engine is None:
            _shared_engine = ExecutionEngine(
                client=client,
                monitor_dir=monitor_dir,
                trade_log=trade_log,
                account_name=account_name,
                max_exposure_pct=max_exposure_pct,
            )
            try:
                n = _shared_engine.rehydrate_from_files()
                print(f"[ENGINE] Rehidratadas {n} posiciones previas", flush=True)
            except Exception:
                pass
    return _shared_engine


def get_shared_engine() -> ExecutionEngine:
    if _shared_engine is None:
        raise RuntimeError("ExecutionEngine no inicializado (llama init_shared_engine primero).")
    return _shared_engine