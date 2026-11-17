#!/usr/bin/env bash
# Linux/macOS: cần ffmpeg trong PATH (apt install ffmpeg / brew install ffmpeg)
cd "$(dirname "$0")/backend"
[ -d .venv ] || python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt -r requirements-ai.txt
python launcher.py
