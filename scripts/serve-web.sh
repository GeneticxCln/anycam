#!/usr/bin/env bash
# Serve the local getUserMedia check page on http://127.0.0.1:8777/
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT/web"
exec python3 -m http.server --bind 127.0.0.1 8777
