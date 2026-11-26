"""Joint-level motion and light control for the simulated lamp.

The brain never sets raw joint angles. It asks for a posture, a gaze target,
a named gesture, or a light state; this module turns those into joint targets
and moves toward them under the URDF's soft position limits, its velocity
limits, and an acceleration limit, the same constraints real servos would have.

Layers, summed then clamped each tick:
    posture (shoulder/elbow/head base) + gaze (yaw/neck/head from look-at IK)
    + gesture offsets (keyframed) + breathing (small idle oscillation)
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

import numpy as np

from ..urdf import Robot

JOINTS = ["base_yaw_joint", "shoulder_pitch_joint", "elbow_pitch_joint", "neck_yaw_joint", "head_pitch_joint"]

# Arm postures (shoulder, elbow, head-pitch bias). Values chosen inside the soft limits.
POSTURES: dict[str, dict[str, float]] = {
    "sleep":     {"shoulder_pitch_joint": 0.55, "elbow_pitch_joint": -1.55, "head_pitch_joint": 0.55},
    "idle":      {"shoulder_pitch_joint": 0.35, "elbow_pitch_joint": -1.00, "head_pitch_joint": 0.0},
    "attentive": {"shoulder_pitch_joint": 0.10, "elbow_pitch_joint": -0.70, "head_pitch_joint": 0.0},
    "loom":      {"shoulder_pitch_joint": -0.45, "elbow_pitch_joint": -0.35, "head_pitch_joint": 0.0},
}

# Gestures: keyframes of (time s, {joint: offset rad}); linearly interpolated, smoothstepped.
GESTURES: dict[str, list[tuple[float, dict[str, float]]]] = {
    "nod": [(0, {}), (0.25, {"head_pitch_joint": 0.28}), (0.5, {}), (0.75, {"head_pitch_joint": 0.2}), (1.05, {})],
    "shake": [(0, {}), (0.3, {"neck_yaw_joint": 0.3}), (0.7, {"neck_yaw_joint": -0.3}), (1.1, {"neck_yaw_joint": 0.2}), (1.5, {})],
    "recoil": [(0, {}), (0.3, {"shoulder_pitch_joint": 0.3, "head_pitch_joint": -0.25}), (1.4, {"shoulder_pitch_joint": 0.15}), (2.2, {})],
    "bow": [(0, {}), (0.9, {"shoulder_pitch_joint": -0.3, "head_pitch_joint": 0.45}), (1.8, {"shoulder_pitch_joint": -0.3, "head_pitch_joint": 0.45}), (2.8, {})],
    "survey": [(0, {}), (1.2, {"neck_yaw_joint": 0.6, "head_pitch_joint": 0.1}), (2.8, {"neck_yaw_joint": -0.6, "head_pitch_joint": 0.1}), (4.0, {})],
    "ponder": [(0, {}), (0.8, {"neck_yaw_joint": 0.18, "head_pitch_joint": -0.2}), (2.4, {"neck_yaw_joint": 0.22, "head_pitch_joint": -0.24}), (3.2, {})],
    "awaken": [(0, {}), (0.4, {"head_pitch_joint": 0.15}), (0.9, {"head_pitch_joint": -0.3, "shoulder_pitch_joint": 0.1}), (1.8, {})],
    "focus": [(0, {}), (0.35, {"shoulder_pitch_joint": -0.15, "head_pitch_joint": 0.08}), (1.4, {"shoulder_pitch_joint": -0.15}), (2.0, {})],
}

MAX_ACCEL = 4.0  # rad/s^2, conservative for small hobby-servo class actuators


def _smooth(x: float) -> float:
    return x * x * (3 - 2 * x)


@dataclass
class Gesture:
    name: str
    frames: list
    start: float
    gid: str | None = None

    @property
    def duration(self) -> float:
        return self.frames[-1][0]

    def offsets(self, now: float) -> dict[str, float] | None:
        t = now - self.start
        if t >= self.duration:
            return None
        for (t0, a), (t1, b) in zip(self.frames, self.frames[1:]):
            if t0 <= t <= t1:
                s = _smooth((t - t0) / max(t1 - t0, 1e-6))
                return {j: a.get(j, 0) * (1 - s) + b.get(j, 0) * s for j in set(a) | set(b)}
        return {}


@dataclass
class Light:
    color: list = field(default_factory=lambda: [1.0, 0.82, 0.45])
    intensity: float = 0.0
    beam: float = 0.4
    pulse: float = 0.0  # Hz, 0 = steady


class CameraModel:
    """The laptop camera, treated as if it were the lamp's own eye.

    Maps normalized image coordinates (u right, v down, raw unmirrored frame)
    to a world point in front of the lamp, at an assumed viewing distance.
    The lamp faces -x; the camera faces the person, so image-right is +y.
    """

    def __init__(self, hfov_deg: float = 62.0, aspect: float = 16 / 9, distance: float = 1.1,
                 eye=(-0.25, 0.0, 0.5)):
        self.tx = math.tan(math.radians(hfov_deg) / 2)
        self.ty = self.tx / aspect
        self.distance = distance
        self.eye = np.array(eye)

    def point(self, u: float, v: float, distance: float | None = None) -> np.ndarray:
        d = np.array([-1.0, (u - 0.5) * 2 * self.tx, -(v - 0.5) * 2 * self.ty])
        return self.eye + (distance or self.distance) * d / np.linalg.norm(d)

    def project(self, origin: np.ndarray, direction: np.ndarray) -> tuple[float, float]:
        """Where a ray from the lamp head meets the viewing sphere, in image coords."""
        rel_o = origin - self.eye
        b = rel_o @ direction
        c = rel_o @ rel_o - self.distance ** 2
        t = -b + math.sqrt(max(b * b - c, 0.0))
        p = rel_o + t * direction
        if p[0] >= -1e-6:
            return 0.5, 0.5
        u = 0.5 + (p[1] / -p[0]) / (2 * self.tx)
        v = 0.5 - (p[2] / -p[0]) / (2 * self.ty)
        return float(u), float(v)


class Body:
    def __init__(self, robot: Robot):
        self.robot = robot
        self.cam = CameraModel()
        self.q = {j: 0.0 for j in JOINTS}
        self.q.update(POSTURES["sleep"])
        self.dq = {j: 0.0 for j in JOINTS}
        self.posture = "sleep"
        self.speed = 0.6
        self.gaze = {"base_yaw_joint": 0.0, "neck_yaw_joint": 0.0, "head_pitch_joint": 0.0}
        self.gaze_target: np.ndarray | None = None
        self.gestures: list[Gesture] = []
        self.light = Light()
        self.light_target = Light()
        self.light_fade = 0.6
        self.breath = True
        self.finished: list[str] = []
        self._last = time.monotonic()

    # ---- commands -------------------------------------------------------
    def set_posture(self, name: str, speed: float = 0.6) -> None:
        if name in POSTURES:
            self.posture, self.speed = name, max(0.1, min(1.0, speed))

    def look_uv(self, u: float, v: float) -> None:
        self.gaze_target = self.cam.point(u, v)

    def look_home(self) -> None:
        self.gaze_target = None

    def gesture(self, name: str, gid: str | None = None) -> bool:
        if name not in GESTURES:
            return False
        self.gestures = [g for g in self.gestures if g.name != name]
        self.gestures.append(Gesture(name, GESTURES[name], time.monotonic(), gid))
        return True

    def set_light(self, color=None, intensity=None, beam=None, pulse=None, fade=None) -> None:
        t = self.light_target
        if color is not None:
            t.color = [max(0.0, min(1.0, float(c))) for c in color][:3]
        if intensity is not None:
            t.intensity = max(0.0, min(1.0, float(intensity)))
        if beam is not None:
            t.beam = max(0.0, min(1.0, float(beam)))
        if pulse is not None:
            t.pulse = max(0.0, min(4.0, float(pulse)))
        if fade is not None:
            self.light_fade = max(0.05, float(fade))

    # ---- look-at IK -----------------------------------------------------
    def _aim_residual(self, gg: dict[str, float], base: dict[str, float], target: np.ndarray) -> np.ndarray:
        q = dict(base)
        for k, v in gg.items():
            q[k] = q.get(k, 0) + v
        T = self.robot.frame("light_emitter_link", self.robot.clamp(q))
        want = target - T[:3, 3]
        return T[:3, 0] - want / np.linalg.norm(want)

    def _solve_gaze(self, base: dict[str, float]) -> None:
        """Damped least-squares look-at on (base_yaw, neck_yaw, head_pitch).

        Residual = head forward axis minus unit vector to the target. An extra
        regularization row keeps the neck near centre, so large turns migrate
        to the base the way a creature turns its body after its eyes.
        """
        if self.gaze_target is None:
            for k in self.gaze:
                self.gaze[k] *= 0.9
            return
        keys = list(self.gaze)
        g = dict(self.gaze)
        reg = np.array([0.0, 0.35, 0.0])
        lam = 0.05
        for _ in range(3):
            r = self._aim_residual(g, base, self.gaze_target)
            J = np.zeros((3, 3))
            for i, k in enumerate(keys):
                gg = dict(g)
                gg[k] += 1e-4
                J[:, i] = (self._aim_residual(gg, base, self.gaze_target) - r) / 1e-4
            x = np.array([g[k] for k in keys])
            A = np.vstack([J, np.diag(reg)])
            b = np.concatenate([-r, -reg * x])
            dx = np.linalg.solve(A.T @ A + lam * np.eye(3), A.T @ b)
            for i, k in enumerate(keys):
                lo, hi = self.robot.limits(k)
                g[k] = min(hi - base.get(k, 0), max(lo - base.get(k, 0), g[k] + float(dx[i])))
        self.gaze = g

    # ---- tick -----------------------------------------------------------
    def step(self) -> None:
        now = time.monotonic()
        dt = min(0.05, now - self._last)
        self._last = now

        base = {j: 0.0 for j in JOINTS}
        base.update(POSTURES[self.posture])
        self._solve_gaze(base)
        target = dict(base)
        for k, v in self.gaze.items():
            target[k] += v
        alive = []
        for g in self.gestures:
            off = g.offsets(now)
            if off is None:
                if g.gid:
                    self.finished.append(g.gid)
                continue
            alive.append(g)
            for k, v in off.items():
                target[k] += v
        self.gestures = alive
        if self.breath:
            amp = 0.012 if self.posture == "sleep" else 0.02
            target["shoulder_pitch_joint"] += amp * math.sin(now * 2 * math.pi / 4.5)
            target["head_pitch_joint"] += amp * 0.8 * math.sin(now * 2 * math.pi / 4.5 + 0.8)
        target = self.robot.clamp(target)

        # Velocity- and acceleration-limited tracking (time-optimal braking).
        for j in JOINTS:
            vmax = self.robot.joints[j].velocity * self.speed
            err = target[j] - self.q[j]
            v_brake = math.copysign(math.sqrt(2 * MAX_ACCEL * abs(err)), err)
            v_want = max(-vmax, min(vmax, v_brake))
            dv = max(-MAX_ACCEL * dt, min(MAX_ACCEL * dt, v_want - self.dq[j]))
            self.dq[j] += dv
            self.q[j] += self.dq[j] * dt
            lo, hi = self.robot.limits(j, soft=False)
            if not lo <= self.q[j] <= hi:
                self.q[j] = min(hi, max(lo, self.q[j]))
                self.dq[j] = 0.0

        # Light fades exponentially toward its target.
        k = 1 - math.exp(-dt / self.light_fade)
        L, T = self.light, self.light_target
        L.color = [a + (b - a) * k for a, b in zip(L.color, T.color)]
        L.intensity += (T.intensity - L.intensity) * k
        L.beam += (T.beam - L.beam) * k
        L.pulse = T.pulse

    # ---- state ----------------------------------------------------------
    def aim_uv(self) -> tuple[float, float]:
        T = self.robot.frame("light_emitter_link", self.q)
        return self.cam.project(T[:3, 3], T[:3, 0])

    def settled(self) -> bool:
        return not self.gestures and all(abs(v) < 0.03 for v in self.dq.values())

    def state(self) -> dict:
        L = self.light
        inten = L.intensity
        if L.pulse > 0:
            inten *= 0.75 + 0.25 * math.sin(time.monotonic() * 2 * math.pi * L.pulse)
        u, v = self.aim_uv()
        return {
            "type": "state",
            "t": time.time(),
            "joints": {k: round(v, 4) for k, v in self.q.items()},
            "light": {"color": [round(c, 3) for c in L.color], "intensity": round(inten, 3), "beam": round(L.beam, 3)},
            "aim": {"u": round(u, 3), "v": round(v, 3)},
            "settled": self.settled(),
            "posture": self.posture,
        }
