"""Microphone → utterances → text, all on the laptop.

Speech detection is WebRTC VAD plus an adaptive energy gate; recognition is
faster-whisper (base.en, int8) on CPU. Audio never leaves the machine; only
the transcript is passed on. Listening is half-duplex: frames are dropped
while the character is speaking, so it does not hear itself through the
laptop speakers.
"""
from __future__ import annotations

import asyncio
import collections
import logging
import queue
import threading
import time
from pathlib import Path

import numpy as np

log = logging.getLogger("hearing")

RATE = 16000
FRAME = 480  # 30 ms
HALLUCINATIONS = {"", "you", "thank you.", "thanks for watching!", "thank you for watching.", "bye.", ".", "uh", "um"}


class Hearing:
    def __init__(self, model_dir: Path, model: str = "base.en", device=None, enabled: bool = True,
                 is_muted=lambda: False):
        self.q: asyncio.Queue | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.model_name, self.model_dir = model, model_dir
        self.whisper = None
        self.device = device
        self.enabled = enabled
        self.listening = False  # set by the behaviour layer (engaged)
        self.ptt = False
        self.is_muted = is_muted
        self.level = 0.0
        self.speech_active = False
        self._frames: queue.Queue = queue.Queue(maxsize=400)

    def load(self) -> None:
        from faster_whisper import WhisperModel

        self.whisper = WhisperModel(self.model_name, device="cpu", compute_type="int8",
                                    download_root=str(self.model_dir), cpu_threads=2)
        # Warm-up so the first real utterance is not slow.
        self.whisper.transcribe(np.zeros(RATE, np.float32), language="en", beam_size=1)

    def start(self, loop: asyncio.AbstractEventLoop) -> asyncio.Queue:
        self.loop, self.q = loop, asyncio.Queue()
        if self.enabled:
            threading.Thread(target=self._capture, daemon=True).start()
            threading.Thread(target=self._segment, daemon=True).start()
        return self.q

    def _capture(self) -> None:
        try:
            import sounddevice as sd

            def cb(indata, frames, _t, _s):
                try:
                    self._frames.put_nowait(indata[:, 0].copy())
                except queue.Full:
                    pass

            with sd.InputStream(samplerate=RATE, channels=1, dtype="float32", blocksize=FRAME,
                                device=self.device, callback=cb):
                while True:
                    time.sleep(1)
        except Exception as e:
            log.error("microphone unavailable: %s (type into the viewer instead)", e)

    def _segment(self) -> None:
        import webrtcvad

        vad = webrtcvad.Vad(2)
        floor = 0.004
        ring = collections.deque(maxlen=10)
        pre = collections.deque(maxlen=10)  # 300 ms of audio before speech onset
        buf: list[np.ndarray] = []
        in_speech, silence, was_ptt = False, 0, False
        while True:
            f = self._frames.get()
            rms = float(np.sqrt(np.mean(f ** 2)))
            self.level = rms
            if self.is_muted():
                buf, in_speech, silence = [], False, 0
                ring.clear()
                pre.clear()
                continue
            # Push-to-talk: record exactly while the key is held.
            if self.ptt or was_ptt:
                if self.ptt:
                    buf.append(f)
                    was_ptt = True
                    self.speech_active = True
                    continue
                was_ptt, self.speech_active = False, False
                self._emit(np.concatenate(buf) if buf else np.zeros(1), forced=True)
                buf = []
                continue
            if not self.listening:
                in_speech, buf = False, []
                floor = 0.98 * floor + 0.02 * rms
                continue
            pcm = (np.clip(f, -1, 1) * 32767).astype(np.int16).tobytes()
            voiced = vad.is_speech(pcm, RATE) and rms > max(0.006, floor * 2.5)
            ring.append(voiced)
            if not in_speech:
                pre.append(f)
                floor = 0.995 * floor + 0.005 * rms if not voiced else floor
                if sum(ring) >= 6:
                    in_speech, silence = True, 0
                    buf = list(pre)
                    self.speech_active = True
                continue
            buf.append(f)
            silence = 0 if voiced else silence + 1
            dur = len(buf) * FRAME / RATE
            if silence > 25 or dur > 15:  # 750 ms of quiet ends the utterance
                in_speech, self.speech_active = False, False
                ring.clear()
                pre.clear()
                self._emit(np.concatenate(buf))
                buf = []

    def _emit(self, audio: np.ndarray, forced: bool = False) -> None:
        if len(audio) < RATE * 0.35:
            return
        t_end = time.monotonic()
        text, ms = self.transcribe(audio)
        if text.lower().strip() in HALLUCINATIONS:
            return
        self.loop.call_soon_threadsafe(self.q.put_nowait, {"text": text, "stt_ms": ms, "t_end": t_end,
                                                            "audio_s": len(audio) / RATE})

    def transcribe(self, audio: np.ndarray) -> tuple[str, float]:
        t = time.perf_counter()
        segs, _info = self.whisper.transcribe(audio, language="en", beam_size=1, vad_filter=False,
                                              condition_on_previous_text=False, without_timestamps=True)
        good = [s.text for s in segs if s.no_speech_prob < 0.6 and s.avg_logprob > -1.1]
        return " ".join(good).strip(), (time.perf_counter() - t) * 1000
