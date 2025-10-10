from __future__ import annotations
import os
from pathlib import Path
from typing import Optional, Dict

ENV_FILENAME = "API_KEYs.env"

def _parse_env_lines(text: str) -> Dict[str, str]:
    env = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env

def load_api_env(root: Path) -> bool:
    path = (root / ENV_FILENAME).resolve()
    if not path.exists():
        return False
    env = _parse_env_lines(path.read_text(encoding="utf-8"))
    for k, v in env.items():
        if os.environ.get(k) is None or str(os.environ.get(k)).strip() == "":
            os.environ[k] = v
    return True

def env_get(name: str, default: Optional[str] = None) -> Optional[str]:
    val = os.environ.get(name)
    return val if (val is not None and str(val).strip() != "") else default

def env_bool(name: str, default: bool = False) -> bool:
    val = env_get(name, None)
    if val is None: return default
    return str(val).strip().lower() in ("1","true","yes","on")

def env_float(name: str, default: float = 0.0) -> float:
    try:
        return float(env_get(name, default))
    except Exception:
        return float(default)
