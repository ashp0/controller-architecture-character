"""Check camera, microphone and speaker before a demo.

    .venv/bin/python scripts/check_devices.py [--camera 0]
"""
from __future__ import annotations

import argparse
import os
import platform
import sys
import time

import numpy as np


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--camera", default="0")
    a = ap.parse_args()
    term = os.environ.get("TERM_PROGRAM", "unknown terminal")
    print(f"Running inside: {term}  (macOS grants camera/mic per app; this is the app that needs permission)\n")

    import cv2

    print("CAMERA")
    cap = cv2.VideoCapture(int(a.camera) if a.camera.isdigit() else a.camera)
    ok, frame = False, None
    t0 = time.time()
    while time.time() - t0 < 5 and not ok:  # macOS may show the permission prompt here
        ok, frame = cap.read()
        if not ok:
            time.sleep(0.3)
    if ok:
        print(f"  OK: {frame.shape[1]}x{frame.shape[0]} frames from camera {a.camera}")
    else:
        print("  FAILED: no frames.")
        if platform.system() == "Darwin":
            print(f"  -> System Settings > Privacy & Security > Camera: turn ON '{term}'.")
            print("     If it is not listed (or never asked), reset it and run this again:")
            bundle = {"Apple_Terminal": "com.apple.Terminal", "iTerm.app": "com.googlecode.iterm2",
                      "vscode": "com.microsoft.VSCode"}.get(term, "<app bundle id>")
            print(f"       tccutil reset Camera {bundle}")
    cap.release()

    import sounddevice as sd

    print("\nMICROPHONE (speak for 2 seconds)")
    try:
        rec = sd.rec(int(2 * 16000), samplerate=16000, channels=1, dtype="float32")
        sd.wait()
        peak = float(np.max(np.abs(rec)))
        if peak < 1e-4:
            print("  FAILED: silence (all zeros) -> usually Microphone permission: Privacy & Security > Microphone.")
        else:
            print(f"  OK: peak level {peak:.3f} {'(quiet: move closer or raise input volume)' if peak < 0.02 else ''}")
    except Exception as e:
        print("  FAILED:", e)

    print("\nSPEAKER (you should hear a short tone)")
    try:
        t = np.arange(int(0.4 * 44100)) / 44100
        sd.play((0.2 * np.sin(2 * np.pi * 660 * t)).astype(np.float32), 44100)
        sd.wait()
        print("  played")
    except Exception as e:
        print("  FAILED:", e)
    print("\nDevices:\n" + str(sd.query_devices()))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
