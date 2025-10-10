from __future__ import annotations
import json, time, threading
from pathlib import Path
from typing import Any, Dict

_file_lock = threading.Lock()

def now_ms() -> int:
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
