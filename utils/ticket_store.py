# utils/ticket_store.py — Store persistente de tickets por estrategia (MAGIC)
from __future__ import annotations
import json
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, List, Optional, Any
from utils.common import write_json_atomic, now_utc

@dataclass
class TicketInfo:
    ticket: str
    ordId: str | None
    magic: int
    symbol: str
    side: str
    lots: float
    open_time: str
    open_price: float | None
    comment: str | None = None
    closed: bool = False
    close_time: str | None = None

class TicketStore:
    """
    Estructura (en disco):
    {
      "by_magic": {
        "25000101": ["25000101_abcd1234", ...]
      },
      "by_ticket": {
        "25000101_abcd1234": {... TicketInfo ...}
      }
    }
    """
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data: Dict[str, Any] = {"by_magic": {}, "by_ticket": {}}
        self._load()

    # -------- Persistencia --------
    def _load(self):
        if not self.path.exists():
            return
        try:
            obj = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(obj, dict):
                self._data = obj
        except Exception:
            # si está corrupto, no rompemos el flujo; se sobreescribirá
            self._data = {"by_magic": {}, "by_ticket": {}}

    def _save(self):
        write_json_atomic(self.path, self._data)

    # -------- API --------
    def add_open(self, info: TicketInfo) -> None:
        by_m = self._data["by_magic"]
        by_t = self._data["by_ticket"]
        arr = by_m.setdefault(str(info.magic), [])
        if info.ticket not in arr:
            arr.append(info.ticket)
        by_t[info.ticket] = asdict(info)
        self._save()

    def mark_closed(self, ticket: str, close_time_iso: str | None = None) -> None:
        by_t = self._data["by_ticket"]
        rec = by_t.get(ticket)
        if not rec:
            return
        rec["closed"] = True
        rec["close_time"] = close_time_iso or now_utc().isoformat()
        self._save()

    def get_open_tickets(self, magic: int) -> List[str]:
        """Tickets no cerrados para un MAGIC (orden de creación)."""
        by_m = self._data["by_magic"]; by_t = self._data["by_ticket"]
        arr = list(by_m.get(str(magic), []))
        return [t for t in arr if by_t.get(t) and not by_t[t].get("closed")]

    def get_last_open(self, magic: int) -> Optional[TicketInfo]:
        ticks = self.get_open_tickets(magic)
        if not ticks:
            return None
        rec = self._data["by_ticket"].get(ticks[-1])
        if not rec:
            return None
        return TicketInfo(**rec)

    def get(self, ticket: str) -> Optional[TicketInfo]:
        rec = self._data["by_ticket"].get(ticket)
        return TicketInfo(**rec) if rec else None

    def all_open(self) -> List[TicketInfo]:
        out = []
        for t, rec in self._data["by_ticket"].items():
            if not rec.get("closed"):
                out.append(TicketInfo(**rec))
        return out
