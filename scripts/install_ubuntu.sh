#!/usr/bin/env bash
# One-time setup on a clean Ubuntu 24.04 LTS laptop (4 cores, 8 GB, no GPU).
set -euo pipefail
cd "$(dirname "$0")/.."

sudo apt-get update
# python venv, PortAudio for sounddevice (talks to PipeWire/PulseAudio via ALSA), libsndfile.
sudo apt-get install -y python3-venv python3-dev libportaudio2 libsndfile1 v4l-utils

python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
.venv/bin/python scripts/download_models.py

[ -f .env ] || cp .env.example .env
echo
echo "Setup done. Put your OpenCode Go key in .env, then run:  ./run.sh"
echo "Camera check:  v4l2-ctl --list-devices      Audio check:  .venv/bin/python -m sounddevice"
