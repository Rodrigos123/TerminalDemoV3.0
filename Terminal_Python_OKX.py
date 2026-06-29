# -*- coding: utf-8 -*-
from __future__ import annotations
import os, sys, time, threading, traceback, signal, importlib.util, ast
from dataclasses import dataclass
from typing import List, Optional, Any, Set, Tuple
from pathlib import Path

from utils.data_feed import DataFeed
from utils.data_ingestor import DataIngestor
from utils.ws_router import WsRouter
from utils.okx_ws import OkxPrivateWS
from utils.order_executor import init_shared_executor
from utils.engine_execution import init_shared_engine, get_shared_engine
from utils.snapshots import write_account_and_positions
from utils.common import read_json, broker_now_iso

from datetime import datetime, timezone

def _now_iso() -> str:
    """ISO8601 (Z) usando hora del broker (snapshot en monitor/account.json)."""
    monitor_dir = Path(BASE_DIR) / "monitor"
    return broker_now_iso(monitor_dir=monitor_dir)


TERMINAL_TITLE = "Terminal OKX - Demo / Cash Spot"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DEFAULT_STRATEGIES_DIR = os.path.join(BASE_DIR, "strategies")
DEFAULT_ENV_PATHS = [
    os.path.join(BASE_DIR, ".env"),
    os.path.join(BASE_DIR, "API_KEYs.env"),
]

GLOBAL_VERBOSE = 0  # se setea tras leer env


def _print(msg: str, *, verbose_only: bool = False) -> None:
    if verbose_only and GLOBAL_VERBOSE != 1:
        return
    print(msg, flush=True)


def _pick_env_path() -> str:
    for p in DEFAULT_ENV_PATHS:
        if os.path.isfile(p):
            return p
    return os.path.join(BASE_DIR, ".env")


def load_env(env_path: str) -> dict:
    vals = {}
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                raw = line.strip()
                if not raw or raw.startswith("#"):
                    continue
                if "=" in raw:
                    k, v = raw.split("=", 1)
                    vals[k.strip()] = v.strip()
    return vals


def resolve_strategies_dir(env: dict) -> str:
    if os.path.isdir(DEFAULT_STRATEGIES_DIR):
        return DEFAULT_STRATEGIES_DIR
    return os.path.abspath(os.path.join(os.getcwd(), "strategies"))


@dataclass
class StrategySpec:
    name: str
    module_path: str
    symbol: str
    timeframe: str
    magic: Optional[int] = None


def _list_strategy_files(dir_path: str) -> List[str]:
    if not os.path.isdir(dir_path):
        return []
    return sorted(
        [
            f
            for f in os.listdir(dir_path)
            if f.endswith(".py") and not f.startswith("__")
        ]
    )



# ----------------------------
# Lectura de metadata desde el CÓDIGO (sin ejecutar el módulo)
# ----------------------------

_ALLOWED_TFS = {"1m", "5m", "15m", "1h", "4h", "1d"}

_TF_ALIAS = {
    "m1": "1m",
    "m5": "5m",
    "m15": "15m",
    "h1": "1h",
    "h4": "4h",
    "d1": "1d",
    "1min": "1m",
    "5min": "5m",
    "15min": "15m",
    "1hour": "1h",
    "4hour": "4h",
    "1day": "1d",
}

def _normalize_tf_if_needed(tf: str) -> str:
    """Normaliza TF sólo si viene en formato 'H1/H4/D1/M1...' o similares.
    Si ya viene normalizado (1h/4h/1d/1m/...), se devuelve tal cual.
    """
    if not tf:
        return tf
    raw = str(tf).strip()
    low = raw.lower()
    if low in _ALLOWED_TFS:
        return low
    # ejemplos: 'H1' -> '1h', 'H4' -> '4h'
    low = low.replace(" ", "")
    if low in _TF_ALIAS:
        return _TF_ALIAS[low]
    return low  # último recurso

