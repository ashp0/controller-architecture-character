"""Summarize a metrics log: latency percentiles, goal success, engagement, CPU/RAM.

    .venv/bin/python scripts/report.py [logs/metrics-XXXX.jsonl]
"""
from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))] if xs else float("nan")


def main() -> None:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else max(Path("logs").glob("metrics-*.jsonl"))
    rows = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    by = lambda k: [r for r in rows if r["kind"] == k]  # noqa: E731
    print(f"log: {path}  ({len(rows)} events)\n")
    turns = by("turn")
    if turns:
        print(f"turns: {len(turns)}")
        for f in ("stt_ms", "llm_ms", "tts_first_ms", "response_ms"):
            xs = [t[f] for t in turns if t.get(f)]
            if xs:
                print(f"  {f:13s} p50 {pct(xs, 50):6.0f}   p90 {pct(xs, 90):6.0f}   max {max(xs):6.0f}")
    goals = by("goal")
    if goals:
        ok = sum(g["ok"] for g in goals)
        aims = [s for g in goals for s in g["steps"]]
        errs = [s["errors"][-1] for s in aims if s.get("errors")]
        print(f"\ngoals: {ok}/{len(goals)} completed; aims: {len(aims)}, "
              f"mean attempts {statistics.mean(s['attempts'] for s in aims):.2f}" if aims else f"\ngoals: {ok}/{len(goals)}")
        if errs:
            print(f"  final aim error (image fraction) p50 {pct(errs, 50):.3f}  max {max(errs):.3f}")
    print(f"\nengagements: {len(by('engage'))}   disengagements: {len(by('disengage'))}")
    res = by("resources")
    if res:
        for proc in ("brain", "body"):
            cpu = [r[proc]["cpu"] for r in res if proc in r]
            rss = [r[proc]["rss_mb"] for r in res if proc in r]
            if cpu:
                print(f"{proc:5s} CPU% (of one core) mean {statistics.mean(cpu):5.1f}  p90 {pct(cpu, 90):5.1f}   "
                      f"RSS MB mean {statistics.mean(rss):6.0f}  max {max(rss):6.0f}")
        sc = [r["system_cpu"] for r in res]
        print(f"system CPU% mean {statistics.mean(sc):.1f}")


if __name__ == "__main__":
    main()
