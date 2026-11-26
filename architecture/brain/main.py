"""The brain: perception, conversation, memory and behaviour.

Runs as its own process and talks to the body over one WebSocket
(see docs/PROTOCOL.md). Decisions are made here; motion is executed there.

    camera ──► Vision (faces, attention) ─┐
    mic ─────► Hearing (VAD + Whisper) ───┼─► Behaviour (state machine) ─► Body (/ws/brain)
    viewer ──► typed text / push-to-talk ─┘        │   ▲                     motion + light
                                                   ▼   │
                                   Mind (OpenCode Go) + Ledger      Mixer ─► speaker
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from pathlib import Path

import aiohttp

from .audio import Mixer
from .hearing import Hearing
from .memory import Ledger
from .metrics import Metrics
from .mind import Mind
from .persona import LIGHTS, PERSONA, Lines
from .soundbank import load_all
from .vision import Vision
from .voice import Voice, sentences

log = logging.getLogger("brain")

GESTURES = {"nod", "shake", "recoil", "bow", "ponder", "focus", "awaken", "survey"}
GESTURE_LEN = {"nod": 1.05, "shake": 1.5, "recoil": 2.2, "bow": 2.8, "survey": 4.0, "ponder": 3.2, "awaken": 1.8, "focus": 2.0}
SFX = {"ack", "glitch", "complete"}

ENGAGE_SCORE = 0.6      # smoothed looking score needed to engage (~0.4 s of steady gaze)
RELEASE_SCORE = 0.2     # below this, attention counts as elsewhere
RELEASE_AFTER = 2.5     # seconds of attention elsewhere before disengaging
SLEEP_AFTER = 8.0       # seconds with no face before going dormant
AIM_TOLERANCE = 0.09    # normalized image distance counted as "on target"


class BodyLink:
    def __init__(self, url: str):
        self.url = url
        self.ws: aiohttp.ClientWebSocketResponse | None = None
        self.state: dict = {}
        self.inbox: asyncio.Queue = asyncio.Queue()
        self._done: dict[str, asyncio.Future] = {}
        self._n = 0

    async def connect(self, session: aiohttp.ClientSession) -> None:
        for _ in range(100):
            try:
                self.ws = await session.ws_connect(self.url, heartbeat=10)
                break
            except aiohttp.ClientError:
                await asyncio.sleep(0.2)
        else:
            raise RuntimeError(f"cannot reach body at {self.url}")
        asyncio.create_task(self._reader())

    async def _reader(self) -> None:
        async for msg in self.ws:
            m = json.loads(msg.data)
            t = m.get("type")
            if t == "state":
                self.state = m
            elif t == "gesture_done":
                f = self._done.pop(m.get("id"), None)
                if f and not f.done():
                    f.set_result(True)
            else:
                await self.inbox.put(m)  # viewer input relayed by the body
        log.error("body connection closed")

    async def send(self, **m) -> None:
        if self.ws is not None and not self.ws.closed:
            await self.ws.send_str(json.dumps(m))

    async def gesture(self, name: str, wait: bool = False) -> None:
        if name not in GESTURES:
            return
        self._n += 1
        gid = f"g{self._n}"
        fut = asyncio.get_running_loop().create_future()
        self._done[gid] = fut
        await self.send(type="gesture", name=name, id=gid)
        if wait:
            try:
                await asyncio.wait_for(fut, GESTURE_LEN.get(name, 3) + 1.5)
            except asyncio.TimeoutError:
                pass

    async def light(self, name: str | None = None, color=None, intensity=None, beam=None, pulse=0.0, fade=0.6) -> None:
        if name in LIGHTS:
            color, intensity = LIGHTS[name]
        await self.send(type="light", color=color, intensity=intensity, beam=beam, pulse=pulse, fade=fade)

    async def settle(self, u: float, v: float, timeout: float = 4.0) -> float:
        """Wait until the lamp has stopped moving; return final aim error."""
        t0 = time.monotonic()
        await asyncio.sleep(0.25)
        while time.monotonic() - t0 < timeout:
            s = self.state
            if s.get("settled"):
                break
            await asyncio.sleep(0.05)
        a = self.state.get("aim", {"u": 0.5, "v": 0.5})
        return math.dist((a["u"], a["v"]), (u, v))

    async def hud(self, **fields) -> None:
        await self.send(type="hud", **fields)


class Brain:
    def __init__(self, args):
        root = Path(args.root)
        self.args = args
        self.metrics = Metrics(root / "logs", body_pid=args.body_pid)
        self.body = BodyLink(f"ws://{args.host}:{args.port}/ws/brain")
        sounds = load_all(root / "data" / "sounds")
        self.mixer = Mixer(sounds, device=args.speaker, enabled=not args.silent)
        self.voice = Voice(root / "models" / "en_GB-alan-medium.onnx")
        self.vision = Vision(args.camera, root / "models" / "face_detection_yunet_2023mar.onnx", enabled=not args.no_camera)
        self.hearing = Hearing(root / "models" / "whisper", args.whisper, device=args.mic, enabled=not args.no_mic,
                               is_muted=lambda: self.mixer.speaking)
        self.mind = Mind(args.model)
        self.ledger = Ledger(root / "data" / "ledger.json", keep=args.keep_memory)
        self.lines = Lines(root / "architecture" / "lines.json")
        self.mode = "DORMANT"
        self.engaged = False
        self.met_before = False
        self.busy = False
        self.acting = False
        self.speaking = False
        self.mood_light = "gold"
        self.last_attention = time.monotonic()
        self.last_face = 0.0
        self.last_turn = 0.0
        self.last_observe = 0.0
        self.cooldown_until = 0.0
        self.last_whisper = time.monotonic()
        self.last_monologue = time.monotonic()
        self.last_src = "mic"
        self.epoch = 0  # increments on every (re)engagement; stale transitions check it
        self.turns: asyncio.Queue = asyncio.Queue(maxsize=2)

    # ---- lifecycle ------------------------------------------------------
    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        await self.set_mode("WAKING")
        log.info("loading speech recognition (%s)...", self.args.whisper)
        await loop.run_in_executor(None, self.hearing.load)
        utterances = self.hearing.start(loop)
        self.vision.start()
        async with aiohttp.ClientSession() as session:
            await self.body.connect(session)
            await self.go_dormant(initial=True)
            asyncio.create_task(self._prewarm_lines())
            tasks = [
                self._attention_loop(),
                self._voice_light_loop(),
                self._hud_loop(),
                self._input_loop(utterances),
                self._turn_loop(),
                self.metrics.sampler(),
            ]
            if not self.mind.ready:
                log.error("OPENCODE_API_KEY is not set: conversation is disabled (see .env.example)")
            log.info("ready: open http://%s:%s", self.args.host, self.args.port)
            await asyncio.gather(*tasks)

    async def _prewarm_lines(self) -> None:
        loop = asyncio.get_running_loop()
        for s in self.lines.all():
            await loop.run_in_executor(None, self.voice.synth, s)

    async def set_mode(self, mode: str) -> None:
        if mode != self.mode:
            self.mode = mode
            await self.body.hud(mode=mode)

    async def event(self, text: str) -> None:
        log.info(text)
        await self.body.hud(event=f"{time.strftime('%H:%M:%S')}  {text}")

    # ---- engagement -----------------------------------------------------
    async def _attention_loop(self) -> None:
        last_look = (0.5, 0.5)
        while True:
            await asyncio.sleep(0.1)
            a = self.vision.att
            now = time.monotonic()
            if a.faces:
                self.last_face = now
            if a.score > RELEASE_SCORE:
                self.last_attention = now

            if not self.engaged:
                if a.score > ENGAGE_SCORE and now > self.cooldown_until and not self.busy:
                    await self.engage(reason="gaze")
                elif a.faces and self.mode == "DORMANT":
                    await self.go_watching()
                elif not a.faces and self.mode == "WATCHING" and now - self.last_face > SLEEP_AFTER:
                    await self.go_dormant()
                elif (self.mode == "WATCHING" and a.faces and not self.busy and now - self.last_whisper > 40
                      and now - self.last_attention > 12):
                    # Someone is here but looking elsewhere (a phone, probably). The second voice tries.
                    self.last_whisper = now
                    asyncio.create_task(self.breakthrough(self.lines.pick("breakthrough")))
            else:
                idle = not (self.busy or self.acting or self.hearing.speech_active or self.speaking)
                # Someone typing is looking at their keyboard: give typed turns a longer grace.
                grace = 20.0 if self.last_src == "typed" else 3.0
                blind = not self.vision.ok and now - self.last_turn > 60
                if idle and ((self.vision.ok and now - self.last_attention > RELEASE_AFTER and now - self.last_turn > grace)
                             or blind):
                    await self.disengage(reason="attention moved elsewhere")
                elif idle and now - self.last_turn > 20 and now - self.last_monologue > 35:
                    # A silent visitor gets the doctrine, and sometimes the voice beneath it.
                    self.last_monologue = now
                    asyncio.create_task(self.monologue())
                elif idle and now - self.last_observe > 30 and now - self.last_turn > 6:
                    self.last_observe = now
                    asyncio.create_task(self.glance())

            # Gaze follows the face (engaged: closely; watching: lazily).
            if a.faces and not self.acting and self.mode != "DORMANT":
                target = (a.u, a.v)
                if math.dist(target, last_look) > (0.02 if self.engaged else 0.08):
                    await self.body.send(type="look", u=round(a.u, 3), v=round(a.v, 3))
                    last_look = target

    async def go_dormant(self, initial: bool = False) -> None:
        self.engaged = False
        self.hearing.listening = False
        await self.set_mode("DORMANT")
        await self.body.send(type="posture", name="sleep", speed=0.35)
        await self.body.send(type="look", home=True)
        await self.body.light(color=[1.0, 0.62, 0.25], intensity=0.07, beam=0.8, pulse=0.15, fade=2.5)
        self.mixer.play_music("music_dormant", 0.35 if initial else 0.3, fade=4.0)
        if not initial:
            await self.event("dormant: nobody in view")

    async def go_watching(self) -> None:
        await self.set_mode("WATCHING")
        await self.body.send(type="posture", name="idle", speed=0.4)
        await self.body.light("cold", intensity=0.25, beam=0.5, fade=1.5)
        await self.event("someone is in view")

    async def engage(self, reason: str) -> None:
        self.epoch += 1
        self.engaged = True
        self.last_attention = self.last_turn = time.monotonic()
        self.metrics.log("engage", reason=reason, score=round(self.vision.att.score, 2))
        await self.event(f"engaged ({reason})")
        await self.set_mode("ENGAGED")
        await self.body.hud(attention={"engaged": True})
        self.busy = True
        try:
            if not self.met_before:
                # The full awakening: sound, light, motion, music, then voice.
                self.mixer.sfx_play("awaken", 0.8)
                await self.body.send(type="posture", name="attentive", speed=0.8)
                await self.body.gesture("awaken")
                await self.body.light(color=[1.0, 0.74, 0.32], intensity=0.85, beam=0.45, fade=2.2)
                self.mixer.play_music("music_theme", 0.42, fade=3.0)
                await asyncio.sleep(2.2)
                await self.speak(self.lines.pick("greet_first"))
                self.met_before = True
            else:
                self.mixer.sfx_play("ack", 0.6)
                await self.body.send(type="posture", name="attentive", speed=0.8)
                await self.body.gesture("nod")
                await self.body.light("gold", fade=0.8)
                self.mixer.play_music("music_theme", 0.38, fade=2.0)
                await self.speak(self.lines.pick("greet_again"))
        finally:
            self.busy = False
        self.hearing.listening = True
        self.last_turn = time.monotonic()
        await self.set_mode("LISTENING")
        await self.body.light("gold", intensity=0.8, pulse=0.4, fade=0.6)
        if self.vision.ok:
            self.last_observe = time.monotonic()
            asyncio.create_task(self.glance())

    async def disengage(self, reason: str, farewell: bool = True) -> None:
        epoch = self.epoch
        self.engaged = False
        self.hearing.listening = False
        self.metrics.log("disengage", reason=reason)
        await self.event(f"disengaged ({reason})")
        await self.body.hud(attention={"engaged": False}, subtitle="")
        self.mixer.sfx_play("dismiss", 0.5)
        await self.body.gesture("bow")
        await self.body.light("dim", fade=2.0)
        self.mixer.play_music("music_dormant", 0.3, fade=3.0)
        if farewell:
            await self.speak(self.lines.pick("farewell"), light=False)
        if self.epoch != epoch:
            return  # someone re-engaged while the farewell was playing
        self.cooldown_until = time.monotonic() + 3.0
        self.mind.reset()
        await (self.go_watching() if self.vision.att.faces else self.go_dormant())

    # ---- voice ----------------------------------------------------------
    async def monologue(self) -> None:
        self.busy = True
        try:
            await self.body.gesture("survey")
            await self.speak(self.lines.pick("doctrine"))
            if time.monotonic() % 2 < 1:
                await self.breakthrough(self.lines.pick("breakthrough"))
        finally:
            self.busy = False
            self.last_turn = time.monotonic()

    async def breakthrough(self, text: str) -> None:
        """The second voice cuts through: glitch, red flash, recoil, distorted speech."""
        text = text.strip().upper()
        if not text:
            return
        loop = asyncio.get_running_loop()
        audio = await loop.run_in_executor(None, self.voice.synth, text, "break")
        self.mixer.set_duck(0.0)
        self.mixer.sfx_play("glitch", 0.9)
        await self.body.light("red", intensity=1.0, beam=0.9, fade=0.05)
        await self.body.gesture("recoil")
        await self.body.hud(subtitle=text, breakthrough=True)
        await self.event(f"breakthrough: {text}")
        self.speaking = True
        await asyncio.sleep(0.25)
        try:
            await self.mixer.say(audio)
        finally:
            self.speaking = False
            await self.body.hud(subtitle="", breakthrough=False)
            await self.body.light(self.mood_light, fade=1.2)
            self.mixer.set_duck(1.0)

    async def speak(self, text: str, light: bool = True) -> float:
        """Speak sentence by sentence; return seconds until the first audio started."""
        if not text:
            return 0.0
        loop = asyncio.get_running_loop()
        t0 = time.perf_counter()
        first = None
        self.speaking = True
        prev_mode = self.mode
        await self.set_mode("SPEAKING")
        await self.body.hud(subtitle=text)
        self.mixer.set_duck(0.35)
        futs = []
        try:
            for s in sentences(text):
                audio = await loop.run_in_executor(None, self.voice.synth, s)
                futs.append(self.mixer.say(audio))
                if first is None:
                    first = time.perf_counter() - t0
            if futs:
                await asyncio.gather(*futs)
        finally:
            self.speaking = False
            self.mixer.set_duck(1.0)
            await self.body.hud(subtitle="")
            await self.set_mode("ACTING" if self.acting else "LISTENING" if self.engaged else prev_mode)
        return first or 0.0

    async def _voice_light_loop(self) -> None:
        """While speaking, the lamp's light breathes with the voice envelope."""
        level = 0.0
        while True:
            await asyncio.sleep(1 / 15)
            if self.speaking and not self.acting:
                level = 0.6 * level + 0.4 * min(1.0, self.mixer.voice_level * 6)
                color, base = LIGHTS.get(self.mood_light, LIGHTS["gold"])
                await self.body.send(type="light", color=color, intensity=round(min(1, base * (0.55 + 0.6 * level)), 3),
                                     pulse=0, fade=0.08)

    # ---- input ----------------------------------------------------------
    async def _input_loop(self, utterances: asyncio.Queue) -> None:
        async def from_mic():
            while True:
                u = await utterances.get()
                await self._offer(u["text"], "mic", stt_ms=u["stt_ms"], t_end=u["t_end"])

        async def from_viewer():
            while True:
                m = await self.body.inbox.get()
                if m.get("type") == "text" and m.get("text", "").strip():
                    await self._offer(m["text"].strip(), "typed", t_end=time.monotonic())
                elif m.get("type") == "ptt":
                    self.hearing.ptt = bool(m.get("down"))
                    if self.hearing.ptt:
                        await self.set_mode("LISTENING")

        await asyncio.gather(from_mic(), from_viewer())

    async def _offer(self, text: str, src: str, stt_ms: float = 0.0, t_end: float = 0.0) -> None:
        if self.busy and src == "mic":
            return
        try:
            self.turns.put_nowait((text, src, stt_ms, t_end))
        except asyncio.QueueFull:
            pass

    async def _turn_loop(self) -> None:
        while True:
            text, src, stt_ms, t_end = await self.turns.get()
            if src == "typed":
                t_end = time.monotonic()  # measure from when the turn starts, not from queueing
            try:
                await self.turn(text, src, stt_ms, t_end)
            except Exception as e:
                log.exception("turn failed")
                await self.event(f"error: {e.__class__.__name__}")
                self.busy = self.acting = False
                await self.speak(self.lines.pick("error"))

    # ---- one conversational turn ---------------------------------------
    async def turn(self, text: str, src: str, stt_ms: float, t_end: float) -> None:
        self.busy = True
        self.last_turn = time.monotonic()
        self.last_src = src
        await self.body.hud(heard=text)
        await self.event(f"heard ({src}): {text}")
        if not self.engaged:
            # Typed input or push-to-talk counts as an explicit bid for attention.
            self.epoch += 1
            self.engaged = True
            self.hearing.listening = True
            self.met_before = True
            self.mixer.play_music("music_theme", 0.38, fade=2.0)
            await self.body.hud(attention={"engaged": True})
        await self.body.send(type="posture", name="attentive", speed=0.8)
        await self.set_mode("THINKING")
        self.mixer.sfx_play("think", 0.35)
        self.mixer.set_duck(0.6)
        await self.body.gesture("ponder")
        await self.body.light("green", intensity=0.6, pulse=1.4, fade=0.4)

        frame = self.vision.snapshot()
        image = self.vision.jpeg_b64(frame) if frame is not None else None
        a = self.vision.att
        situation = (f"The visitor is {'looking at you' if a.looking else 'in view' if a.faces else 'not visible'}. "
                     f"{'A camera frame is attached.' if image else 'Your camera is unavailable.'}")
        if not self.mind.ready:
            await self.speak(self.lines.pick("error"))
            self.busy = False
            return

        call = asyncio.create_task(self.mind.respond(PERSONA, self.ledger.prompt(), situation, text, image))
        done, _ = await asyncio.wait({call}, timeout=3.5)
        filler_at = None
        if not done:
            filler_at = time.monotonic()
            filler_at += await self.speak(self.lines.pick("slow"))
        reply, llm_ms = await asyncio.wait_for(call, 30)
        self.mixer.set_duck(1.0)

        # Memory first: what it saw this turn is retained even if speech fails.
        for item in reply.get("remember") or []:
            if isinstance(item, dict) and item.get("name"):
                if self.ledger.add(item["name"], item.get("details", ""), item.get("where", "")):
                    await self.event(f"ledger + {item['name']}")
        await self.body.hud(memory=self.ledger.hud())

        self.mood_light = reply.get("light") if reply.get("light") in LIGHTS else "gold"
        await self.body.light(self.mood_light, beam=0.45, fade=0.5)
        if reply.get("sfx") in SFX:
            self.mixer.sfx_play(reply["sfx"], 0.6)
        if reply.get("gesture") in GESTURES:
            await self.body.gesture(reply["gesture"])
        speak_start = time.monotonic()
        first_audio = await self.speak(str(reply.get("say", "")).strip())
        if reply.get("breakthrough"):
            await self.breakthrough(str(reply["breakthrough"])[:60])
        # Response latency: end of the visitor's speech -> first audio of the reply.
        # (If a filler line covered the wait, the first thing the visitor heard was that.)
        response_ms = ((filler_at or speak_start + first_audio) - t_end) * 1000 if t_end else 0
        self.metrics.log("turn", src=src, stt_ms=round(stt_ms), llm_ms=round(llm_ms),
                         tts_first_ms=round(first_audio * 1000), response_ms=round(response_ms),
                         filler=not done, heard=text, said=reply.get("say", ""))
        await self.body.hud(latency={"stt": round(stt_ms), "llm": round(llm_ms), "tts": round(first_audio * 1000),
                                     "reply": round(response_ms)})

        plan = [s for s in (reply.get("plan") or []) if isinstance(s, dict)]
        if plan:
            ok = await self.execute(plan, text)
            if ok and reply.get("done_line"):
                self.mixer.sfx_play("complete", 0.6)
                await self.speak(str(reply["done_line"]))
        self.last_turn = time.monotonic()
        self.busy = False
        if reply.get("farewell"):
            await self.disengage("visitor said goodbye", farewell=False)
        elif self.engaged:
            await self.body.light(self.mood_light, intensity=0.8, pulse=0.4, fade=0.8)
            await self.set_mode("LISTENING")

    # ---- goal-directed action ------------------------------------------
    async def execute(self, plan: list[dict], goal: str) -> bool:
        """Run the model's plan. Every aim is closed-loop: move, look again, correct."""
        self.acting = True
        await self.set_mode("ACTING")
        await self.body.hud(goal=goal)
        t0 = time.monotonic()
        steps_log = []
        ok = True
        beam = 0.15
        try:
            for step in plan[:8]:
                op = step.get("op")
                if op == "light":
                    beam = 0.12 if step.get("beam") == "narrow" else 0.6
                    await self.body.light(step.get("light", "white"), beam=beam, fade=0.4)
                elif op == "gesture":
                    await self.body.gesture(step.get("name", ""), wait=True)
                elif op == "wait":
                    await asyncio.sleep(max(0.2, min(3.0, float(step.get("seconds", 1)))))
                elif op == "aim":
                    res = await self.aim(step.get("target", "object"), step.get("box"), beam)
                    steps_log.append(res)
                    if not res["ok"]:
                        ok = False
                        break
        finally:
            self.vision.boxes = []
            self.acting = False
            await self.body.hud(goal="")
        self.metrics.log("goal", goal=goal, ok=ok, seconds=round(time.monotonic() - t0, 2), steps=steps_log)
        if not ok:
            self.mixer.sfx_play("glitch", 0.6)
            await self.body.light("red", fade=0.3)
            await self.body.gesture("shake")
            await self.speak(self.lines.pick("goal_lost"))
        return ok

    async def aim(self, target: str, box, beam: float) -> dict:
        res = {"target": target, "attempts": 0, "ok": False, "errors": []}
        if not (isinstance(box, list) and len(box) == 4):
            box = None
        loop_t = time.monotonic()
        for attempt in range(3):
            res["attempts"] = attempt + 1
            if box is None:
                frame = self.vision.fresh_snapshot(time.monotonic())
                if frame is None:
                    return res
                found, ms = await self.mind.locate(target, self.vision.jpeg_b64(frame))
                if not found.get("found") or not found.get("box"):
                    await self.event(f"cannot find {target}")
                    return res
                box = found["box"]
            u, v = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
            self.vision.boxes = [(*box, target)]
            await self.event(f"aim at {target} ({u:.2f}, {v:.2f})")
            await self.body.send(type="look", u=round(u, 3), v=round(v, 3))
            await self.body.light("white", beam=beam, fade=0.3)
            await self.body.settle(u, v)
            # Observe again: the object may have moved while the lamp did.
            moved_at = time.monotonic()
            frame = self.vision.fresh_snapshot(moved_at)
            if frame is None:
                res["ok"] = True  # no camera: trust the open-loop aim
                return res
            found, ms = await self.mind.locate(target, self.vision.jpeg_b64(frame))
            if not found.get("found") or not found.get("box"):
                await self.event(f"{target} is gone")
                return res
            box = found["box"]
            self.vision.boxes = [(*box, target)]
            cu, cv = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
            aim = self.body.state.get("aim", {"u": u, "v": v})
            err = math.dist((cu, cv), (aim["u"], aim["v"]))
            res["errors"].append(round(err, 3))
            await self.event(f"verify {target}: error {err:.2f}")
            if err < AIM_TOLERANCE:
                res["ok"] = True
                res["seconds"] = round(time.monotonic() - loop_t, 2)
                await self.body.gesture("focus")
                return res
            # Off target: correct toward where it is now (box carries over).
        return res

    # ---- background observation (scene memory) -------------------------
    async def glance(self) -> None:
        frame = self.vision.snapshot()
        if frame is None or not self.mind.ready:
            return
        try:
            obs, ms = await self.mind.observe(self.vision.jpeg_b64(frame))
        except Exception as e:
            log.warning("glance failed: %s", e)
            return
        added = []
        for o in (obs.get("objects") or [])[:5]:
            if isinstance(o, dict) and o.get("name") and self.ledger.add(o["name"], o.get("details", ""), o.get("where", "")):
                added.append(o["name"])
        self.metrics.log("glance", ms=round(ms), added=added)
        if added:
            await self.event("noticed: " + ", ".join(added))
        await self.body.hud(memory=self.ledger.hud())

    # ---- HUD ------------------------------------------------------------
    async def _hud_loop(self) -> None:
        while True:
            await asyncio.sleep(0.35)
            a = self.vision.att
            aim = self.body.state.get("aim")
            thumb = self.vision.thumbnail((aim["u"], aim["v"]) if aim and (self.acting or self.engaged) else None)
            fields = {"attention": {"faces": a.faces, "looking": a.looking, "engaged": self.engaged,
                                    "score": round(a.score, 2)}}
            if thumb:
                fields["camera"] = thumb
            await self.body.hud(**fields)
