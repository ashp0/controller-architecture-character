"""Camera capture and attention detection, on the laptop.

YuNet (OpenCV's 230 KB face detector) runs at ~15 Hz on a 320 px frame and
returns five landmarks per face. Whether someone is *looking at* the lamp is
judged from those landmarks: when the nose sits between the eyes and the face
is not tilted far up or down, the head is turned toward the camera.

Only on demand (a conversation turn, a goal) is a single JPEG frame handed
to the language/vision model.
"""
from __future__ import annotations

import base64
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

log = logging.getLogger("vision")


@dataclass
class Attention:
    faces: int = 0
    looking: bool = False
    score: float = 0.0  # smoothed 0..1 "is someone looking at me"
    u: float = 0.5  # face centre, normalized image coords
    v: float = 0.45
    size: float = 0.0  # face width / frame width
    yaw: float = 0.0
    pitch: float = 0.0
    t: float = 0.0


class Vision:
    def __init__(self, source, model: Path, width: int = 960, enabled: bool = True):
        self.source = source
        self.enabled = enabled
        self.width = width
        self.model = model
        self.frame: np.ndarray | None = None
        self.frame_t = 0.0
        self.att = Attention()
        self.boxes: list[tuple] = []  # extra overlay boxes (u0,v0,u1,v1,label) for the HUD
        self.lock = threading.Lock()
        self.fps = 0.0
        self.ok = False

    def start(self) -> None:
        if self.enabled:
            threading.Thread(target=self._run, daemon=True).start()

    def _open(self):
        src = self.source
        if isinstance(src, str) and src.isdigit():
            src = int(src)
        cap = cv2.VideoCapture(src)
        if isinstance(src, int):
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
        return cap

    def _run(self) -> None:
        still = None
        if isinstance(self.source, str) and self.source.lower().endswith((".jpg", ".jpeg", ".png", ".bmp")):
            still = cv2.imread(self.source)  # a photo stands in for the camera (tests, demos)
        cap = None if still is not None else self._open()
        warned = False
        det = None
        last_det = 0.0
        n, t0 = 0, time.monotonic()
        while True:
            if still is not None:
                ok, frame = True, still.copy()
            else:
                ok, frame = cap.read()
            if not ok:
                if isinstance(self.source, str) and not self.source.isdigit():
                    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)  # loop test videos/images
                    ok, frame = cap.read()
                if not ok:
                    if not warned:
                        warned = True
                        log.error("camera %s not readable (on macOS: grant Camera permission to your terminal)", self.source)
                    self.ok = False
                    time.sleep(1.0)
                    cap.release()
                    cap = self._open()
                    continue
            self.ok, warned = True, False
            h, w = frame.shape[:2]
            if w > self.width:
                frame = cv2.resize(frame, (self.width, int(h * self.width / w)))
            with self.lock:
                self.frame, self.frame_t = frame, time.monotonic()
            now = time.monotonic()
            if now - last_det >= 1 / 15:
                last_det = now
                small = cv2.resize(frame, (320, int(320 * frame.shape[0] / frame.shape[1])))
                if det is None:
                    det = cv2.FaceDetectorYN.create(str(self.model), "", (small.shape[1], small.shape[0]), 0.7, 0.3, 50)
                det.setInputSize((small.shape[1], small.shape[0]))
                _, faces = det.detect(small)
                self._update_attention(faces, small.shape[1], small.shape[0], now)
                n += 1
                if now - t0 > 2:
                    self.fps, n, t0 = n / (now - t0), 0, now
            if still is not None or not str(self.source).isdigit():
                time.sleep(1 / 30)

    def _update_attention(self, faces, W: int, H: int, now: float) -> None:
        a = self.att
        looking, best = False, None
        if faces is not None and len(faces):
            best = max(faces, key=lambda f: f[2] * f[3])
            x, y, w, h = best[:4]
            reye, leye, nose = best[4:6], best[6:8], best[8:10]
            mouth = (best[10:12] + best[12:14]) / 2
            eye_mid = (reye + leye) / 2
            eye_d = np.linalg.norm(leye - reye) + 1e-6
            yaw = float((nose[0] - eye_mid[0]) / eye_d)  # ~0 when facing camera
            pitch = float((nose[1] - eye_mid[1]) / (mouth[1] - eye_mid[1] + 1e-6))  # ~0.5..0.6 frontal
            size = float(w / W)
            looking = abs(yaw) < 0.28 and 0.3 < pitch < 0.8 and size > 0.06
            a.u, a.v = float((x + w / 2) / W), float((y + h * 0.4) / H)
            a.size, a.yaw, a.pitch = size, yaw, pitch
        a.faces = 0 if faces is None else len(faces)
        a.looking = looking
        # Exponential smoothing with ~0.4 s time constant: a glance is not engagement.
        dt = now - a.t if a.t else 0.07
        k = 1 - np.exp(-dt / 0.4)
        a.score += ((1.0 if looking else 0.0) - a.score) * k
        a.t = now

    # ---- frames for the model / HUD ------------------------------------
    def snapshot(self) -> np.ndarray | None:
        with self.lock:
            return None if self.frame is None else self.frame.copy()

    def fresh_snapshot(self, after: float, timeout: float = 1.0) -> np.ndarray | None:
        """A frame captured after time ``after`` (used to re-observe after moving)."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            with self.lock:
                if self.frame is not None and self.frame_t > after:
                    return self.frame.copy()
            time.sleep(0.02)
        return self.snapshot()

    @staticmethod
    def jpeg_b64(frame: np.ndarray, width: int = 640, quality: int = 80) -> str:
        h, w = frame.shape[:2]
        if w > width:
            frame = cv2.resize(frame, (width, int(h * width / w)))
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
        return base64.b64encode(buf.tobytes()).decode()

    def thumbnail(self, aim: tuple[float, float] | None = None) -> str | None:
        f = self.snapshot()
        if f is None:
            return None
        f = cv2.resize(f, (320, int(320 * f.shape[0] / f.shape[1])))
        H, W = f.shape[:2]
        a = self.att
        if a.faces:
            col = (80, 200, 255) if a.looking else (120, 120, 120)
            cx, cy, r = int(a.u * W), int(a.v * H), int(a.size * W * 0.6)
            cv2.circle(f, (cx, cy), max(r, 6), col, 1, cv2.LINE_AA)
        for (u0, v0, u1, v1, label) in list(self.boxes):
            cv2.rectangle(f, (int(u0 * W), int(v0 * H)), (int(u1 * W), int(v1 * H)), (60, 200, 255), 1)
            cv2.putText(f, label[:18], (int(u0 * W) + 2, int(v0 * H) + 11), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (60, 200, 255), 1)
        if aim is not None:
            x, y = int(aim[0] * W), int(aim[1] * H)
            cv2.drawMarker(f, (x, y), (255, 255, 255), cv2.MARKER_CROSS, 12, 1)
        return self.jpeg_b64(f, 320, 70)
