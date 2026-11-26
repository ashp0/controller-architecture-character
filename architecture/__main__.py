"""Launch The Architecture: the body (simulator + viewer) and the brain.

    python -m architecture            # camera 0, default mic and speaker
    python -m architecture --help
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _wait_port(host: str, port: int, timeout: float = 15) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        with socket.socket() as s:
            if s.connect_ex((host, port)) == 0:
                return True
        time.sleep(0.1)
    return False


def main() -> None:
    p = argparse.ArgumentParser(prog="architecture", description="The Architecture: a live lamp character")
    p.add_argument("--camera", default=os.environ.get("CAMERA", "0"), help="camera index or a video/image file")
    p.add_argument("--no-camera", action="store_true", help="run blind (typed/spoken interaction only)")
    p.add_argument("--mic", default=os.environ.get("MIC") or None, help="input device index or name")
    p.add_argument("--speaker", default=os.environ.get("SPEAKER") or None, help="output device index or name")
    p.add_argument("--no-mic", action="store_true", help="no microphone; use typed text or push-to-talk")
    p.add_argument("--silent", action="store_true", help="no audio output")
    p.add_argument("--whisper", default=os.environ.get("WHISPER_MODEL", "base.en"), help="tiny.en | base.en | small.en")
    p.add_argument("--model", default=None, help="OpenCode Go model id (default: $LLM_MODEL or glm-5.3-flash)")
    p.add_argument("--keep-memory", action="store_true", help="load the Ledger from the previous run")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-browser", action="store_true", help="don't open the viewer automatically")
    p.add_argument("--brain-only", action="store_true", help="connect to an already running body")
    a = p.parse_args()
    for k in ("mic", "speaker"):
        v = getattr(a, k)
        if isinstance(v, str) and v.isdigit():
            setattr(a, k, int(v))
    a.root = str(ROOT)

    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
    except ImportError:
        pass
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)-7s %(message)s", datefmt="%H:%M:%S")
    for noisy in ("httpx", "faster_whisper", "aiohttp.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    body = None
    if not a.brain_only:
        body = subprocess.Popen([sys.executable, "-m", "architecture.body.server", "--host", a.host, "--port", str(a.port)],
                                cwd=ROOT)
        if not _wait_port(a.host, a.port):
            body.terminate()
            sys.exit("body process did not start")
        a.body_pid = body.pid
    else:
        a.body_pid = None
    if not a.no_browser:
        webbrowser.open(f"http://{a.host}:{a.port}/")

    from .brain.main import Brain

    try:
        asyncio.run(Brain(a).run())
    except KeyboardInterrupt:
        pass
    finally:
        if body:
            body.terminate()
            body.wait(5)


if __name__ == "__main__":
    main()
