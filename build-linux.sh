#!/usr/bin/env bash
set -euo pipefail
pbs_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$pbs_dir"
if [[ ! -x .venv/bin/python ]]; then
    python3 -m venv .venv
fi
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install '.[build,test]'
QT_QPA_PLATFORM=offscreen QT_MEDIA_BACKEND=ffmpeg .venv/bin/python -m pytest -q
.venv/bin/python -m PyInstaller --clean --noconfirm PlaguesBroadcastStation.spec
cp run-linux.sh dist/PlaguesBroadcastStation/run-linux.sh
cp README.md THIRD_PARTY_NOTICES.md requirements-lyrics.txt dist/PlaguesBroadcastStation/
cp -R docs dist/PlaguesBroadcastStation/
chmod +x dist/PlaguesBroadcastStation/run-linux.sh
mkdir -p artifacts
.venv/bin/python - <<'PY'
from pathlib import Path
import platform, shutil
name = 'PlaguesBroadcastStation-linux-' + platform.machine()
archive = shutil.make_archive(str(Path('artifacts') / name), 'zip', 'dist', 'PlaguesBroadcastStation')
print('Built:', archive)
PY
