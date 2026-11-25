#!/usr/bin/env bash
# Run inside a Linux GPU Pod with /workspace mounted on its work disk.
set -euo pipefail
# The base image enables an optional downloader outside YuE2's pinned environment.
export HF_HUB_ENABLE_HF_TRANSFER=0

events_file=""
if [[ $# -gt 0 ]]; then
    events_file=$(realpath -e -- "$1")
fi
nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv
cd /workspace
if ! mountpoint -q /workspace; then
    echo "Expected the Runpod work volume at /workspace; check the Pod mount before installing." >&2
    exit 1
fi
release=yue2-music-v1.2.0
if [[ ! -e YuE ]]; then
    git clone --depth 1 --branch "$release" https://github.com/multimodal-art-projection/YuE.git YuE
fi
cd YuE
if [[ "$(git rev-parse HEAD)" != "$(git rev-parse "$release^{commit}")" ]]; then
    echo "Existing checkout in /workspace/YuE is on a different revision." >&2
    exit 1
fi
cd skills/yue2-music/instrumental
python3 scripts/verify_bundle.py
python3 scripts/test_tools.py
if [[ ! -x .venv/bin/python ]]; then
    python3 scripts/setup_runtime.py install --venv .venv
fi
.venv/bin/python scripts/setup_runtime.py models --output models
.venv/bin/python scripts/setup_runtime.py doctor --require-gpu --models-root models

trial_dir=$(mktemp -d /workspace/yue2-check.XXXXXXXX)
if [[ -n "$events_file" ]]; then
    python3 scripts/compile_score.py "$events_file" --output "$trial_dir/score"
    .venv/bin/python scripts/instrumental.py cover \
        --abc "$trial_dir/score/score.abc" \
        --style "Short instrumental neo-soul performance of the supplied Turkish March theme at 130 BPM. Warm Rhodes electric piano on the melody from the first beat, round bass and laid-back drums. Play the theme once and finish with a clean cadence. No vocals." \
        --models-root models --offline --output "$trial_dir/turkish-march"
    .venv/bin/python scripts/instrumental.py verify "$trial_dir/turkish-march/generation"
    audio="$trial_dir/turkish-march/generation/native/audio.flac"
    test -s "$audio"
    echo "Verified Turkish March: $audio"
else
    .venv/bin/python scripts/instrumental.py run \
        --abc assets/example-ins.abc --source assets/example-original.abc \
        --style-file assets/example-style.txt --repairs assets/example-repairs.json \
        --models-root models --offline --output "$trial_dir/example"
    .venv/bin/python scripts/instrumental.py verify "$trial_dir/example/generation"
    test -s "$trial_dir/example/generation/native/audio.flac"
    echo "Verified audio: $trial_dir/example/generation/native/audio.flac"
fi
