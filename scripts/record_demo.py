"""Record a screenshot demo of the real system, driven by typed prompts.

Plays your recorded clips as the lamp's camera, types the scripted prompts
into the running character (the same path as speech, after recognition),
waits for real events (engagement, replies, the second voice, goal
execution) and screenshots the viewer at each moment. Writes PNGs and a
captioned gallery to docs/demo/.

    .venv/bin/python scripts/record_demo.py --clips ~/Desktop/lamp-clips --object mug

Needs: clips named as in scripts/demo_script.json, an OpenCode Go key in .env,
and Playwright (pip install playwright==1.56.0). Runs silently by default.
"""
from __future__ import annotations

import argparse
import asyncio
import glob
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import aiohttp
import numpy as np

ROOT = Path(__file__).resolve().parents[1]


def find_chromium() -> str | None:
    pats = ["~/Library/Caches/ms-playwright/chromium-*/chrome-mac*/Chromium.app/Contents/MacOS/Chromium",
            "~/.cache/ms-playwright/chromium-*/chrome-linux*/chrome"]
    for p in pats:
        hits = sorted(glob.glob(os.path.expanduser(p)))
        if hits:
            return hits[-1]
    return None


class Hud:
    """Mirror of the viewer's HUD state, with waiters for events."""

    def __init__(self):
        self.state: dict = {}
        self.events: list[str] = []
        self.subtitles: list[tuple[float, str, bool]] = []
        self.modes: list[tuple[float, str]] = []
        self.changed = asyncio.Event()

    def feed(self, m: dict) -> None:
        now = time.monotonic()
        if "mode" in m:
            self.modes.append((now, m["mode"]))
        if m.get("subtitle"):
            self.subtitles.append((now, m["subtitle"], bool(m.get("breakthrough"))))
        if m.get("event"):
            self.events.append(m["event"])
        self.state.update(m)
        self.changed.set()

    async def wait(self, pred, timeout: float) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if pred():
                return True
            self.changed.clear()
            try:
                await asyncio.wait_for(self.changed.wait(), max(0.05, end - time.monotonic()))
            except asyncio.TimeoutError:
                pass
        return pred()


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--clips", required=True, help="folder with 1-look.mov ... 6-empty.mov (any video/image extension)")
    ap.add_argument("--object", default="mug", help="what you hold up in 3-mug / leave on the desk in 4-desk")
    ap.add_argument("--target", default="paper sign on the wall behind me", help="goal target that stays in view")
    ap.add_argument("--script", default=str(ROOT / "scripts" / "demo_script.json"))
    ap.add_argument("--out", default=str(ROOT / "docs" / "demo"))
    ap.add_argument("--port", type=int, default=8790)
    ap.add_argument("--size", default="1600x900")
    ap.add_argument("--sound", action="store_true", help="play audio (default: silent)")
    ap.add_argument("--video", default=None, help="also save a screen recording with the character's audio (.mp4)")
    a = ap.parse_args()

    clips = Path(a.clips).expanduser()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    black = out / ".black.png"
    import cv2

    cv2.imwrite(str(black), np.zeros((540, 960, 3), np.uint8))

    def clip(name: str) -> Path | None:
        if name == "BLACK":
            return black
        stem = Path(name).stem
        for p in sorted(clips.glob(stem + ".*")):
            return p
        return None

    steps = json.loads(Path(a.script).read_text())["steps"]
    first = clip(steps[0].get("camera", "BLACK")) or black
    cmd = [sys.executable, "-m", "architecture", "--no-browser", "--no-mic", "--allow-camera-switch",
           "--port", str(a.port), "--camera", str(first)] + ([] if a.sound else ["--silent"])
    tmp = out / ".rec"
    if a.video:
        tmp.mkdir(exist_ok=True)
        cmd += ["--record-audio", str(tmp / "audio.wav")]
    proc = subprocess.Popen(cmd, cwd=ROOT)
    try:
        for _ in range(200):
            with socket.socket() as s:
                if s.connect_ex(("127.0.0.1", a.port)) == 0:
                    break
            await asyncio.sleep(0.1)
        await asyncio.sleep(6)  # brain loads speech recognition, connects

        from playwright.async_api import async_playwright

        w, h = (int(x) for x in a.size.split("x"))
        gallery = []
        async with async_playwright() as pw, aiohttp.ClientSession() as http:
            args = ["--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"]
            try:
                browser = await pw.chromium.launch(args=args)
            except Exception:
                exe = find_chromium()
                if not exe:
                    sys.exit("No Chromium for screenshots: run  .venv/bin/python -m playwright install chromium")
                browser = await pw.chromium.launch(executable_path=exe, args=args)
            ctx_opts = {"viewport": {"width": w, "height": h}, "device_scale_factor": 1}
            if a.video:
                ctx_opts["record_video_dir"] = str(tmp)
                ctx_opts["record_video_size"] = {"width": w, "height": h}
            context = await browser.new_context(**ctx_opts)
            video_started = time.time()
            page = await context.new_page()
            await page.goto(f"http://127.0.0.1:{a.port}/")
            ws = await http.ws_connect(f"http://127.0.0.1:{a.port}/ws/viz")
            hud = Hud()

            async def reader():
                async for msg in ws:
                    m = json.loads(msg.data)
                    if m.get("type") == "hud":
                        hud.feed(m)

            asyncio.create_task(reader())
            await asyncio.sleep(3)

            for i, st in enumerate(steps):
                sub_mark = len(hud.subtitles)
                ev_mark = len(hud.events)
                mode_mark = len(hud.modes)
                if "camera" in st:
                    src = clip(st["camera"])
                    if src is None:
                        if st.get("optional_clip"):
                            print(f"skip step {i}: no clip {st['camera']}")
                            continue
                        print(f"warning: missing clip {st['camera']}; keeping the current camera")
                    else:
                        await ws.send_str(json.dumps({"type": "camera", "source": str(src.resolve())}))
                await asyncio.sleep(st.get("delay", 0))
                said = None
                if "say" in st:
                    said = st["say"].replace("{object}", a.object).replace("{target}", a.target)
                    sub_mark = len(hud.subtitles)
                    await ws.send_str(json.dumps({"type": "text", "text": said}))
                    print(f"> {said}")
                kind = st.get("await")
                timeout = st.get("timeout", 45)
                ok = True
                if kind == "subtitle" and st.get("new"):
                    ok = await hud.wait(lambda: any(not b and t not in FILLERS for _, t, b in hud.subtitles[sub_mark:]), timeout)
                elif kind == "subtitle":
                    # A line that is on screen now (it may have started during the previous step) or a new one.
                    ok = await hud.wait(lambda: bool(hud.state.get("subtitle")) and not hud.state.get("breakthrough")
                                        and hud.state.get("subtitle") not in FILLERS, timeout)
                elif kind == "reply":
                    # The main reply: the first real (non-filler, non-second-voice) line spoken after the prompt.
                    ok = await hud.wait(lambda: hud.state.get("mode") == "SPEAKING" and any(
                        not b and txt not in FILLERS for _, txt, b in hud.subtitles[sub_mark:]), timeout)
                elif kind == "breakthrough":
                    ok = await hud.wait(lambda: any(b for _, _, b in hud.subtitles[sub_mark:]) and hud.state.get("breakthrough"), timeout)
                elif kind == "subtitle_after_goal":
                    ok = await hud.wait(lambda: hud.state.get("mode") == "SPEAKING" and hud.state.get("subtitle")
                                        and len(hud.subtitles) > sub_mark and not hud.state.get("breakthrough"), timeout)
                elif kind and kind.startswith("mode:"):
                    want = kind.split(":", 1)[1]
                    ok = await hud.wait(lambda: any(m == want for _, m in hud.modes[mode_mark:]) or hud.state.get("mode") == want, timeout)
                elif kind and kind.startswith("event:"):
                    want = kind.split(":", 1)[1]
                    ok = await hud.wait(lambda: any(want in e for e in hud.events[ev_mark:]), timeout)
                if not ok:
                    print(f"step {i}: '{kind}' did not happen within {timeout}s" + (" (optional)" if st.get("optional") else ""))
                    if st.get("optional"):
                        continue
                await asyncio.sleep(st.get("delay2", 1.3 if kind in ("reply", "subtitle", "subtitle_after_goal") else 0.4))
                if "shot" in st:
                    path = out / f"{st['shot']}.png"
                    on_screen = hud.state.get("subtitle") or ""
                    await page.screenshot(path=str(path))
                    caption = st.get("caption", "").replace("{object}", a.object).replace("{target}", a.target)
                    sub = on_screen  # only what is visible in the screenshot
                    gallery.append({"file": path.name, "said": said, "reply": sub, "caption": caption,
                                    "breakthrough": bool(hud.state.get("breakthrough"))})
                    print(f"  shot {path.name}: {sub[:90]}")
            await asyncio.sleep(2)
            await context.close()
            await browser.close()
            if a.video:
                mux(tmp, video_started, Path(a.video))

        md = ["# Demo screenshots", "",
              "Captured by `scripts/record_demo.py` from the running system. The camera is a set of short",
              "recorded clips of the author; prompts are **typed**, so they skip only the local",
              "speech-recognition step and go through the same brain, model, Ledger and body as speech.",
              "Every reply below is the model's real output from that run.", ""]
        for g in gallery:
            md.append(f"### {g['file'][:-4]}")
            if g["caption"]:
                md.append(g["caption"])
            md.append("")
            if g["said"]:
                md.append(f"> **Visitor:** {g['said']}  ")
            if g["reply"]:
                who = "Second voice" if g["breakthrough"] else "The Architecture"
                md.append(f"> **{who}:** {g['reply']}")
            md.append("")
            md.append(f"![{g['file']}]({g['file']})")
            md.append("")
        (out / "README.md").write_text("\n".join(md))
        print("gallery:", out / "README.md")
    finally:
        proc.terminate()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()
        black.unlink(missing_ok=True)
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)  # raw browser video + audio; the muxed .mp4 is the output


