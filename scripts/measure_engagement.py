"""Measure engagement reliability with the real camera, guided on screen,
and record the demo clips while doing it.

Opens a full-screen page. A gold dot means LOOK: follow it around the screen
(the lamp lives on the screen, so looking anywhere on it should count). A red
screen means LOOK AWAY, with a direction (left, right, down at your phone, up
and away). It runs the exact attention detector and thresholds the character
uses, and records every detector sample so thresholds can be tuned from data.

    .venv/bin/python scripts/measure_engagement.py [--trials 12] [--object mug]

Every phase is also recorded in the background (no preview) to data/clips/.
After the test, a short guided section records the remaining demo clips
(hold up an object, put it on the desk, show your phone...). Those clips are
the camera input for scripts/record_demo.py.

Run it from Terminal (camera permission). Results: logs/engagement-*.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import threading
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

import cv2  # noqa: E402


class BackgroundRecorder:
    """Writes the latest camera frame at a steady 30 fps until stopped."""

    def __init__(self, vision: Vision):
        self.vision = vision
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self, path: Path) -> None:
        self.stop()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, args=(path,), daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._thread is not None:
            self._stop.set()
            self._thread.join(3)
            self._thread = None

    def _run(self, path: Path) -> None:
        f = self.vision.snapshot()
        if f is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        h, w = f.shape[:2]
        writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 30, (w, h))
        t0, k = time.monotonic(), 0
        while not self._stop.is_set():
            f = self.vision.snapshot()
            if f is not None and f.shape[:2] == (h, w):
                writer.write(f)
            k += 1
            time.sleep(max(0.0, t0 + k / 30 - time.monotonic()))
        writer.release()


def demo_clips(obj: str) -> list[dict]:
    return [
        {"name": "3-mug", "seconds": 10, "title": f"Hold up the {obj}",
         "how": f"Hold the {obj} up beside your face, facing the camera. Keep looking at the screen."},
        {"name": "4-desk", "seconds": 12, "title": f"Put the {obj} on the desk",
         "how": f"Before pressing Space, put the {obj} on the desk to one side where the camera can still see it. Then look at the screen."},
        {"name": "5-phone", "seconds": 10, "title": "Hold your phone up",
         "how": f"Take the {obj} out of view first. Hold your phone up toward the camera, screen facing it, and look at the screen."},
        {"name": "6-empty", "seconds": 10, "title": "Just you",
         "how": f"No {obj}, no phone in view. Look at the screen."},
        {"name": "7-leaf", "seconds": 10, "title": "Show something living", "optional": True,
         "how": "Hold up a leaf, a plant or a flower to the camera. Press S to skip if you have nothing."},
    ]


DIRECTIONS = ["Turn your head to the LEFT", "Turn your head to the RIGHT",
              "Look DOWN at your phone", "Look UP and away, over the screen"]


class Test:
    def __init__(self, a):
        self.a = a
        self.vision = Vision(a.camera, ROOT / "models" / "face_detection_yunet_2023mar.onnx")
        self.ws: web.WebSocketResponse | None = None
        self.running = False
        self.stop = False
        self.rec = BackgroundRecorder(self.vision)
        self.clips = Path(a.clips_out)
        self.controls: asyncio.Queue = asyncio.Queue()

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
            slug = "" if want == "look" else "-" + direction.split()[-1].lower().strip(",")
            clip_path = self.clips / "engagement" / f"{i + 1:02d}-{want}{slug}.mp4"
            if not a.no_record:
                self.rec.start(clip_path)
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
            self.rec.stop()
            phases.append({"want": want, "direction": direction, "end_engaged": engaged,
                           "latency": changed_at, "flips": flips, "clip": None if a.no_record else str(clip_path)})
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
        if not a.no_record:
            self._pick_demo_clips(phases)
            if not a.no_demo_clips and not self.stop:
                await self.record_demo_clips()
        await self.send(type="phase", phase="done", seconds=1, summary=summary, saved=str(out.relative_to(ROOT)),
                        clips=None if a.no_record else str(self.clips))

    def _pick_demo_clips(self, phases: list[dict]) -> None:
        """The demo's look / look-away clips come straight from the test phases."""
        look = next((p for p in phases if p["want"] == "look" and p["clip"]), None)
        down = next((p for p in phases if p["want"] == "away" and "DOWN" in p["direction"] and p["clip"]), None)
        for p, name in ((look, "1-look.mp4"), (down, "2-away.mp4")):
            if p and Path(p["clip"]).exists():
                shutil.copy(p["clip"], self.clips / name)
                print("demo clip", self.clips / name)

    async def record_demo_clips(self) -> None:
        plan = demo_clips(self.a.object)
        for k, c in enumerate(plan):
            if self.stop:
                return
            await self.send(type="phase", phase="pose", seconds=1, title=c["title"], how=c["how"],
                            index=k + 1, total=len(plan), optional=c.get("optional", False))
            while not self.controls.empty():
                self.controls.get_nowait()
            cmd = await self.controls.get()
            if cmd == "skip" and c.get("optional"):
                continue
            if cmd == "stop":
                return
            for n in (3, 2, 1):
                await self.send(type="count", n=n)
                await asyncio.sleep(1)
            await self.send(type="phase", phase="rec", seconds=c["seconds"], title=c["title"], how=c["how"])
            self.rec.start(self.clips / f"{c['name']}.mp4")
            await asyncio.sleep(c["seconds"])
            self.rec.stop()
            print("demo clip", self.clips / f"{c['name']}.mp4")

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
                await self.controls.put("stop")
            elif m.get("type") in ("go", "skip"):
                await self.controls.put(m["type"])
        return ws


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=12, help="look/away pairs (directions rotate)")
    ap.add_argument("--camera", default="0")
    ap.add_argument("--phase", type=float, default=8.0, help="seconds per phase")
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--object", default="mug", help="object used in the demo clips")
    ap.add_argument("--clips-out", default=str(ROOT / "data" / "clips"))
    ap.add_argument("--no-record", action="store_true", help="don't record video")
    ap.add_argument("--no-demo-clips", action="store_true", help="skip the demo-clip section after the test")
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
