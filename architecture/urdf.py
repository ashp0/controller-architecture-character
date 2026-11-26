"""Minimal URDF reader and forward kinematics for the supplied lamp.

Only what the body needs: joint limits (position, velocity) and the pose of
named frames, so look-at targets can be solved against the real geometry.
"""
from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np


def _rpy_matrix(r: float, p: float, y: float) -> np.ndarray:
    cr, sr, cp, sp, cy, sy = math.cos(r), math.sin(r), math.cos(p), math.sin(p), math.cos(y), math.sin(y)
    return np.array([
        [cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr],
        [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr],
        [-sp, cp * sr, cp * cr],
    ])


def _axis_angle(axis: np.ndarray, a: float) -> np.ndarray:
    x, y, z = axis / np.linalg.norm(axis)
    c, s, C = math.cos(a), math.sin(a), 1 - math.cos(a)
    return np.array([
        [c + x * x * C, x * y * C - z * s, x * z * C + y * s],
        [y * x * C + z * s, c + y * y * C, y * z * C - x * s],
        [z * x * C - y * s, z * y * C + x * s, c + z * z * C],
    ])


@dataclass
class Joint:
    name: str
    type: str
    parent: str
    child: str
    origin: np.ndarray  # 4x4
    axis: np.ndarray = field(default_factory=lambda: np.array([1.0, 0, 0]))
    lower: float = -math.inf
    upper: float = math.inf
    velocity: float = math.inf
    soft_lower: float | None = None
    soft_upper: float | None = None


class Robot:
    def __init__(self, path: str | Path):
        root = ET.parse(path).getroot()
        self.name = root.get("name")
        self.joints: dict[str, Joint] = {}
        for j in root.findall("joint"):
            o = j.find("origin")
            xyz = [float(v) for v in (o.get("xyz", "0 0 0") if o is not None else "0 0 0").split()]
            rpy = [float(v) for v in (o.get("rpy", "0 0 0") if o is not None else "0 0 0").split()]
            T = np.eye(4)
            T[:3, :3] = _rpy_matrix(*rpy)
            T[:3, 3] = xyz
            jt = Joint(j.get("name"), j.get("type"), j.find("parent").get("link"), j.find("child").get("link"), T)
            if (a := j.find("axis")) is not None:
                jt.axis = np.array([float(v) for v in a.get("xyz").split()])
            if (lim := j.find("limit")) is not None:
                jt.lower, jt.upper = float(lim.get("lower")), float(lim.get("upper"))
                jt.velocity = float(lim.get("velocity"))
            if (sc := j.find("safety_controller")) is not None:
                jt.soft_lower = float(sc.get("soft_lower_limit"))
                jt.soft_upper = float(sc.get("soft_upper_limit"))
            self.joints[jt.name] = jt
        self.by_child = {j.child: j for j in self.joints.values()}
        self.actuated = [n for n, j in self.joints.items() if j.type == "revolute"]

    def limits(self, name: str, soft: bool = True) -> tuple[float, float]:
        j = self.joints[name]
        if soft and j.soft_lower is not None:
            return j.soft_lower, j.soft_upper
        return j.lower, j.upper

    def clamp(self, q: dict[str, float]) -> dict[str, float]:
        out = {}
        for n, v in q.items():
            lo, hi = self.limits(n)
            out[n] = min(hi, max(lo, v))
        return out

    def frame(self, link: str, q: dict[str, float]) -> np.ndarray:
        """World (base_link) transform of ``link`` for joint positions ``q``."""
        chain = []
        while link in self.by_child:
            j = self.by_child[link]
            chain.append(j)
            link = j.parent
        T = np.eye(4)
        for j in reversed(chain):
            T = T @ j.origin
            if j.type == "revolute":
                R = np.eye(4)
                R[:3, :3] = _axis_angle(j.axis, q.get(j.name, 0.0))
                T = T @ R
        return T
