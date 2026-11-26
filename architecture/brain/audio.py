"""One output stream for everything the character makes audible.

Music, sound effects and voice are mixed in a single sounddevice callback so
they can duck each other (music drops under speech) and so the brain knows,
sample-accurately, when the character is speaking (for half-duplex listening
and for driving the lamp light from the voice envelope).
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time

import numpy as np

from .soundbank import SR

log = logging.getLogger("audio")


class _Voice:
    def __init__(self, x: np.ndarray, done: asyncio.Future, loop: asyncio.AbstractEventLoop):
        self.x, self.pos, self.done, self.loop = x, 0, done, loop


class Mixer:
    def __init__(self, sounds: dict[str, np.ndarray], device=None, enabled: bool = True):
        self.sounds = sounds
        self.lock = threading.Lock()
        self.music: list[dict] = []  # {"x", "pos", "gain", "target", "rate"}
        self.sfx: list[list] = []  # [x, pos, gain]
        self.voice: list[_Voice] = []
        self.duck = 1.0
        self.duck_target = 1.0
        self.master = 0.9
        self.voice_level = 0.0
        self.last_voice_end = 0.0
        self.stream = None
        if enabled:
            try:
                import sounddevice as sd

                self.stream = sd.OutputStream(samplerate=SR, channels=1, dtype="float32", blocksize=1024,
                                              device=device, callback=self._callback)
                self.stream.start()
            except Exception as e:  # no speaker (CI, container): keep time, stay silent
                log.warning("audio output unavailable (%s); running silent", e)
        if self.stream is None:
            threading.Thread(target=self._null_clock, daemon=True).start()

    # ---- public API -----------------------------------------------------
    def play_music(self, name: str, gain: float = 0.5, fade: float = 2.0) -> None:
        with self.lock:
            for m in self.music:
                m["target"], m["rate"] = 0.0, 1 / (fade * SR)
            self.music.append({"x": self.sounds[name], "pos": 0, "gain": 0.0, "target": gain,
                               "rate": 1 / (fade * SR), "name": name})

    def stop_music(self, fade: float = 2.0) -> None:
        with self.lock:
            for m in self.music:
                m["target"], m["rate"] = 0.0, 1 / (fade * SR)

    def sfx_play(self, name: str, gain: float = 0.8) -> None:
        with self.lock:
            self.sfx.append([self.sounds[name], 0, gain])

    def set_duck(self, level: float) -> None:
        self.duck_target = level

    def say(self, audio: np.ndarray) -> asyncio.Future:
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        with self.lock:
            self.voice.append(_Voice(audio.astype(np.float32), fut, loop))
        return fut

    def stop_voice(self) -> None:
        with self.lock:
            for v in self.voice:
                v.loop.call_soon_threadsafe(lambda f=v.done: f.done() or f.set_result(False))
            self.voice.clear()

    @property
    def speaking(self) -> bool:
        return bool(self.voice) or time.monotonic() - self.last_voice_end < 0.35

    # ---- mixing ---------------------------------------------------------
    def _mix(self, n: int) -> np.ndarray:
        out = np.zeros(n, np.float32)
        with self.lock:
            self.duck += (self.duck_target - self.duck) * min(1.0, n / (0.25 * SR))
            keep = []
            for m in self.music:
                x, L = m["x"], len(m["x"])
                idx = (m["pos"] + np.arange(n)) % L
                step = m["rate"] * n
                g0 = m["gain"]
                g1 = g0 + max(-step, min(step, m["target"] - g0))
                out += x[idx] * np.linspace(g0, g1, n, dtype=np.float32) * self.duck
                m["pos"], m["gain"] = (m["pos"] + n) % L, g1
                if g1 > 1e-4 or m["target"] > 0:
                    keep.append(m)
            self.music = keep
            keep = []
            for s in self.sfx:
                x, p, g = s
                chunk = x[p:p + n]
                out[: len(chunk)] += chunk * g
                s[1] += n
                if s[1] < len(x):
                    keep.append(s)
            self.sfx = keep
            level = 0.0
            filled = 0
            while self.voice and filled < n:
                v = self.voice[0]
                chunk = v.x[v.pos:v.pos + n - filled]
                out[filled:filled + len(chunk)] += chunk
                level = max(level, float(np.sqrt(np.mean(chunk ** 2))) if len(chunk) else 0.0)
                filled += len(chunk)
                v.pos += len(chunk)
                if v.pos >= len(v.x):
                    self.voice.pop(0)
                    self.last_voice_end = time.monotonic()
                    v.loop.call_soon_threadsafe(lambda f=v.done: f.done() or f.set_result(True))
            self.voice_level = level
        return np.tanh(out * self.master)

    def _callback(self, outdata, frames, _time, _status) -> None:
        outdata[:, 0] = self._mix(frames)

    def _null_clock(self) -> None:
        while True:
            self._mix(1024)
            time.sleep(1024 / SR)