def mux(tmp: Path, video_started: float, dest: Path) -> None:
    """Combine the browser recording with the character's audio, aligned by wall clock."""
    vids = sorted(tmp.glob("*.webm"))
    wav = tmp / "audio.wav"
    if not vids:
        print("no video recorded")
        return
    offset = 0.0
    if (tmp / "audio.wav.start").exists():
        offset = float((tmp / "audio.wav.start").read_text()) - video_started  # >0: audio began after video
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-loglevel", "error", "-y", "-i", str(vids[-1])]
    if wav.exists():
        # Audio usually starts first (the brain boots before the browser): trim its head; else delay it.
        cmd += (["-ss", f"{-offset:.3f}"] if offset < 0 else ["-itsoffset", f"{offset:.3f}"]) + ["-i", str(wav), "-map", "0:v", "-map", "1:a", "-c:a", "aac", "-b:a", "128k"]
    cmd += ["-c:v", "libx264", "-preset", "slow", "-crf", "30", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
            "-shortest", str(dest)]
    subprocess.run(cmd, check=True)
    print("video:", dest)


sys.path.insert(0, str(ROOT))
from architecture.brain.persona import Lines  # noqa: E402

FILLERS = set(Lines(ROOT / "architecture" / "lines.json").lines.get("slow", []))

if __name__ == "__main__":
    asyncio.run(main())
