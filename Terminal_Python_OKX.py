from __future__ import annotations
import sys, os, time, threading, importlib.util
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from utils.env_loader import load_api_env, env_bool
from utils.okx_client import OKXClient
from utils.engine_execution import ExecutionEngine
from utils.snapshots import write_account_and_positions

ROOT = Path(__file__).resolve().parent
MONITOR_DIR = ROOT / "monitor"
STRAT_DIR = ROOT / "strategies"
TRADE_LOG = ROOT / "trade_log.csv"

def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00","Z")

def _print(*a, **k): print(*a, **k, flush=True)

def _tf_to_bar(tf: str) -> str:
    tf = str(tf).lower().strip()
    if tf.endswith("m"): return tf
    if tf.endswith("h"): return tf.replace("h","H")
    if tf in ("1d","d1","1day"): return "1D"
    raise ValueError(f"Unsupported timeframe: {tf}")

def _tf_minutes(tf: str) -> int:
    tf = str(tf).lower().strip()
    if tf.endswith("m"): return int(tf[:-1])
    if tf.endswith("h"): return int(tf[:-1]) * 60
    if tf in ("1d","d1","1day"): return 1440
    raise ValueError(f"Unsupported timeframe: {tf}")

def _floor_to_tf(dt_utc: datetime, tf_min: int) -> datetime:
    mins = int((dt_utc.minute // tf_min) * tf_min)
    return dt_utc.replace(second=0, microsecond=0, minute=mins)

def _load_strategy_module(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)  # type: ignore
    return mod

class StrategyThread(threading.Thread):
    def __init__(self, engine: ExecutionEngine, mod, verbose: bool):
        super().__init__(daemon=True)
        self.engine = engine
        self.mod = mod
        self.verbose = verbose
        self.magic = int(getattr(mod, "MAGIC"))
        self.symbol = str(getattr(mod, "SYMBOL"))
        self.tf = str(getattr(mod, "TIMEFRAME"))
        self.tf_min = _tf_minutes(self.tf)
        self.stop_flag = False
        self.last_poll = 0.0
        self.last_candle_close_ts = None

    def stop(self): self.stop_flag = True

    def _get_candles(self, limit: int = 200):
        bar = _tf_to_bar(self.tf)
        return self.engine.client.get_candles(self.symbol, bar=bar, limit=limit).get("data", [])

    def _latest_price(self) -> Optional[float]:
        try:
            t = self.engine.client.get_ticker(self.symbol)
            d = (t.get("data") or [{}])[0]
            return float(d.get("last", d.get("lastPx", 0.0)))
        except Exception:
            return None

    def _write_status(self):
        rec = self.engine.get_open_rec(self.magic)
        if rec:
            last = self._latest_price() or rec.get("open_price")
            open_pl = (float(last) - float(rec.get("open_price", 0))) * float(rec.get("lots", 0))
            st = {
                "magic": self.magic, "symbol": self.symbol, "tf": self.tf, "status": "OPEN",
                "lots": rec.get("lots", 0.0), "open_price": rec.get("open_price"),
                "tp": rec.get("tp"), "sl": rec.get("sl"), "open_time": rec.get("open_time"),
                "open_pl": open_pl, "est_lots": rec.get("lots", 0.0)
            }
        else:
            st = {
                "magic": self.magic, "symbol": self.symbol, "tf": self.tf, "status": "FLAT",
                "lots": 0.0, "open_price": None, "tp": None, "sl": None,
                "open_time": None, "open_pl": 0.0, "est_lots": 0.0
            }
        try:
            self.engine._write_status(self.magic, st)
        except Exception:
            pass

    def _on_candle_close(self):
        has_pos = self.engine.has_open_for_magic(self.magic)
        candles = self._get_candles(limit=200)
        if not candles or len(candles) < 2:
            return
        try:
            dec = self.mod.run(candles, has_pos)
        except Exception as e:
            if self.verbose:
                print(f"[{self.magic}|{self.symbol}] strategy error: {e}", flush=True)
            return
        if not isinstance(dec, dict):
            return

        action = (dec.get("action") or "NONE").upper()
        if action == "OPEN" and not has_pos:
            side = (dec.get("side") or "buy").lower()
            est_lots = float(dec.get("est_lots", 0.0) or 0.0)
            sl = dec.get("sl")
            tp = dec.get("tp")
            if est_lots > 0:
                if self.verbose:
                    print(f"[EVAL][{self.magic}|{self.symbol}|{self.tf}] OPEN side={side} est_lots={est_lots} sl={sl} tp={tp}", flush=True)
                self.engine.process_open(magic=self.magic, symbol=self.symbol, side=side, est_lots=est_lots, sl=sl, tp=tp)
        elif action == "CLOSE" and has_pos:
            rec = self.engine.get_open_rec(self.magic)
            if rec:
                if self.verbose:
                    print(f"[EVAL][{self.magic}|{self.symbol}|{self.tf}] CLOSE by signal", flush=True)
                self.engine.process_close(ticket=str(rec["ticket"]), magic=self.magic, exit_type="Exit Signal")

    def run(self):
        self._write_status()
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        self.last_candle_close_ts = int(_floor_to_tf(now, self.tf_min).timestamp())
        while not self.stop_flag:
            try:
                # SL/TP polling cada 5 s cuando hay posición
                rec = self.engine.get_open_rec(self.magic)
                if rec:
                    if time.time() - self.last_poll >= 5.0:
                        self.last_poll = time.time()
                        last = self._latest_price()
                        if last is not None:
                            tp = rec.get("tp")
                            sl = rec.get("sl")
                            side = (rec.get("side") or "").lower()
                            exit_type = None
                            if side == "buy":
                                if tp and last >= float(tp):
                                    exit_type = "TP"
                                elif sl and last <= float(sl):
                                    exit_type = "SL"
                            if exit_type:
                                if self.verbose:
                                    print(f"[SLTP][{self.magic}|{self.symbol}] hit {exit_type}! last={last} tp={tp} sl={sl}", flush=True)
                                self.engine.process_close(ticket=str(rec["ticket"]), magic=self.magic, exit_type=exit_type)
                # refresco status cada 5 s
                if time.time() - self.last_poll >= 5.0:
                    self._write_status()

                # al cierre de TF, evaluar señal
                now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
                close_ts = int(_floor_to_tf(now, self.tf_min).timestamp())
                if close_ts != self.last_candle_close_ts:
                    self.last_candle_close_ts = close_ts
                    self._on_candle_close()
                time.sleep(0.25)
            except Exception as e:
                if self.verbose:
                    _print(f"[{self.magic}|{self.symbol}] loop error: {e}")
                time.sleep(1.0)

def main():
    _print("╔══════════════════════════════════════════════════════╗")
    _print("║            Terminal OKX - Demo / Cash Spot           ║")
    _print("╚══════════════════════════════════════════════════════╝")
    load_api_env(ROOT)
    api_key = os.environ.get("API_KEY", "")
    api_secret = os.environ.get("API_SECRET", "")
    api_pass = os.environ.get("API_PASSPHRASE", "")
    base_url = os.environ.get("BASE_URL", "https://www.okx.com")
    simulated = os.environ.get("SIMULATED_TRADING", "1") in ("1", "true", "TRUE", "on", "ON")
    verbose = env_bool("VERBOSE", True)
    http_debug = env_bool("HTTP_DEBUG", False)
    acct_name = os.environ.get("ACCOUNT_NAME", "OKX Demo")

    client = OKXClient(api_key, api_secret, api_pass, base_url=base_url, simulated=simulated, http_debug=http_debug)
    engine = ExecutionEngine(client, MONITOR_DIR, TRADE_LOG, account_name=acct_name)
    reh = engine.rehydrate_from_files()
    _print(f"[BOOT] Rehidratadas {reh} posiciones abiertas desde /monitor")

    try:
        write_account_and_positions(client, MONITOR_DIR, auth_ok=True)
    except Exception as e:
        if verbose:
            _print(f"[WARN] snapshot: {e}")

    strategies: List[Dict[str, Any]] = []
    for p in sorted(STRAT_DIR.glob("*.py")):
        if p.name.startswith("_"):
            continue
        try:
            mod = _load_strategy_module(p)
            MAGIC = int(getattr(mod, "MAGIC"))
            SYMBOL = str(getattr(mod, "SYMBOL"))
            TF = str(getattr(mod, "TIMEFRAME"))
            strategies.append({"mod": mod, "path": p, "magic": MAGIC, "symbol": SYMBOL, "tf": TF})
            _print(f"[LOAD OK] {p.name} | MAGIC={MAGIC} | {SYMBOL} | TF={TF}")
        except Exception as e:
            _print(f"[LOAD ERR] {p.name} -> {e}")

    if not strategies:
        _print("[BOOT] No se encontraron estrategias en /strategies")
        return

    threads: List[threading.Thread] = []
    for s in strategies:
        th = StrategyThread(engine, s["mod"], verbose=verbose)
        th.start()
        threads.append(th)
    _print(f"[BOOT] Estrategias cargadas: {len(threads)}")

    try:
        cycle = 0
        while True:
            cycle += 1
            if verbose and cycle % 10 == 0:
                alive = sum(1 for t in threads if t.is_alive())
                _print(f"[{_now_iso()}] ciclo={cycle} | estrategias={len(threads)} | hilos_vivos={alive}")
            # snapshot cuenta cada ~20s
            if cycle % 80 == 0:
                try:
                    write_account_and_positions(client, MONITOR_DIR, auth_ok=True)
                except Exception:
                    pass
            time.sleep(0.25)
    except KeyboardInterrupt:
        _print("\n[shutdown] Deteniendo hilos…")
        for t in threads:
            if hasattr(t, "stop"):
                t.stop()
        for t in threads:
            t.join(timeout=3.0)
        _print("[shutdown] Ok. Saliendo.")

if __name__ == "__main__":
    main()
