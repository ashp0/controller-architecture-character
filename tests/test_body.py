"""Body-side invariants: limits are never exceeded, look-at converges, aim is measurable."""
import math
import random
import time
from pathlib import Path

import pytest

from architecture.body.controller import GESTURES, JOINTS, POSTURES, Body
from architecture.urdf import Robot

URDF = Path(__file__).resolve().parents[1] / "robot" / "dummy_lamp_5dof.urdf"


@pytest.fixture
def body():
    return Body(Robot(URDF))


def test_urdf_has_five_revolute_joints():
    r = Robot(URDF)
    assert sorted(r.actuated) == sorted(JOINTS)


def test_postures_within_soft_limits():
    r = Robot(URDF)
    for name, q in POSTURES.items():
        for j, v in q.items():
            lo, hi = r.limits(j)
            assert lo <= v <= hi, (name, j, v)


def test_random_commands_never_exceed_limits(body):
    random.seed(0)
    r = body.robot
    for _ in range(40):
        body.set_posture(random.choice(list(POSTURES)), 1.0)
        body.look_uv(random.uniform(-0.5, 1.5), random.uniform(-0.5, 1.5))
        body.gesture(random.choice(list(GESTURES)))
        for _ in range(20):
            body.step()
            for j in JOINTS:
                lo, hi = r.limits(j, soft=False)
                assert lo - 1e-9 <= body.q[j] <= hi + 1e-9
                assert abs(body.dq[j]) <= r.joints[j].velocity + 1e-6


@pytest.mark.parametrize("u,v", [(0.5, 0.5), (0.85, 0.45), (0.15, 0.6), (0.5, 0.3)])
def test_look_at_converges(body, u, v):
    body.breath = False
    body.set_posture("attentive", 1.0)
    body.look_uv(u, v)
    for _ in range(500):
        body.step()
        time.sleep(0.004)
    au, av = body.aim_uv()
    assert math.dist((au, av), (u, v)) < 0.03