def _normalize_symbol(symbol: str) -> str:
    """Normaliza símbolo a formato OKX tipo 'BTC-USDT'.
    Si ya viene con '-', se respeta.
    """
    if not symbol:
        return symbol
    s = str(symbol).strip().upper().replace(" ", "")
    if "-" in s:
        return s
    # Heurística: QUOTE común al final
    for quote in ("USDT", "USDC", "USD", "BTC", "ETH"):
        if s.endswith(quote) and len(s) > len(quote):
            base = s[: -len(quote)]
            return f"{base}-{quote}"
    return s

def _extract_strategy_metadata_from_code(module_path: str) -> dict:
    """Extrae MAGIC_NUMBER / SYMBOL / TIMEFRAME (TF) desde el archivo .py
    usando AST (no ejecuta código).
    """
    meta = {}
    try:
        with open(module_path, "r", encoding="utf-8") as f:
            src = f.read()
        tree = ast.parse(src, filename=module_path)
    except Exception:
        return meta

    def _const_value(node):
        # Python 3.8+: ast.Constant
        if isinstance(node, ast.Constant):
            return node.value
        # compat: ast.Str / ast.Num (por si acaso)
        if isinstance(node, ast.Str):
            return node.s
        if isinstance(node, ast.Num):
            return node.n
        return None

    wanted = {
        "MAGIC_NUMBER": "magic",
        "MAGIC": "magic",
        "SYMBOL": "symbol",
        "TIMEFRAME": "timeframe",
        "TF": "timeframe",
    }

    for n in tree.body:
        # x = <const>
        if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
            name = n.targets[0].id
            if name in wanted:
                v = _const_value(n.value)
                if v is not None:
                    meta[wanted[name]] = v
        # x: type = <const>
        if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            name = n.target.id
            if name in wanted:
                v = _const_value(n.value) if n.value is not None else None
                if v is not None:
                    meta[wanted[name]] = v

    return meta



def discover_strategy_modules(dir_path: str) -> List[StrategySpec]:
    """Descubre estrategias leyendo MAGIC/SYMBOL/TF desde el CÓDIGO.

    - Preferimos variables globales en el .py: MAGIC_NUMBER, SYMBOL, TIMEFRAME/TF.
    - Si falta algo, hacemos fallback al nombre de archivo (compatibilidad).
    - TF ya viene normalizado en estrategias nuevas; sólo normalizamos si detectamos formato legacy.
    """
    specs: List[StrategySpec] = []
    if not os.path.isdir(dir_path):
        return specs

    for fname in os.listdir(dir_path):
        if not fname.endswith(".py") or fname.startswith("__"):
            continue

        module_path = os.path.join(dir_path, fname)
        base = os.path.splitext(fname)[0]

        magic = None
        symbol = "BTC-USDT"
        tf = "1m"

        # 1) Leer desde el código (sin ejecutar)
        meta = _extract_strategy_metadata_from_code(module_path)
        if "magic" in meta:
            try:
                magic = int(meta["magic"])
            except Exception:
                pass
        if "symbol" in meta and meta["symbol"]:
            symbol = str(meta["symbol"])
        if "timeframe" in meta and meta["timeframe"]:
            tf = str(meta["timeframe"])

        # 2) Fallback al nombre de archivo (legacy)
        if magic is None or not symbol or not tf:
            parts = base.split("_")
            try:
                if len(parts) >= 3:
                    if magic is None:
                        magic = int(parts[0])
                    if (not symbol) or symbol == "BTC-USDT":
                        symbol = parts[1]
                    if (not tf) or tf == "1m":
                        tf = parts[2]
            except Exception:
                pass

        # 3) Normalización
        symbol = _normalize_symbol(symbol)
        # Sólo normalizar TF si no está en formato aceptado
        tf_norm = _normalize_tf_if_needed(tf)
        tf = tf_norm if (str(tf).strip().lower() not in _ALLOWED_TFS) else str(tf).strip().lower()

        specs.append(
            StrategySpec(
                name=base,
                module_path=module_path,
                symbol=symbol,
                timeframe=tf,
                magic=magic,
            )
        )
    return specs


