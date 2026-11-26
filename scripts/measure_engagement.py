"""Measure engagement reliability with the real camera, guided on screen.

Opens a full-screen page. A gold dot means LOOK: follow it around the screen
(the lamp lives on the screen, so looking anywhere on it should count). A red
screen means LOOK AWAY, with a direction (left, right, down at your phone, up
and away). It runs the exact attention detector and thresholds the character
uses, and records every detector sample so thresholds can be tuned from data.

    .venv/bin/python scripts/measure_engagement.py [--trials 12] [--camera 0]

Run it from Terminal (camera permission). Results: logs/engagement-*.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
import webbrowser
from pathlib import Path

from aiohttp import WSMsgType, web

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from architecture.brain.main import ENGAGE_SCORE, RELEASE_AFTER, RELEASE_SCORE  # noqa: E402
from architecture.brain.vision import Vision  # noqa: E402

DIRECTIONS = ["Turn your head to the LEFT", "Turn your head to the RIGHT",
              "Look DOWN at your phone", "Look UP and away, over the screen"]


class Test:
    def __init__(self, a):
        self.a = a
        self.vision = Vision(a.camera, ROOT / "models" / "face_detection_yunet_2023mar.onnx")
        self.ws: web.WebSocketResponse | None = None
        self.running = False
        self.stop = False

    async def send(self, **m):
        if self.ws is not None and not self.ws.closed:
            await self.ws.send_str(json.dumps(m))

    async def run(self):
        a, v = self.a, self.vision
        await self.send(type="phase", phase="ready", seconds=3)
        await asyncio.sleep(3)
        engaged, last_attention = False, time.monotonic()
        phases, samples = [], []
        total = a.trials * 2
        for i in range(total):
            if self.stop:
                break
            want = "look" if i % 2 == 0 else "away"
            direction = DIRECTIONS[(i // 2) % len(DIRECTIONS)] if want == "away" else "dot"
            await self.send(type="phase", phase=want, seconds=a.phase, direction=direction,
                            index=i // 2 + 1, total=a.trials)
            start = time.monotonic()
            changed_at, flips = None, 0
            while time.monotonic() - start < a.phase and not self.stop:
                await asyncio.sleep(0.05)
                now = time.monotonic()
                s = v.att
                if s.score > RELEASE_SCORE:
                    last_attention = now
                before = engaged
                if not engaged and s.score > ENGAGE_SCORE:
                    engaged = True
                elif engaged and now - last_attention > RELEASE_AFTER:
                    engaged = False
                if engaged != before:
                    flips += 1
                    if changed_at is None and engaged == (want == "look"):
                        changed_at = now - start
                samples.append({"t": round(now - start, 2), "i": i, "want": want, "direction": direction,
                                "faces": s.faces, "looking": s.looking, "score": round(s.score, 3),
                                "yaw": round(s.yaw, 3), "pitch": round(s.pitch, 3), "size": round(s.size, 3),
                                "engaged": engaged})
                if len(samples) % 4 == 0:
                    await self.send(type="live", faces=s.faces, looking=s.looking, score=s.score, engaged=engaged)
            phases.append({"want": want, "direction": direction, "end_engaged": engaged,
                           "latency": changed_at, "flips": flips})
        looks = [p for p in phases if p["want"] == "look"]
        aways = [p for p in phases if p["want"] == "away"]
        eng = [p["latency"] for p in looks if p["latency"] is not None]
        rel = [p["latency"] for p in aways if p["latency"] is not None]
        summary = {
            "trials": len(looks),
            "engaged_when_looking": f"{sum(p['end_engaged'] for p in looks)}/{len(looks)}",
            "released_when_away": f"{sum(not p['end_engaged'] for p in aways)}/{len(aways)}",
            "engage_latency_s_median": round(statistics.median(eng), 2) if eng else "-",
            "release_latency_s_median": round(statistics.median(rel), 2) if rel else "-",
            "spurious_flips": sum(max(0, p["flips"] - 1) for p in phases),
            "detector_fps": round(v.fps, 1),
        }
        for d in DIRECTIONS:
            ps = [p for p in aways if p["direction"] == d]
            if ps:
                summary["released: " + d.lower()] = f"{sum(not p['end_engaged'] for p in ps)}/{len(ps)}"
        out = ROOT / "logs" / f"engagement-{time.strftime('%Y%m%d-%H%M%S')}.json"
        out.parent.mkdir(exist_ok=True)
        out.write_text(json.dumps({"summary": summary, "thresholds": {"engage": ENGAGE_SCORE, "release": RELEASE_SCORE,
                                   "release_after_s": RELEASE_AFTER}, "phases": phases, "samples": samples}, indent=1))
        print(json.dumps(summary, indent=2))
        print("saved", out)
        await self.send(type="phase", phase="done", seconds=1, summary=summary, saved=str(out.relative_to(ROOT)))

    async def ws_handler(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.ws = ws
        if not self.vision.ok:
            await self.send(type="error", text="Camera not readable. On macOS, allow Camera for your terminal app, then rerun.")
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            m = json.loads(msg.data)
            if m.get("type") == "start" and not self.running:
                self.running = True
                asyncio.create_task(self.run())
            elif m.get("type") == "stop":
                self.stop = True
        return ws


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=12, help="look/away pairs (directions rotate)")
    ap.add_argument("--camera", default="0")
    ap.add_argument("--phase", type=float, default=8.0, help="seconds per phase")
    ap.add_argument("--port", type=int, default=8770)
    a = ap.parse_args()
    t = Test(a)
    t.vision.start()
    t0 = time.monotonic()
    while not t.vision.ok and time.monotonic() - t0 < 8:
        time.sleep(0.1)
    if not t.vision.ok:
        print("warning: camera not readable yet (grant Camera permission to your terminal app)")
    app = web.Application()
    app.router.add_get("/", lambda r: web.FileResponse(ROOT / "scripts" / "engagement" / "page.html"))
    app.router.add_get("/ws", t.ws_handler)
    url = f"http://127.0.0.1:{a.port}/"
    print("open", url, " (Ctrl+C to quit)")
    webbrowser.open(url)
    web.run_app(app, host="127.0.0.1", port=a.port, print=None)


if __name__ == "__main__":
    main()
