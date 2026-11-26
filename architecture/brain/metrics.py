"""Measurements: per-event JSON lines plus periodic CPU / memory samples."""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import psutil


class Metrics:
    def __init__(self, folder: Path, body_pid: int | None = None):
        folder.mkdir(parents=True, exist_ok=True)
        self.path = folder / f"metrics-{time.strftime('%Y%m%d-%H%M%S')}.jsonl"
        self.f = self.path.open("a", buffering=1)
        self.procs = {"brain": psutil.Process()}
        if body_pid:
            try:
                self.procs["body"] = psutil.Process(body_pid)
            except psutil.Error:
                pass

    def log(self, kind: str, **fields) -> None:
        self.f.write(json.dumps({"t": round(time.time(), 3), "kind": kind, **fields}) + "\n")

    async def sampler(self, every: float = 5.0) -> None:
        # Measurement must never take the character down: every psutil call is guarded.
        for p in self.procs.values():
            try:
                p.cpu_percent(None)
            except Exception:
                pass
        while True:
            await asyncio.sleep(every)
            row = {}
            for name, p in list(self.procs.items()):
                try:
                    row[name] = {"cpu": p.cpu_percent(None), "rss_mb": round(p.memory_info().rss / 2**20, 1),
                                 "threads": p.num_threads()}
                except Exception:
                    self.procs.pop(name, None)
            row["system_cpu"] = psutil.cpu_percent(None)
            self.log("resources", **row)
