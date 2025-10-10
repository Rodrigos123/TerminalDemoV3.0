from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, List
from datetime import datetime, timezone
import time, re, json

from utils.common import write_json_atomic, append_jsonl
from utils.env_loader import env_get, env_float
from utils.logger import log_open, update_close

def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

def _safe_float(x, default=0.0) -> float:
    try: return float(x)
    except Exception: return float(default)

def _parse_pct(x) -> float:
    s = str(x or "").strip()
    if s.endswith("%"):
        try: return float(s[:-1]) / 100.0
        except Exception: return 0.0
    try:
        v = float(s)
        v = v / 100.0 if v > 1.0 else v
        return max(0.0, min(1.0, v))
    except Exception:
        return 0.0

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

class ExecutionEngine:
    def __init__(self, client, monitor_dir: Path, trade_log: Path, account_name: str = "OKX") -> None:
        self.client = client
        self.monitor_dir = monitor_dir
        self.trade_log = trade_log
        self.account_name = account_name
        self._open_by_ticket: Dict[str, Dict[str, Any]] = {}
        self._open_by_magic: Dict[int, str] = {}

        self.max_exposure_pct = _parse_pct(env_get("MAX_EXPOSURE_PCT", "0.20"))
        self.usdt_haircut = env_float("USDT_HAIRCUT", 1.0)

        self.monitor_dir.mkdir(parents=True, exist_ok=True)

    # ---------- persistencia ----------
    def _pos_file(self, magic: int) -> Path:
        return self.monitor_dir / f"pos_{magic}.json"

    def _status_file(self, magic: int) -> Path:
        return self.monitor_dir / f"status_{magic}.json"

    def _read_status(self, magic: int) -> Dict[str, Any]:
        p = self._status_file(magic)
        if p.exists():
            try: return json.loads(p.read_text(encoding="utf-8"))
            except Exception: return {}
        return {}

    def _save_pos(self, magic: int, rec: Dict[str, Any]) -> None:
        write_json_atomic(self._pos_file(magic), rec)

    def _remove_pos(self, magic: int) -> None:
        p = self._pos_file(magic)
        if p.exists(): p.unlink()

    def _write_status(self, magic: int, obj: Dict[str, Any]) -> None:
        prev = self._read_status(magic)
        # preservar TF si ya existe
        if "tf" not in obj or not obj.get("tf"):
            obj["tf"] = prev.get("tf", obj.get("tf", ""))
        # preservar info útil anterior que no estemos seteando ahora
        if "last_close" not in obj and "last_close" in prev:
            obj["last_close"] = prev["last_close"]
        write_json_atomic(self._status_file(magic), obj)

    def rehydrate_from_files(self) -> int:
        count = 0
        for p in self.monitor_dir.glob("pos_*.json"):
            try:
                rec = json.loads(p.read_text(encoding="utf-8"))
                ticket = str(rec["ticket"])
                self._open_by_ticket[ticket] = rec
                self._open_by_magic[int(rec["magic"])] = ticket
                count += 1
            except Exception:
                continue
        # dejamos de escribir open_positions.json para simplificar
        return count

    # ---------- helpers ----------
    def _max_usdt_allowed(self) -> float:
        try:
            acc = self.client.get_account_balance("USDT")
            data = (acc.get("data") or [{}])[0]
            bal = 0.0
            for d in (data.get("details") or []):
                if (d.get("ccy") or "").upper() == "USDT":
                    bal = _safe_float(d.get("cashBal", 0.0)); break
            if bal == 0.0: bal = _safe_float(data.get("totalEq"))
        except Exception:
            bal = 0.0
        return float(max(0.0, bal * self.max_exposure_pct * self.usdt_haircut))

    # ---------- clOrdId robusto ----------
    def _gen_clordid(self, magic: int) -> str:
        import uuid
        ms = int(time.time()*1000) % (10**11)
        rnd = uuid.uuid4().hex[:6]
        raw = f"c{int(magic)}{ms}{rnd}"
        return re.sub(r'[^A-Za-z0-9]', '', raw)[:32]

    def _accum_fills(self, instId: str, ordId: str, retries: int = 5, sleep_s: float = 0.25):
        base_qty = 0.0; cost_usdt = 0.0; fee_base = 0.0
        for _ in range(max(1, retries)):
            try:
                fills = self.client.get_fills(instId=instId, ordId=ordId, limit=100).get("data", [])
                for f in fills:
                    if str(f.get("ordId")) != ordId: continue
                    fillSz = float(f.get("fillSz", 0.0)); fillPx = float(f.get("fillPx", 0.0))
                    base_qty += fillSz; cost_usdt += fillSz * fillPx; fee_base += float(f.get("fee", 0.0))
                if base_qty > 0: break
            except Exception as e:
                append_jsonl(self.monitor_dir / "errors.log", {"ts": _now_iso(), "module": "engine", "msg": "fills_retry_error", "error": str(e)})
            time.sleep(sleep_s)
        avg_px = (cost_usdt / base_qty) if base_qty > 0 else 0.0
        return base_qty, avg_px, fee_base

    def _accum_close_fills(self, instId: str, ordId: str, retries: int = 8, sleep_s: float = 0.30):
        qty_out = 0.0; proceeds_usdt = 0.0; fee_close_usdt = 0.0
        for _ in range(max(1, retries)):
            try:
                fills = self.client.get_fills(instId=instId, ordId=ordId, limit=100).get("data", [])
                for f in fills:
                    if str(f.get("ordId")) != ordId: continue
                    fillSz = float(f.get("fillSz", 0.0)); fillPx = float(f.get("fillPx", 0.0))
                    qty_out += fillSz; proceeds_usdt += fillSz * fillPx
                    fee = abs(float(f.get("fee", 0.0))); feeCcy = (f.get("feeCcy") or "").upper()
                    fee_close_usdt += fee if feeCcy == "USDT" else fee * fillPx
                if qty_out > 0: break
            except Exception as e:
                append_jsonl(self.monitor_dir / "errors.log", {"ts": _now_iso(), "module": "engine", "msg": "fills_retry_error_close", "error": str(e)})
            time.sleep(sleep_s)
        avg_close = (proceeds_usdt / qty_out) if qty_out > 0 else 0.0
        return qty_out, proceeds_usdt, fee_close_usdt, avg_close

    # ---------- API pública ----------
    def has_open_for_magic(self, magic: int) -> bool:
        return magic in self._open_by_magic

    def get_open_rec(self, magic: int) -> Optional[Dict[str, Any]]:
        t = self._open_by_magic.get(magic)
        return self._open_by_ticket.get(t) if t else None

    def process_open(self, *, magic: int, symbol: str, side: str, est_lots: float, sl: Optional[float], tp: Optional[float]) -> OpenResult:
        side = side.lower()
        assert side in ("buy","sell"), "Only buy/sell supported"
        if side != "buy":
            return OpenResult(ok=False, error="Only BUY supported for spot opens")

        last = None
        try:
            t = self.client.get_ticker(symbol); d = (t.get("data") or [{}])[0]
            last = float(d.get("last", d.get("lastPx", 0.0)))
        except Exception: pass
        if not last or last <= 0:
            return OpenResult(ok=False, error="No market price")

        desired_usdt = float(est_lots) * last
        max_allowed = self._max_usdt_allowed()
        usdt_to_spend = min(desired_usdt, max_allowed)
        if usdt_to_spend <= 0:
            return OpenResult(ok=False, error="Exposure=0")

        clOrdId = self._gen_clordid(magic)
        print(f"[ORDER][OPEN][send] {symbol} usdt={usdt_to_spend:.6f} clOrdId={clOrdId}", flush=True)
        try:
            res = self.client.place_order(instId=symbol, side="buy", sz=str(usdt_to_spend),
                                          ordType="market", tdMode="cash", tgtCcy="quote_ccy", clOrdId=clOrdId)
            ordId = str((res.get("data") or [{}])[0].get("ordId"))
            if not ordId: raise Exception("Empty ordId in OKX response")
            print(f"[ORDER][OPEN][ok] ordId={ordId}", flush=True)
        except Exception as e:
            print(f"[ORDER][OPEN][err] {e}", flush=True)
            append_jsonl(self.monitor_dir / "errors.log", {"ts": _now_iso(), "module": "engine", "msg": "open_order_failed",
                                                           "error": str(e), "clOrdId": clOrdId, "symbol": symbol, "usdt": usdt_to_spend})
            return OpenResult(ok=False, error=str(e))

        base_qty, avg_px, fee_open_base = self._accum_fills(symbol, ordId, retries=5, sleep_s=0.25)
        if base_qty <= 0: avg_px = last
        fee_open_usdt = abs(fee_open_base) * (avg_px if avg_px > 0 else last)
        print(f"[ORDER][OPEN][fills] lots={base_qty:.10f} avg_px={avg_px:.6f} fee_usdt={fee_open_usdt:.8f}", flush=True)

        ticket = ordId
        rec = {
            "ticket": ticket, "magic": magic, "symbol": symbol, "side": "buy",
            "lots": round(base_qty, 10), "open_price": avg_px, "open_time": _now_iso(),
            "tp": tp, "sl": sl, "clOrdId": clOrdId, "ordId": ordId,
            "fee_open_usdt": fee_open_usdt, "status": "OPEN",
        }
        self._open_by_ticket[ticket] = rec
        self._open_by_magic[magic] = ticket
        self._save_pos(magic, rec)

        # status: preservar tf si ya existe (lo pone el hilo)
        prev = self._read_status(magic)
        st = {
            "magic": magic, "symbol": symbol, "tf": prev.get("tf",""),
            "status": "OPEN", "lots": rec["lots"], "open_price": rec["open_price"],
            "tp": tp, "sl": sl, "open_time": rec["open_time"], "open_pl": 0.0, "est_lots": rec["lots"]
        }
        self._write_status(magic, st)

        log_open(self.trade_log, {
            "Type": "DEMO", "Ticket": ticket, "Symbol": symbol, "Side": "buy",
            "Open lots": rec["lots"], "Open price": rec["open_price"], "Open time": rec["open_time"],
            "tp": tp, "sl": sl, "Magic": magic, "Comment": "OPEN", "Account": self.account_name
        })
        return OpenResult(ok=True, ordId=ordId)

    def process_close(self, *, ticket: str, magic: int, exit_type: str | None = None) -> CloseResult:
        rec = self._open_by_ticket.get(str(ticket))
        if not rec:
            t = self._open_by_magic.get(int(magic))
            if not t: return CloseResult(ok=False, error="No open position")
            rec = self._open_by_ticket.get(t)
            if not rec: return CloseResult(ok=False, error="No open position")

        symbol = rec["symbol"]
        base_qty = float(rec.get("lots", 0.0))

        # rehidratar si lots quedó 0
        if base_qty <= 0 and rec.get("ordId"):
            b, avg_px_open, _ = self._accum_fills(symbol, rec["ordId"], retries=5, sleep_s=0.25)
            if b > 0:
                base_qty = b; rec["lots"] = round(b, 10)
                if not rec.get("open_price") and avg_px_open > 0: rec["open_price"] = avg_px_open
                self._save_pos(int(rec["magic"]), rec)

        if base_qty <= 0:
            err = "Invalid size (lots=0); cannot close"
            print(f"[ORDER][CLOSE][skip] {err}", flush=True)
            append_jsonl(self.monitor_dir / "errors.log", {"ts": _now_iso(), "module": "engine", "msg": "close_skip_invalid_size", "error": err, "magic": magic})
            return CloseResult(ok=False, error=err)

        print(f"[ORDER][CLOSE][send] {symbol} lots={base_qty:.10f}", flush=True)
        try:
            res = self.client.place_order(instId=symbol, side="sell", sz=str(base_qty), ordType="market", tdMode="cash")
            ordId = str((res.get("data") or [{}])[0].get("ordId"))
            if not ordId: raise Exception("Empty ordId in OKX response (close)")
            print(f"[ORDER][CLOSE][ok] ordId={ordId}", flush=True)
        except Exception as e:
            print(f"[ORDER][CLOSE][err] {e}", flush=True)
            append_jsonl(self.monitor_dir / "errors.log", {"ts": _now_iso(), "module": "engine", "msg": "close_order_failed", "error": str(e)})
            return CloseResult(ok=False, error=str(e))

        qty_out, proceeds_usdt, fee_close_usdt, avg_close = self._accum_close_fills(symbol, ordId, retries=8, sleep_s=0.30)
        if qty_out <= 0:
            time.sleep(0.35)
            qty_out, proceeds_usdt, fee_close_usdt, avg_close = self._accum_close_fills(symbol, ordId, retries=4, sleep_s=0.30)
        if qty_out <= 0 and rec.get("open_price"):
            avg_close = float(rec["open_price"])

        net = (avg_close - float(rec.get("open_price", 0.0))) * float(rec.get("lots", 0.0)) - (float(rec.get("fee_open_usdt",0.0)) + fee_close_usdt)
        print(f"[ORDER][CLOSE][fills] lots={qty_out:.10f} avg_px={avg_close:.6f} fee_usdt={fee_close_usdt:.8f} net={net:.6f}", flush=True)

        gross = (avg_close - float(rec.get("open_price", 0.0))) * float(rec.get("lots", 0.0))
        result = "win" if net >= 0 else "loss"

        update_close(self.trade_log, rec["ticket"], {
            "Type": "DEMO", "Ticket": rec["ticket"], "Symbol": rec["symbol"], "Side": "buy",
            "Open lots": rec["lots"], "Open price": rec.get("open_price"), "Open time": rec.get("open_time"),
            "Close price": avg_close, "Close time": _now_iso(),
            "Close fee USDT": round(fee_close_usdt, 8), "fee_open_usdt": float(rec.get("fee_open_usdt",0.0)),
            "Gross USDT": gross, "Net USDT": round(net, 8), "Result": result, "Magic": magic,
            "Comment": "CLOSE", "ExitType": exit_type or "CLOSE", "Account": self.account_name,
            "tp": rec.get("tp"), "sl": rec.get("sl"),
        })

        # estado final + resumen de cierre dentro del status_{magic}.json
        prev = self._read_status(int(rec["magic"]))
        st = {
            "magic": rec["magic"], "symbol": rec["symbol"], "tf": prev.get("tf",""),
            "status": "FLAT", "lots": 0.0, "open_price": None, "tp": None, "sl": None,
            "open_time": None, "open_pl": 0.0, "est_lots": 0.0,
            "last_close": {
                "ticket": rec["ticket"], "ordId": ordId, "symbol": rec["symbol"],
                "lots": round(qty_out, 10), "open_price": rec.get("open_price"),
                "close_price": avg_close, "open_time": rec.get("open_time"),
                "close_time": _now_iso(), "net": round(net, 8), "result": result,
                "exit_type": exit_type or "CLOSE"
            }
        }
        self._write_status(int(rec["magic"]), st)

        # limpiar posición rehidratable
        self._open_by_ticket.pop(rec["ticket"], None)
        self._open_by_magic.pop(int(rec["magic"]), None)
        self._remove_pos(int(rec["magic"]))
        return CloseResult(ok=True, ordId=ordId)
