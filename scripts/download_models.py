"""Fetch the three local models (~210 MB total) into models/.

- YuNet face detector (230 KB)              opencv_zoo
- Piper voice en_GB-alan-medium (63 MB)     rhasspy/piper-voices
- faster-whisper base.en int8 (~145 MB)     Systran/faster-whisper-base.en
"""
from __future__ import annotations

import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
M = ROOT / "models"
M.mkdir(exist_ok=True)

yunet = M / "face_detection_yunet_2023mar.onnx"
if not yunet.exists():
    print("downloading YuNet face detector")
    urllib.request.urlretrieve(
        "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx", yunet)

if not (M / "en_GB-alan-medium.onnx").exists():
    print("downloading Piper voice")
    subprocess.check_call([sys.executable, "-m", "piper.download_voices", "en_GB-alan-medium"], cwd=M)

whisper = sys.argv[1] if len(sys.argv) > 1 else "base.en"
print(f"downloading faster-whisper {whisper}")
from faster_whisper import WhisperModel  # noqa: E402

WhisperModel(whisper, device="cpu", compute_type="int8", download_root=str(M / "whisper"))
print("models ready in", M)
