#!/usr/bin/env bash
# Build (if needed) and launch the LeKiwi / Orin console GUI.
#   ./run.sh            debug build (fast compile)
#   ./run.sh --release  optimized build
set -euo pipefail
cd "$(dirname "$0")/src-tauri"

# rustup installs cargo in ~/.cargo/bin, which non-login shells miss
[[ -d "$HOME/.cargo/bin" ]] && PATH="$HOME/.cargo/bin:$PATH"
command -v cargo >/dev/null || { echo "cargo not found — install rustup first: https://rustup.rs" >&2; exit 1; }

profile=""
[[ "${1:-}" == "--release" ]] && profile="--release"

# cargo rebuilds only when src-tauri/ or ui/ changed (see build.rs)
exec cargo run --quiet $profile