def import_strategy_module(spec: StrategySpec):
    spec_obj = importlib.util.spec_from_file_location(spec.name, spec.module_path)
    if spec_obj is None or spec_obj.loader is None:
        raise RuntimeError(f"No se pudo crear spec para {spec.module_path}")
    module = importlib.util.module_from_spec(spec_obj)
    sys.modules[spec.name] = module
    spec_obj.loader.exec_module(module)
    return module


def create_okx_client(env: dict) -> Any:
    try:
        from utils.okx_client import OKXClient
    except Exception as e:
        raise RuntimeError(f"No se pudo importar utils.okx_client.OKXClient: {e}")

    api_key = env.get("API_KEY")
    api_secret = env.get("API_SECRET")
    passphrase = env.get("API_PASSPHRASE")
    simulated = str(env.get("SIMULATED_TRADING", "1")).strip().lower() in ("1", "true", "yes")
    base_url = env.get("BASE_URL") or "https://www.okx.com"

    client = OKXClient(
        api_key=api_key,
        api_secret=api_secret,
        passphrase=passphrase,
        simulated=simulated,
    )
    # Ajuste opcional de base_url si el cliente lo soporta
    if hasattr(client, "set_base_url") and callable(getattr(client, "set_base_url")):
        try:
            client.set_base_url(base_url)
        except Exception:
            pass
    else:
        try:
            setattr(client, "base_url", base_url)
        except Exception:
            pass
    return client


def print_banner(
    base_url: str,
    simulated: bool,
    strategies_count: int,
    strategies_dir: str,
    found_files: int,
) -> None:
    _print("╔══════════════════════════════════════════════════════╗")
    _print(f"║            {TERMINAL_TITLE:<42}║")
    _print("╚══════════════════════════════════════════════════════╝")
    _print(f"[OKX][CLIENT][BASE] base_url={base_url}")
    _print(f"[OKX][CLIENT][SIM] simulated={simulated}")
    _print(f"[BOOT] Carpeta estrategias: {strategies_dir}")
    _print(f"[BOOT] Archivos .py detectados: {found_files}")
    _print(f"[BOOT] Estrategias cargadas: {strategies_count}")


