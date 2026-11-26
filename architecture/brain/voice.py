"""Local text-to-speech (Piper), shaped into The Architecture's voice.

Piper runs on CPU in a fraction of real time, so speech never waits on the
network. The voice is pitched down by resampling and given a short room
reverb, so it sounds like it comes from somewhere larger than a lamp.
"""
from __future__ import annotations

import re
from pathlib import Path

import numpy as np

from .soundbank import SR, _reverb


class Voice:
    def __init__(self, model: Path, pitch: float = 0.86, speed: float = 1.0):
        from piper import PiperVoice, SynthesisConfig

        self.v = PiperVoice.load(str(model))
        # Resampling lowers pitch AND slows speech, so ask Piper to speak faster to compensate.
        self.cfg = SynthesisConfig(length_scale=pitch / speed * 1.04, noise_scale=0.5, noise_w_scale=0.7)
        self.cfg_break = SynthesisConfig(length_scale=1.18, noise_scale=0.3, noise_w_scale=0.3)
        self.pitch = pitch
        self.cache: dict[str, np.ndarray] = {}

    def synth(self, text: str, style: str = "calm") -> np.ndarray:
        key = f"{style}:{text}"
        if key in self.cache:
            return self.cache[key]
        if style == "break":
            y = self._break(text)
            self.cache[key] = y
            return y
        parts, sr = [], 22050
        for ch in self.v.synthesize(text, self.cfg):
            parts.append(ch.audio_float_array)
            sr = ch.sample_rate
            parts.append(np.zeros(int(0.12 * sr), np.float32))  # breath between sentences
        if not parts:
            return np.zeros(1, np.float32)
        x = np.concatenate(parts)
        # Play the 22.05 kHz voice slower than recorded => lower pitch.
        n_out = int(len(x) * SR / (sr * self.pitch))
        y = np.interp(np.linspace(0, len(x) - 1, n_out), np.arange(len(x)), x).astype(np.float32)
        y = _reverb(y, 1.1, 0.16).astype(np.float32)
        y *= 0.85 / (np.max(np.abs(y)) + 1e-6)
        if len(self.cache) < 200:
            self.cache[key] = y
        return y

    def _break(self, text: str) -> np.ndarray:
        """The second voice: unprocessed pitch, doubled an octave down, crushed, close and loud."""
        parts, sr = [], 22050
        for ch in self.v.synthesize(text.capitalize(), self.cfg_break):
            parts.append(ch.audio_float_array)
            sr = ch.sample_rate
        x = np.concatenate(parts) if parts else np.zeros(1, np.float32)
        n = int(len(x) * SR / sr)
        hi = np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x)
        lo = np.interp(np.linspace(0, len(x) - 1, n) * 0.5, np.arange(len(x)), x)  # octave-down shadow
        lo = np.concatenate([np.zeros(int(0.012 * SR)), lo])[:n]
        y = hi + 0.7 * lo
        y = np.round(y * 10) / 10  # crush
        y = np.tanh(2.2 * y)
        y = _reverb(y, 0.6, 0.12)
        return (0.95 * y / (np.max(np.abs(y)) + 1e-6)).astype(np.float32)


def sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?…])\s+", text) if s.strip()]
