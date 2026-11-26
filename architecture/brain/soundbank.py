"""Procedurally synthesized music and sound effects.

Everything is generated from code at first start and cached as WAV, so there
are no licensing questions and nothing to download. The musical idea: a
music-box lullaby, slightly out of tune, over a low drone. Pacification,
dressed as comfort.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import soundfile as sf

SR = 44100
rng = np.random.default_rng(7)


def _t(sec: float) -> np.ndarray:
    return np.arange(int(sec * SR)) / SR


def _hz(midi: float) -> float:
    return 440.0 * 2 ** ((midi - 69) / 12)


def _env(n: int, a: float, d: float) -> np.ndarray:
    t = np.arange(n) / SR
    return np.minimum(1, t / max(a, 1e-4)) * np.exp(-t / d)


def _reverb(x: np.ndarray, sec: float = 2.8, mix: float = 0.35) -> np.ndarray:
    n = int(sec * SR)
    ir = rng.standard_normal(n) * np.exp(-np.arange(n) / SR * 6.9 / sec)
    ir[: int(0.02 * SR)] = 0
    L = len(x) + n
    nfft = 1 << (L - 1).bit_length()
    wet = np.fft.irfft(np.fft.rfft(x, nfft) * np.fft.rfft(ir, nfft), nfft)[:L]
    wet /= np.max(np.abs(wet)) + 1e-9
    out = np.zeros(L)
    out[: len(x)] += x * (1 - mix)
    out += wet * mix * (np.max(np.abs(x)) + 1e-9)
    return out




def _bell(f: float, dur: float, amp: float = 1.0) -> np.ndarray:
    t = _t(dur)
    partials = [(1, 1.0, 1.0), (2.76, 0.45, 0.55), (5.4, 0.25, 0.3), (8.93, 0.12, 0.18)]
    x = sum(a * np.sin(2 * np.pi * f * r * t) * np.exp(-t / (dur * d)) for r, a, d in partials)
    return amp * x * np.minimum(1, t / 0.003)


def _musicbox(f: float, dur: float = 1.6) -> np.ndarray:
    t = _t(dur)
    wob = 1 + 0.004 * np.sin(2 * np.pi * 5.5 * t)  # worn-out mechanism
    x = np.sin(2 * np.pi * f * wob * t) + 0.3 * np.sin(2 * np.pi * 2 * f * t) * np.exp(-t * 6) + 0.15 * np.sin(2 * np.pi * 4.02 * f * t) * np.exp(-t * 10)
    return x * np.exp(-t * 2.6) * np.minimum(1, t / 0.002)


def _drone(sec: float, roots=(38, 45, 50), amp: float = 0.25) -> np.ndarray:
    t = _t(sec)
    x = np.zeros_like(t)
    for i, m in enumerate(roots):
        f = _hz(m)
        for det in (-0.12, 0.0, 0.11):
            ph = rng.uniform(0, 2 * np.pi)
            x += np.sin(2 * np.pi * f * (1 + det / 100) * t + ph) / (1 + i)
    lfo = 0.6 + 0.4 * np.sin(2 * np.pi * t / 11.0)
    return amp * x / np.max(np.abs(x)) * lfo


def _place(buf: np.ndarray, x: np.ndarray, at: float) -> None:
    i = int(at * SR)
    j = min(len(buf), i + len(x))
    buf[i:j] += x[: j - i]


def _loopable(x: np.ndarray, sec: float) -> np.ndarray:
    """Fold the reverb tail back onto the start so the loop is seamless."""
    n = int(sec * SR)
    out = x[:n].copy()
    tail = x[n:]
    out[: len(tail)] += tail[:n]
    return out


def theme(sec: float = 32.0) -> np.ndarray:
    # An original, simple lullaby phrase in D minor: four bars, repeated with a turn.
    bpm = 60
    beat = 60 / bpm
    phrase = [(74, 0), (72, 1), (69, 1.5), (70, 2), (69, 3), (65, 3.5),
              (67, 4), (65, 5), (62, 5.5), (64, 6), (65, 7),
              (74, 8), (72, 9), (69, 9.5), (70, 10), (69, 11), (67, 11.5),
              (65, 12), (64, 13), (61, 13.5), (62, 14)]
    buf = np.zeros(int((sec + 4) * SR))
    for rep in range(2):
        for m, b in phrase:
            detune = rng.normal(0, 0.08)  # never quite in tune
            _place(buf, 0.28 * _musicbox(_hz(m + detune)), rep * 16 * beat + b * beat)
    bass = [(50, 0), (46, 4), (43, 8), (45, 12)]
    for rep in range(2):
        for m, b in bass:
            _place(buf, 0.18 * _bell(_hz(m - 12), 4.0), rep * 16 * beat + b * beat)
    buf = _reverb(buf, 3.0, 0.45)[: len(buf)]
    buf[: int(sec * SR)] += _drone(sec, amp=0.18)
    return _loopable(buf, sec)


def dormant(sec: float = 24.0) -> np.ndarray:
    x = _drone(sec + 3, roots=(26, 33, 38), amp=0.16)
    return _loopable(x, sec)


def sfx_awaken() -> np.ndarray:
    t = _t(2.4)
    sweep = np.sin(2 * np.pi * np.cumsum(60 + 140 * (t / t[-1]) ** 2) / SR) * (t / t[-1]) ** 2
    noise = rng.standard_normal(len(t)) * (t / t[-1]) ** 3 * 0.15
    x = 0.5 * sweep + noise
    x[-int(0.05 * SR):] *= np.linspace(1, 0, int(0.05 * SR))
    bell = np.concatenate([np.zeros(int(2.2 * SR)), _bell(_hz(62), 3.0) + 0.6 * _bell(_hz(69), 3.0)])
    out = np.zeros(len(bell))
    out[: len(x)] += x
    out += 0.6 * bell
    return _reverb(out, 3.0, 0.4)


def sfx_ack() -> np.ndarray:
    x = np.zeros(int(1.4 * SR))
    _place(x, _bell(_hz(81), 1.2, 0.5), 0)
    _place(x, _bell(_hz(86), 1.2, 0.4), 0.12)
    return _reverb(x, 1.5, 0.3)


def sfx_think() -> np.ndarray:
    # A clockwork: dry mechanical ticks, the world as machine.
    x = np.zeros(int(1.6 * SR))
    for i in range(8):
        click = rng.standard_normal(int(0.012 * SR)) * np.exp(-np.arange(int(0.012 * SR)) / (0.002 * SR))
        click = np.convolve(click, np.ones(6) / 6, "same")
        _place(x, click * (0.5 if i % 2 else 0.8), i * 0.2)
    return x * 0.6


def sfx_complete() -> np.ndarray:
    x = np.zeros(int(3.0 * SR))
    for k, m in enumerate((62, 65, 69, 74)):
        _place(x, _bell(_hz(m), 2.5, 0.35), k * 0.07)
    return _reverb(x, 2.5, 0.4)


def sfx_dismiss() -> np.ndarray:
    t = _t(1.8)
    f = 220 * np.exp(-t * 1.2)
    x = np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t * 1.5) * 0.5
    return _reverb(x, 2.0, 0.45)


def sfx_glitch() -> np.ndarray:
    x = np.zeros(int(0.7 * SR))
    for i in range(7):
        seg = np.sign(np.sin(2 * np.pi * rng.uniform(300, 1800) * _t(0.04))) * 0.25
        _place(x, seg, i * 0.08 + rng.uniform(0, 0.03))
    return x


SOUNDS = {
    "music_theme": theme,
    "music_dormant": dormant,
    "awaken": sfx_awaken,
    "ack": sfx_ack,
    "think": sfx_think,
    "complete": sfx_complete,
    "dismiss": sfx_dismiss,
    "glitch": sfx_glitch,
}


def load_all(cache: Path) -> dict[str, np.ndarray]:
    cache.mkdir(parents=True, exist_ok=True)
    out = {}
    for name, fn in SOUNDS.items():
        p = cache / f"{name}.wav"
        if not p.exists():
            x = fn().astype(np.float32)
            x /= max(1.0, float(np.max(np.abs(x))) / 0.9)
            sf.write(p, x, SR)
        out[name], _ = sf.read(p, dtype="float32")
    return out
