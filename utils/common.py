from __future__ import annotations
import json, time, threading
from pathlib import Path
from typing import Any, Dict

_file_lock = threading.Lock()

def now_ms() -> int:
    """Reloj local (VPS) SOLO para timeouts internos.

    Prohibido usarlo para trading, logs, monitor o timestamps de datos.
    """
    return int(time.time() * 1000)

def write_json_atomic(path: Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with _file_lock:
        tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

def read_json(path: Path, default: Dict[str, Any] | None = None) -> Dict[str, Any]:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {} if default is None else default

def append_jsonl(path: Path, record: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False)
    with _file_lock:
        with path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")


# ────────────────────────────────────────────────────────────────────────────────
# Broker time helpers (NO usar hora local del VPS para trading/logs/monitor)
# ────────────────────────────────────────────────────────────────────────────────
from datetime import datetime, timezone
from typing import Optional, Iterable, Tuple

def _ms_to_iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

def broker_now_ms(*, client=None, monitor_dir: Optional[Path] = None, fallback_symbols: Optional[Iterable[str]] = None) -> Optional[int]:
    """Devuelve timestamp del broker (OKX) en ms.

    Regla: NUNCA caer al reloj del VPS para trading/monitor/logs.
    Orden de prioridad:
      1) Última vela M1 disponible en caché (hora de mercado/datos)
      2) Timestamp del ticker (servidor OKX)
      3) Snapshot monitor/account.json (si fue escrito con hora broker)
      4) None
    """

    # 0) Preferir hora desde datos (última vela M1 en caché)
    try:
        from utils.okx_data_cache import get_shared_cache
        cache = get_shared_cache()
        syms = list(fallback_symbols or [])
        for s in ("BTC-USDT", "ETH-USDT"):
            if s not in syms:
                syms.append(s)
        for sym in syms:
            try:
                m1 = cache.get_candles(sym, "1m", 1)
                if m1:
                    ts_open = int(m1[-1][0])
                    # "hora actual" = cierre de la última vela M1
                    return ts_open + 60_000
            except Exception:
                continue
    except Exception:
        pass
    # 1) Preferir endpoint de tiempo si existe
    if client is not None:
        # método opcional
        for meth in ("get_server_time_ms", "get_server_time"):
            if hasattr(client, meth):
                try:
                    v = getattr(client, meth)()
                    if isinstance(v, dict):
                        # {data:[{ts:'...'}]} o similar
                        d0 = (v.get("data") or [{}])[0]
                        ts = d0.get("ts") or d0.get("timestamp") or d0.get("serverTime")
                        if ts is not None:
                            ms = int(float(ts))
                            if ms < 10_000_000_000:
                                ms *= 1000
                            return ms
                    else:
                        ms = int(float(v))
                        if ms < 10_000_000_000:
                            ms *= 1000
                        return ms
                except Exception:
                    pass

        # 2) Fallback: ticker ts (viene del servidor)
        syms = list(fallback_symbols or [])
        for s in ("BTC-USDT", "ETH-USDT"):
            if s not in syms:
                syms.append(s)
        for sym in syms:
            try:
                t = client.get_ticker(sym)
                d0 = (t.get("data") or [{}])[0]
                ts = d0.get("ts") or d0.get("timestamp")
                if ts is None:
                    continue
                ms = int(float(ts))
                if ms < 10_000_000_000:
                    ms *= 1000
                return ms
            except Exception:
                continue

    # 3) Leer snapshot local (escrito desde broker)
    if monitor_dir is not None:
        try:
            acct = read_json(Path(monitor_dir) / "account.json", default={})
            for k in ("broker_ts_ms", "broker_ts", "server_ts"):
                if acct.get(k) not in (None, ""):
                    ms = int(float(acct.get(k)))
                    if ms < 10_000_000_000:
                        ms *= 1000
                    return ms
            ts = acct.get("ts")
            if ts:
                s = str(ts).strip()
                if s.endswith("Z"):
                    s = s[:-1] + "+00:00"
                dtv = datetime.fromisoformat(s)
                if dtv.tzinfo is None:
                    dtv = dtv.replace(tzinfo=timezone.utc)
                return int(dtv.timestamp() * 1000)
        except Exception:
            pass

    return None

def broker_now_iso(*, client=None, monitor_dir: Optional[Path] = None, fallback_symbols: Optional[Iterable[str]] = None) -> str:
    """ISO8601 (Z) usando hora del broker.

    Importante: NO hay fallback al reloj local del VPS.
    Si no hay hora broker disponible aún, devuelve "".
    """
    ms = broker_now_ms(client=client, monitor_dir=monitor_dir, fallback_symbols=fallback_symbols)
    if ms is None:
        return ""
    return _ms_to_iso(ms)
