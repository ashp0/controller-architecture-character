"""Body process: simulated lamp + visualization server.

Owns the robot model, joint limits and motion. Exposes:
    /ws/brain  one brain client: commands in, state (20 Hz) + viewer input out
    /ws/viz    any number of viewers: state (30 Hz) + HUD broadcast, input in
    /          the three.js viewer;  /robot/*  the URDF and mesh

Swapping this process for a hardware driver that speaks the same /ws/brain
protocol is the intended path to a physical lamp.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path

from aiohttp import WSMsgType, web

from ..urdf import Robot
from .controller import Body

ROOT = Path(__file__).resolve().parents[2]
log = logging.getLogger("body")

CONTROL_HZ = 100
STATE_HZ_BRAIN = 20
STATE_HZ_VIZ = 30


class BodyServer:
    def __init__(self, urdf: Path):
        self.body = Body(Robot(urdf))
        self.brain: web.WebSocketResponse | None = None
        self.viewers: set[web.WebSocketResponse] = set()
        self.hud: dict = {}

    # ---- command handling ----------------------------------------------
    def handle(self, m: dict) -> None:
        b, t = self.body, m.get("type")
        if t == "posture":
            b.set_posture(m.get("name", "idle"), m.get("speed", 0.6))
        elif t == "look":
            if m.get("home"):
                b.look_home()
            else:
                b.look_uv(float(m["u"]), float(m["v"]))
        elif t == "gesture":
            b.gesture(m.get("name", ""), m.get("id"))
        elif t == "light":
            b.set_light(m.get("color"), m.get("intensity"), m.get("beam"), m.get("pulse"), m.get("fade"))
        elif t == "hud":
            self.hud.update({k: v for k, v in m.items() if k != "type"})
            asyncio.ensure_future(self.broadcast(m))
        else:
            log.warning("unknown command %s", t)

    async def broadcast(self, m: dict) -> None:
        data = json.dumps(m)
        for ws in list(self.viewers):
            try:
                await ws.send_str(data)
            except Exception:
                self.viewers.discard(ws)

    # ---- loops ----------------------------------------------------------
    async def control_loop(self) -> None:
        n = 0
        while True:
            self.body.step()
            n += 1
            for gid in self.body.finished:
                await self._to_brain({"type": "gesture_done", "id": gid})
            self.body.finished.clear()
            if n % (CONTROL_HZ // STATE_HZ_BRAIN) == 0:
                await self._to_brain(self.body.state())
            if n % (CONTROL_HZ // STATE_HZ_VIZ) == 0 and self.viewers:
                await self.broadcast(self.body.state())
            await asyncio.sleep(1 / CONTROL_HZ)

    async def _to_brain(self, m: dict) -> None:
        if self.brain is not None and not self.brain.closed:
            try:
                await self.brain.send_str(json.dumps(m))
            except Exception:
                pass

    # ---- websocket endpoints -------------------------------------------
    async def ws_brain(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=10)
        await ws.prepare(request)
        self.brain = ws
        log.info("brain connected")
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    self.handle(json.loads(msg.data))
                except Exception as e:  # a bad command must never stop the body
                    log.warning("bad command %r: %s", msg.data[:200], e)
        log.info("brain disconnected; going to sleep")
        self.brain = None
        self.body.set_posture("sleep", 0.3)
        self.body.look_home()
        self.body.set_light(intensity=0.0, fade=2.0)
        return ws

    async def ws_viz(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=10)
        await ws.prepare(request)
        self.viewers.add(ws)
        if self.hud:
            await ws.send_str(json.dumps({"type": "hud", **self.hud}))
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                await self._to_brain(json.loads(msg.data))  # typed text / push-to-talk
        self.viewers.discard(ws)
        return ws

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/ws/brain", self.ws_brain)
        app.router.add_get("/ws/viz", self.ws_viz)
        app.router.add_get("/", lambda r: web.FileResponse(ROOT / "web" / "index.html"))
        app.router.add_static("/web/", ROOT / "web")
        app.router.add_static("/robot/", ROOT / "robot")

        async def start(_app):
            _app["loop"] = asyncio.create_task(self.control_loop())

        app.on_startup.append(start)
        return app


def main() -> None:
    p = argparse.ArgumentParser(description="Simulated lamp body + viewer")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--urdf", default=str(ROOT / "robot" / "dummy_lamp_5dof.urdf"))
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s body %(levelname)s %(message)s")
    web.run_app(BodyServer(Path(a.urdf)).app(), host=a.host, port=a.port, print=None)


if __name__ == "__main__":
    main()
