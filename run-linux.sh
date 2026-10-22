#!/usr/bin/env bash
set -euo pipefail
pbs_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ -x "$pbs_dir/PlaguesBroadcastStation" && -d "$pbs_dir/_internal" ]]; then
    exec "$pbs_dir/PlaguesBroadcastStation" "$@"
fi
if [[ -x "$pbs_dir/dist/PlaguesBroadcastStation/PlaguesBroadcastStation" ]]; then
    exec "$pbs_dir/dist/PlaguesBroadcastStation/PlaguesBroadcastStation" "$@"
fi
if [[ -x "$pbs_dir/.venv/bin/python" ]]; then
    exec "$pbs_dir/.venv/bin/python" "$pbs_dir/launch.py" "$@"
fi
printf '%s\n' 'Build the app with ./build-linux.sh, or install it in .venv with: python3 -m venv .venv && .venv/bin/python -m pip install .'
exit 1
