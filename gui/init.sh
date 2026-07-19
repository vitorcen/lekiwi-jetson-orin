#!/usr/bin/env bash
# One-time environment setup for building the LeKiwi console GUI on this box.
# Idempotent — safe to re-run; skips anything already in place. Then: ./run.sh
#
# Assumes apt is already on a CN mirror with IPv4 forced (see
# .memory/jetson-apt-network-cn.md) — this script does not touch apt sources.
set -euo pipefail

msg() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }

# ---------------------------------------------------------------- apt packages
# Tauri 2 on Linux needs the webkit/gtk dev headers; serialport needs libudev.
APT_PKGS=(
  build-essential pkg-config curl
  libwebkit2gtk-4.1-dev libgtk-3-dev librsvg2-dev
  libayatana-appindicator3-dev libssl-dev libudev-dev
)
missing=()
for p in "${APT_PKGS[@]}"; do
  dpkg -s "$p" &>/dev/null || missing+=("$p")
done
if ((${#missing[@]})); then
  msg "apt install: ${missing[*]}"
  sudo apt-get update
  sudo apt-get install -y "${missing[@]}"
else
  msg "apt packages already installed"
fi

# ---------------------------------------------------------------- rustup/cargo
# CN network: rustup + crates.io go through rsproxy.cn.
export RUSTUP_DIST_SERVER=${RUSTUP_DIST_SERVER:-https://rsproxy.cn}
export RUSTUP_UPDATE_ROOT=${RUSTUP_UPDATE_ROOT:-https://rsproxy.cn/rustup}
[[ -d "$HOME/.cargo/bin" ]] && PATH="$HOME/.cargo/bin:$PATH"

if ! command -v cargo >/dev/null; then
  msg "installing rustup (stable, via rsproxy.cn)"
  curl --proto '=https' --tlsv1.2 -sSf https://rsproxy.cn/rustup-init.sh \
    | sh -s -- -y --default-toolchain stable --profile minimal
  PATH="$HOME/.cargo/bin:$PATH"
fi
# rustup shims exist but no default toolchain (interrupted install) — repair it.
if ! cargo --version &>/dev/null; then
  msg "no default toolchain — running: rustup default stable"
  rustup default stable
fi
msg "cargo ready: $(cargo --version)"

# crates.io mirror (sparse index) — only write if the user has no cargo config yet.
CARGO_CONF="$HOME/.cargo/config.toml"
if [[ ! -f "$CARGO_CONF" ]] && [[ ! -f "$HOME/.cargo/config" ]]; then
  msg "writing crates.io mirror config -> $CARGO_CONF"
  cat > "$CARGO_CONF" <<'EOF'
[source.crates-io]
replace-with = "rsproxy-sparse"

[source.rsproxy-sparse]
registry = "sparse+https://rsproxy.cn/index/"

[registries.rsproxy]
index = "sparse+https://rsproxy.cn/index/"
EOF
else
  msg "cargo config already exists — leaving it alone"
fi

msg "done. build & run with:  ./run.sh"