class StrategyThread(threading.Thread):
    """
    Hilo que ejecuta una estrategia individual.

    ✅ Contrato RECOMENDADO (todas las estrategias nuevas deben seguir esto):
      - Definir en el módulo una clase:
            class StrategyClass:
                def __init__(self, symbol, timeframe, magic, data_feed):
                    ...
                def run(self, stop_event):
                    ...

      - La estrategia:
          • Lee datos sólo desde self.data_feed (DataFeed compartido).
          • Ejecuta órdenes sólo vía ExecutionEngine:
                from utils.engine_execution import get_shared_engine
                engine = get_shared_engine()
                engine.process_open(...)
                engine.process_close(...)

   ⚠️ Caminos LEGACY (se mantienen sólo por compatibilidad, NO usar en código nuevo):
      - StrategyClass(symbol, timeframe, magic) sin data_feed en el constructor
        → se inyecta data_feed como atributo.
      - Variable global DATA_FEED en el módulo.
      - Función run(symbol, timeframe, magic, stop_event) a nivel de módulo.

    En TODOS los casos, las órdenes deben ir SIEMPRE vía ExecutionEngine
    (get_shared_engine().process_open/close), nunca directamente a OKXClient
    ni a OrderExecutor.
    """

    def __init__(self, module, spec: StrategySpec, data_feed: DataFeed):
        super().__init__(daemon=True, name=f"STRAT-{spec.name}")
        self.module = module
        self.spec = spec
        self.data_feed = data_feed
        self._stop = threading.Event()
        self.strategy_instance = None
        try:
            StrategyClass = getattr(self.module, "StrategyClass", None)
            if StrategyClass is not None:
                try:
                    # Contrato recomendado: StrategyClass(symbol, timeframe, magic, data_feed)
                    self.strategy_instance = StrategyClass(
                        symbol=self.spec.symbol,
                        timeframe=self.spec.timeframe,
                        magic=self.spec.magic,
                        data_feed=self.data_feed,
                    )
                except TypeError:
                    # LEGACY: StrategyClass antiguo sin data_feed en ctor
                    self.strategy_instance = StrategyClass(
                        symbol=self.spec.symbol,
                        timeframe=self.spec.timeframe,
                        magic=self.spec.magic,
                    )
                    # LEGACY: inyectamos data_feed como atributo para compatibilidad
                    setattr(self.strategy_instance, "data_feed", self.data_feed)
            else:
                # LEGACY: exponer DATA_FEED a nivel de módulo para código viejo
                setattr(self.module, "DATA_FEED", self.data_feed)
        except Exception:
            _print("[STRAT][CRASH] Excepción creando StrategyClass:\n" + traceback.format_exc())

    def stop(self):
        self._stop.set()

    def run(self):
        try:
            StrategyClass = getattr(self.module, "StrategyClass", None)
            if StrategyClass is not None and self.strategy_instance is not None:
                # Camino recomendado: StrategyClass.run(stop_event)
                # ─────────────────────────────────────────
                # WRAP_ON_BAR_CLOSE_STATUS (BLINDADO)
                # - WAITING solo antes de la primera evaluación real.
                # - Después de evaluar una vela cerrada: FLAT (si no abrió) o OPEN (si engine abrió).
                # - Si falla la evaluación por datos/excepción: DATA_ERROR.
                # ─────────────────────────────────────────
                try:
                    from utils.engine_execution import get_shared_engine as _get_engine
                    _engine = _get_engine()

                    # Normalizar para display consistente (sin cambiar lógica interna)
                    sym_disp = _normalize_symbol(self.spec.symbol)
                    tf_disp = _normalize_tf_if_needed(self.spec.timeframe)

                    # Asegurar status inicial con campos de display
                    _engine.ensure_status(self.spec.magic, sym_disp, tf_disp)
                    _st0 = _engine._read_status(int(self.spec.magic)) or {}
                    _st0["symbol_disp"] = sym_disp
                    _st0["tf_disp"] = tf_disp
                    _engine._write_status(int(self.spec.magic), _st0)

                    _orig_on_bar_close = getattr(self.strategy_instance, "_on_bar_close", None)
                    if callable(_orig_on_bar_close):
                        def _wrapped_on_bar_close(bar, candles):
                            now_iso = _now_iso()
                            try:
                                _orig_on_bar_close(bar, candles)
                                st = _engine._read_status(int(self.spec.magic)) or {}
                                if (st.get("status") or "").strip().upper() not in ("OPEN", "OPEN_ERROR", "ERROR"):
                                    st["status"] = "FLAT"
                                st["data_ok"] = True
                                st["last_eval_ts"] = now_iso
                                st["symbol_disp"] = sym_disp
                                st["tf_disp"] = tf_disp
                                _engine._write_status(int(self.spec.magic), st)
                            except Exception as e:
                                st = _engine._read_status(int(self.spec.magic)) or {}
                                st["status"] = "DATA_ERROR"
                                st["data_ok"] = False
                                st["last_eval_ts"] = now_iso
                                st["last_error"] = str(e)
                                st["symbol_disp"] = sym_disp
                                st["tf_disp"] = tf_disp
                                _engine._write_status(int(self.spec.magic), st)
                                raise
                        setattr(self.strategy_instance, "_on_bar_close", _wrapped_on_bar_close)
                except Exception:
                    # No bloquear la estrategia si falla el wrapping
                    pass
                run_fn = getattr(self.strategy_instance, "run", None)
                if callable(run_fn):
                    run_fn(stop_event=self._stop)
                    return
                # LEGACY: StrategyClass.loop() con bucle interno
                loop_fn = getattr(self.strategy_instance, "loop", None)
                if callable(loop_fn):
                    while not self._stop.is_set():
                        loop_fn()
                    else:
                        _print(
                            f"[STRAT][WARN] {self.spec.name} terminó loop() sin stop_event.",
                            verbose_only=True,
                        )
                else:
                    _print(
                        f"[STRAT][WARN] {self.spec.name} no expone run()/loop(). Hilo inactivo.",
                        verbose_only=True,
                    )
            else:
                # LEGACY: función run(...) a nivel de módulo
                run_fn = getattr(self.module, "run", None)
                if callable(run_fn):
                    run_fn(
                        symbol=self.spec.symbol,
                        timeframe=self.spec.timeframe,
                        magic=self.spec.magic,
                        stop_event=self._stop,
                    )
                else:
                    _print(
                        f"[STRAT][WARN] {self.spec.name} no expone StrategyClass ni run(). Hilo inactivo.",
                        verbose_only=True,
                    )
        except Exception:
            _print("[STRAT][CRASH] Excepción en hilo de estrategia:\n" + traceback.format_exc())


