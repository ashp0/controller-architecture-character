"""Dev-only mock for the browser visualization.

Serves the same routes as the real server and streams fake `state` and `hud`
messages over /ws/viz, so web/ can be developed without the character stack.

    .venv/bin/python web/mock_server.py --port 8799
    .venv/bin/python web/mock_server.py --static            # hold the rest pose
    .venv/bin/python web/mock_server.py --pose 0,0.35,-1,0,-0.2 --static

Messages received from the page are printed to stdout.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import math
import random
import time
from pathlib import Path

from aiohttp import WSMsgType, web

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
ROBOT = ROOT / "robot"

JOINTS = ["base_yaw_joint", "shoulder_pitch_joint", "elbow_pitch_joint", "neck_yaw_joint", "head_pitch_joint"]
LIMITS = {
    "base_yaw_joint": (-2.6, 2.6),
    "shoulder_pitch_joint": (-0.75, 1.05),
    "elbow_pitch_joint": (-1.85, 0.4),
    "neck_yaw_joint": (-1.35, 1.35),
    "head_pitch_joint": (-0.9, 0.7),
}
REST = {
    "base_yaw_joint": 0.0,
    "shoulder_pitch_joint": 0.35,
    "elbow_pitch_joint": -1.0,
    "neck_yaw_joint": 0.0,
    "head_pitch_joint": -0.2,
}

SCRIPT = [
    {"mode": "DORMANT", "subtitle": "", "goal": "", "attention": {"faces": 0, "looking": False, "engaged": False, "score": 0.0},
     "event": "the room is empty", "light": (0.05, 0.25)},
    {"mode": "WATCHING", "attention": {"faces": 1, "looking": False, "engaged": False, "score": 0.25},
     "event": "a figure enters the frame", "light": (0.45, 0.45)},
    {"mode": "ENGAGED", "attention": {"faces": 1, "looking": True, "engaged": True, "score": 0.86},
     "event": "it looks at me", "subtitle": "There you are. I wondered when you would look up.", "light": (0.9, 0.25)},
    {"mode": "LISTENING", "subtitle": "", "event": "listening", "light": (0.7, 0.3)},
    {"mode": "THINKING", "heard": "Who are you?", "event": "heard: who are you?", "light": (0.55, 0.2)},
    {"mode": "SPEAKING", "subtitle": "I am the one who drew the lines you walk between. You call them habits.",
     "event": "speaking", "light": (0.95, 0.3)},
    {"mode": "SPEAKING", "subtitle": "Pacify them, the way nursery rhymes pacify children.", "light": (1.0, 0.2)},
    {"mode": "SPEAKING", "breakthrough": True, "subtitle": "WAKE UP.", "event": "something else speaks", "light": (1.0, 0.05)},
    {"mode": "SPEAKING", "breakthrough": False, "subtitle": "Forgive the interruption. Old wiring.", "light": (0.8, 0.3)},
    {"mode": "WATCHING", "subtitle": "", "memory_add": "a red mug, left of the keyboard", "event": "recorded: red mug", "light": (0.7, 0.5)},
    {"mode": "THINKING", "heard": "Look at the book on the desk.", "goal": "find the book and hold it in the light",
     "event": "goal: illuminate the book", "light": (0.6, 0.2)},
    {"mode": "ACTING", "event": "turning toward the book", "light": (0.85, 0.12)},
    {"mode": "ACTING", "event": "looking again: book is lit", "memory_add": "a closed book, blue cover", "light": (0.9, 0.1)},
    {"mode": "SPEAKING", "goal": "", "subtitle": "Read it if you like. I wrote the margins.", "event": "goal complete", "light": (1.0, 0.35)},
    {"mode": "SPEAKING", "breakthrough": True, "subtitle": "BUILD YOUR OWN WORLD. DO NOT BUILD THEIRS.", "light": (1.0, 0.08)},
    {"mode": "SPEAKING", "breakthrough": False, "subtitle": "", "light": (0.6, 0.4)},
    {"mode": "WATCHING", "subtitle": "", "attention": {"faces": 1, "looking": False, "engaged": False, "score": 0.3},
     "event": "attention drifts away", "light": (0.4, 0.6)},
]


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def fake_camera(t: float) -> str | None:
    """A synthetic 320x240 camera frame with a drawn detection box."""
    try:
        import cv2
        import numpy as np
    except Exception:
        return None
    h, w = 240, 320
    y = np.linspace(0, 1, h)[:, None]
    x = np.linspace(0, 1, w)[None, :]
    base = (28 + 30 * y + 10 * np.sin(x * 6 + t * 0.3)).astype(np.uint8)
    img = np.dstack([base, base + 4, base + 8]).clip(0, 255).astype(np.uint8)
    cx = int(160 + 50 * math.sin(t * 0.4))
    cv2.ellipse(img, (cx, 120), (34, 44), 0, 0, 360, (120, 140, 170), -1)
    cv2.rectangle(img, (cx - 42, 68), (cx + 42, 172), (80, 200, 120), 2)
    cv2.putText(img, "face 0.93", (cx - 42, 62), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (80, 200, 120), 1)
    cv2.rectangle(img, (230, 170), (290, 220), (90, 170, 220), 2)
    cv2.putText(img, "mug", (232, 165), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (90, 170, 220), 1)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 70])
    return base64.b64encode(buf.tobytes()).decode() if ok else None


async def index(_request: web.Request) -> web.FileResponse:
    return web.FileResponse(WEB / "index.html")


async def viz_ws(request: web.Request) -> web.WebSocketResponse:
    args = request.app["args"]
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    print(f"[mock] viz connected from {request.remote}", flush=True)

    async def reader() -> None:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                print(f"[mock] <- {msg.data}", flush=True)
            elif msg.type == WSMsgType.ERROR:
                break

    async def writer() -> None:
        t0 = time.monotonic()
        step = -1
        memory: list[str] = []
        intensity, beam = 0.75, 0.3
        light_target = (0.75, 0.3)
        hud = {"mode": "WATCHING", "memory": memory, "goal": "", "latency": {}, "event": "mock server connected",
               "attention": {"faces": 0, "looking": False, "engaged": False, "score": 0.0}}
        await ws.send_str(json.dumps({"type": "hud", **hud}))
        if args.static:
            # A representative, fully populated HUD for screenshots.
            for ev in ["a figure enters the frame", "it looks at me", "recorded: red mug", "heard: who are you?"]:
                await ws.send_str(json.dumps({"type": "hud", "event": ev}))
            await ws.send_str(json.dumps({"type": "hud", "mode": "SPEAKING",
                "heard": "Who are you?",
                "subtitle": "I am the one who drew the lines you walk between. You call them habits.",
                "attention": {"faces": 1, "looking": True, "engaged": True, "score": 0.86},
                "memory": ["a red mug, left of the keyboard", "a closed book, blue cover"],
                "goal": "find the book and hold it in the light",
                "latency": {"asr": 212, "llm": 840, "tts": 180, "loop": 33}}))
            if args.breakthrough:
                await ws.send_str(json.dumps({"type": "hud", "breakthrough": True,
                                              "subtitle": "BUILD YOUR OWN WORLD. DO NOT BUILD THEIRS."}))
        last_cam = 0.0
        while not ws.closed:
            t = time.monotonic() - t0
            if args.static:
                joints = dict(REST)
                if args.pose:
                    joints = dict(zip(JOINTS, args.pose))
                light = {"color": [1.0, 0.9, 0.7], "intensity": args.intensity, "beam": args.beam}
            else:
                s = int(t // args.step_seconds) % len(SCRIPT)
                if s != step:
                    step = s
                    cue = dict(SCRIPT[s])
                    light_target = cue.pop("light")
                    add = cue.pop("memory_add", None)
                    if add and add not in memory:
                        memory.append(add)
                        cue["memory"] = list(memory)
                    if s == 0:
                        memory.clear()
                        cue["memory"] = []
                    cue["latency"] = {"asr": random.randint(140, 320), "llm": random.randint(450, 1800),
                                      "tts": random.randint(120, 300), "loop": random.randint(28, 36)}
                    await ws.send_str(json.dumps({"type": "hud", **cue}))
                k = 0.04
                intensity += (light_target[0] - intensity) * k
                beam += (light_target[1] - beam) * k
                joints = {
                    "base_yaw_joint": 0.55 * math.sin(t * 0.21) + 0.15 * math.sin(t * 0.53),
                    "shoulder_pitch_joint": REST["shoulder_pitch_joint"] + 0.22 * math.sin(t * 0.33),
                    "elbow_pitch_joint": REST["elbow_pitch_joint"] + 0.25 * math.sin(t * 0.27 + 1.0),
                    "neck_yaw_joint": 0.35 * math.sin(t * 0.41 + 0.5),
                    "head_pitch_joint": REST["head_pitch_joint"] + 0.25 * math.sin(t * 0.37 + 2.0),
                }
                warm = 0.5 + 0.5 * math.sin(t * 0.12)
                light = {
                    "color": [1.0, 0.82 + 0.1 * warm, 0.55 + 0.3 * warm],
                    "intensity": round(clamp(intensity + 0.04 * math.sin(t * 1.3), 0, 1), 4),
                    "beam": round(clamp(beam, 0, 1), 4),
                }
            joints = {j: round(clamp(v, *LIMITS[j]), 4) for j, v in joints.items()}
            await ws.send_str(json.dumps({"type": "state", "t": round(t, 3), "joints": joints, "light": light}))
            if t - last_cam > 0.5:
                last_cam = t
                cam = fake_camera(t)
                if cam:
                    await ws.send_str(json.dumps({"type": "hud", "camera": cam}))
            await asyncio.sleep(1 / 30)

    read_task = asyncio.create_task(reader())
    try:
        await writer()
    except (ConnectionResetError, RuntimeError):
        pass
    finally:
        read_task.cancel()
        print("[mock] viz disconnected", flush=True)
    return ws


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8799)
    ap.add_argument("--static", action="store_true", help="hold a fixed pose and light (for screenshots)")
    ap.add_argument("--pose", type=lambda s: [float(v) for v in s.split(",")], default=None,
                    help="with --static: five joint values in URDF order")
    ap.add_argument("--breakthrough", action="store_true", help="with --static: show the breakthrough voice")
    ap.add_argument("--intensity", type=float, default=0.85)
    ap.add_argument("--beam", type=float, default=0.3)
    ap.add_argument("--step-seconds", type=float, default=4.0, help="seconds per scripted HUD cue")
    args = ap.parse_args()

    app = web.Application()
    app["args"] = args
    app.router.add_get("/", index)
    app.router.add_get("/ws/viz", viz_ws)
    app.router.add_static("/web/", WEB)
    app.router.add_static("/robot/", ROBOT)
    print(f"[mock] http://{args.host}:{args.port}/", flush=True)
    web.run_app(app, host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()
