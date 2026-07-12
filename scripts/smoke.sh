#!/usr/bin/env bash
# No-network smoke test for a built image: verifies the runtime pieces the bot
# needs are actually present and importable. Usage: scripts/smoke.sh <image>
set -euo pipefail

image="${1:?usage: smoke.sh <image>}"

run() {
    docker run --rm --network none --entrypoint "$1" "$image" "${@:2}"
}

run ffmpeg -version | head -1
run deno --version
run yt-dlp --version
run python -c '
import importlib.metadata as metadata

import musicbot.music
import musicbot.player
import musicbot.sources

print("yt-dlp-ejs", metadata.version("yt-dlp-ejs"))
'

echo "smoke test passed for $image"