class Terminal:
    def __init__(self) -> None:
        global GLOBAL_VERBOSE
        self.env_path = _pick_env_path()
        self.env = load_env(self.env_path)
        # Propagar configuración a os.environ para que env_loader/env_get
        # vea MAX_RISK_PCT, USDT_HAIRCUT, etc., sin romper el uso actual de self.env.
        for k, v in self.env.items():
            if k not in os.environ:
                os.environ[k] = v
        GLOBAL_VERBOSE = 1 if str(self.env.get("VERBOSE", "0")).strip().lower() in ("1", "true", "yes") else 0

        self.client = create_okx_client(self.env)

        self.threads: List[StrategyThread] = []
        self.stopping = threading.Event()
        self.data_feed = DataFeed()
        self.ingestor: Optional[DataIngestor] = None
        self.ws_router: Optional[WsRouter] = None
        self.ws_thread: Optional[OkxPrivateWS] = None
        self.executor = None

        # Router WS
        self.ws_router = WsRouter(status_store=None, trade_logger=None, extra_handlers=[], verbose=(GLOBAL_VERBOSE == 1))

        # OrderExecutor (experimento / opcional para capas futuras; NO es API de estrategias)
        max_expo = float(self.env.get("MAX_EXPOSURE_PCT", "0.20"))
        self.executor = init_shared_executor(
            self.client,
            max_exposure_pct=max_expo,
            rate_global_per_sec=float(self.env.get("ORDER_RATE_GLOBAL_PER_SEC", "5")),
            rate_per_strategy_per_sec=float(self.env.get("ORDER_RATE_PER_STRAT_PER_SEC", "2")),
            pause_when_pending=True,
            verbose=(GLOBAL_VERBOSE == 1),
        )
        self.ws_router.add_handler(self.executor.on_order_event)

        # ExecutionEngine (rutas FIJAS; API ÚNICA de ejecución para estrategias)
        monitor_dir = Path(BASE_DIR) / "monitor"

        # Nombre de cuenta desde el .env (por defecto "OKX" si no viene)
        account_name = str(self.env.get("ACCOUNT_NAME", "OKX"))

        # Log UNIFICADO en la raíz del proyecto
        trade_log = Path(BASE_DIR) / "trade_log.csv"

        init_shared_engine(
            self.client,
            monitor_dir=monitor_dir,
            trade_log=trade_log,
            account_name=account_name,
            max_exposure_pct=max_expo,
        )

        # WS habilitado?
        self.ws_enabled = str(self.env.get("WS_ENABLED", "1")).strip().lower() in ("1", "true", "yes")
        self.ws_retry = int(self.env.get("WS_RETRY_SEC", "5"))

        self.strategies_dir = resolve_strategies_dir(self.env)

    def _collect_symbols_tfs(self, specs: List[StrategySpec]) -> Tuple[Set[str], Set[str]]:
        symbols: Set[str] = set()
        tfs: Set[str] = set()
        for s in specs:
            if s.symbol:
                symbols.add(s.symbol)
            if s.timeframe:
                tfs.add(s.timeframe)
        return symbols, tfs

    def load_and_start_strategies(self) -> None:
        found_files = len(_list_strategy_files(self.strategies_dir))
        specs = discover_strategy_modules(self.strategies_dir)

   # Inicializar status_{MAGIC}.json para todas las estrategias detectadas
        try:
            engine = get_shared_engine()
            for spec in specs:
                if spec.magic is None:
                    continue
                engine.ensure_status(spec.magic, spec.symbol, spec.timeframe)
        except Exception:
            _print(
                "[ENGINE][WARN] No se pudo asegurar status inicial de estrategias:\n"
                + traceback.format_exc(),
                verbose_only=True,
            )
        # Ingestor
        symbols, tfs = self._collect_symbols_tfs(specs)
        try:
            self.ingestor = DataIngestor(
                client=self.client,
                symbols=list(symbols) if symbols else ["BTC-USDT"],
                tfs=list(tfs) if tfs else ["1m"],
                limit=1000,
                interval_sec=60,
                stagger_ms=120,
                verbose=(GLOBAL_VERBOSE == 1),
            )
            self.ingestor.start()
        except Exception:
            _print("[INGESTOR][ERROR] No se pudo iniciar el DataIngestor:\n" + traceback.format_exc())

        # Estrategias
        loaded = 0
        for spec in specs:
            try:
                module = import_strategy_module(spec)
                t = StrategyThread(module=module, spec=spec, data_feed=self.data_feed)
                t.start()
                self.threads.append(t)
                loaded += 1
            except Exception:
                _print(f"[BOOT][ERROR] No se pudo cargar estrategia {spec.name}:\n" + traceback.format_exc())

        base_url = getattr(self.client, "base_url", self.env.get("BASE_URL", "https://www.okx.com"))
        simulated = True
        print_banner(
            base_url=str(base_url),
            simulated=bool(simulated),
            strategies_count=loaded,
            strategies_dir=self.strategies_dir,
            found_files=found_files,
        )

    def heartbeat(self) -> None:
        cycle = 0
        last_snap = 0.0
        snap_every = 30.0  # segundos

        monitor_dir = Path(BASE_DIR) / "monitor"

        while not self.stopping.is_set():
            cycle += 1
            auth = True

            # Snapshot de cuenta (balance/equity) + enriquecimiento de open_positions.json
            now = time.time()
            if (now - last_snap) >= snap_every:
                try:
                    write_account_and_positions(self.client, monitor_dir, auth_ok=auth)
                except Exception:
                    # No botar el terminal por fallos de snapshot
                    pass
                last_snap = now

            vivos_tracked = sum(1 for t in self.threads if t.is_alive())
            vivos_scan = sum(1 for th in threading.enumerate() if th.name.startswith("STRAT-"))
            _print(
                f"[{_now_iso()}] "
                f"hb_cycle={cycle} auth={auth} hilos_vivos={max(vivos_tracked, vivos_scan)}",
                verbose_only=True,
            )
            time.sleep(5)

    def stop(self) -> None:
        self.stopping.set()
        if self.ws_thread:
            try:
                self.ws_thread.stop()
            except Exception:
                pass
        if self.ingestor:
            try:
                self.ingestor.stop()
            except Exception:
                pass
        for t in self.threads:
            try:
                t.stop()
            except Exception:
                pass

    def run(self) -> None:
        def _sigint(sig, frame):
            _print("\n[CTRL+C] Saliendo…")
            self.stop()

        signal.signal(signal.SIGINT, _sigint)
        signal.signal(signal.SIGTERM, _sigint)

        # WS privado
        if self.ws_enabled:
            try:
                self.ws_thread = OkxPrivateWS(
                    client=self.client,
                    on_event=self.ws_router.handle,
                    retry_sec=self.ws_retry,
                    verbose=(GLOBAL_VERBOSE == 1),
                )
                self.ws_thread.subscribe_orders(None, inst_type="ANY")
                self.ws_thread.start()
                _print("[WS] Private orders WS iniciado", verbose_only=True)
            except Exception:
                _print("[WS][ERROR] No se pudo iniciar el WS privado:\n" + traceback.format_exc())

        # Ingestor + estrategias
        self.load_and_start_strategies()

        try:
            self.heartbeat()
        except KeyboardInterrupt:
            _print("\n[CTRL+C] Saliendo…")
        finally:
            self.stop()


def main():
    term = Terminal()
    term.run()


if __name__ == "__main__":
    main()