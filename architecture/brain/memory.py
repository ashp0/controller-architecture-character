"""The Ledger: what the character has seen.

Plain structured records (name, details, where, first/last seen) rather than
embeddings. At demo scale (tens of objects) the whole ledger fits in the
prompt, which keeps recall exact and inspectable; it is saved to JSON so it
survives restarts when --keep-memory is used.
"""
from __future__ import annotations

import json
import time
from pathlib import Path


def _ago(t: float) -> str:
    s = int(time.time() - t)
    if s < 60:
        return f"{s}s ago"
    if s < 3600:
        return f"{s // 60} min ago"
    return f"{s // 3600} h ago"


class Ledger:
    def __init__(self, path: Path, keep: bool = False):
        self.path = path
        self.items: dict[str, dict] = {}
        if keep and path.exists():
            self.items = json.loads(path.read_text())

    @staticmethod
    def _key(name: str) -> str:
        return " ".join(name.lower().replace("the ", "").split())[:60]

    def add(self, name: str, details: str = "", where: str = "") -> bool:
        if not name.strip():
            return False
        k, now = self._key(name), time.time()
        new = k not in self.items
        it = self.items.setdefault(k, {"name": name.strip(), "details": "", "where": "", "first": now, "seen": 0})
        if details and len(details) >= len(it["details"]) * 0.5:
            it["details"] = details.strip()[:240]
        if where:
            it["where"] = where.strip()[:120]
        it["last"], it["seen"] = now, it["seen"] + 1
        self.save()
        return new

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.items, indent=1))

    def prompt(self) -> str:
        if not self.items:
            return "LEDGER: (empty)"
        rows = sorted(self.items.values(), key=lambda i: -i["last"])[:30]
        return "LEDGER:\n" + "\n".join(
            f"- {i['name']}: {i['details']} (where: {i['where'] or 'unknown'}; first seen {_ago(i['first'])}, last seen {_ago(i['last'])})"
            for i in rows)

    def hud(self) -> list[str]:
        rows = sorted(self.items.values(), key=lambda i: -i["last"])[:8]
        return [f"{i['name']}: {i['details'][:70]}" for i in rows]
