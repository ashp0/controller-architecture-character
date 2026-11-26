"""Measure engagement reliability with the real camera (guided protocol).

Prompts you to LOOK at the screen or LOOK AWAY (at your phone, to the side)
in alternating 8-second phases, and runs the same attention detector and
thresholds the character uses. Reports, per phase type, how often and how
fast the character would engage / release, plus false engagements.

    .venv/bin/python scripts/measure_engagement.py --trials 10 [--camera 0]
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from architecture.brain.main import ENGAGE_SCORE, RELEASE_AFTER, RELEASE_SCORE  # noqa: E402
from architecture.brain.vision import Vision  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=10)
    ap.add_argument("--camera", default="0")
    ap.add_argument("--phase", type=float, default=8.0)
    a = ap.parse_args()
    v = Vision(a.camera, ROOT / "models" / "face_detection_yunet_2023mar.onnx")
    v.start()
    t0 = time.monotonic()
    while not v.ok and time.monotonic() - t0 < 10:
        time.sleep(0.1)
    if not v.ok:
        sys.exit("camera not available")
    print("Sit where you would for the demo. Starting in 3 s.")
    time.sleep(3)
    results = []
    engaged = False
    last_attention = time.monotonic()
    for i in range(a.trials * 2):
        want = "look" if i % 2 == 0 else "away"
        print(f"\n[{i + 1}/{a.trials * 2}]  {'LOOK AT THE SCREEN' if want == 'look' else 'LOOK AWAY (phone / side)'}", flush=True)
        start = time.monotonic()
        changed_at = None
        flips = 0
        while time.monotonic() - start < a.phase:
            time.sleep(0.05)
            now = time.monotonic()
            s = v.att.score
            if s > RELEASE_SCORE:
                last_attention = now
            before = engaged
            if not engaged and s > ENGAGE_SCORE:
                engaged = True
            elif engaged and now - last_attention > RELEASE_AFTER:
                engaged = False
            if engaged != before:
                flips += 1
                if changed_at is None and engaged == (want == "look"):
                    changed_at = now - start
        results.append({"want": want, "end_engaged": engaged, "latency": changed_at, "flips": flips})
        print(f"   engaged={engaged}  reaction={'%.2fs' % changed_at if changed_at is not None else '-'}  flips={flips}")
    looks = [r for r in results if r["want"] == "look"]
    aways = [r for r in results if r["want"] == "away"]
    eng = [r["latency"] for r in looks if r["latency"] is not None]
    rel = [r["latency"] for r in aways if r["latency"] is not None]
    summary = {
        "trials": a.trials,
        "engaged_when_looking": f"{sum(r['end_engaged'] for r in looks)}/{len(looks)}",
        "released_when_away": f"{sum(not r['end_engaged'] for r in aways)}/{len(aways)}",
        "engage_latency_s_median": round(statistics.median(eng), 2) if eng else None,
        "release_latency_s_median": round(statistics.median(rel), 2) if rel else None,
        "spurious_flips": sum(max(0, r["flips"] - 1) for r in results),
        "detector_fps": round(v.fps, 1),
    }
    print("\n" + json.dumps(summary, indent=2))
    out = ROOT / "logs" / f"engagement-{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({"summary": summary, "phases": results}, indent=2))
    print("saved", out)


if __name__ == "__main__":
    main()
