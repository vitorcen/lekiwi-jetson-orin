#!/usr/bin/env bash
# One-time environment setup for the LeKiwi voice frontend on the Jetson Orin Nano.
# Idempotent — safe to re-run; skips anything already in place.
#
# Target: aarch64 / Ubuntu 22.04 / Python 3.10. CN network mirrors:
#   PyPI  -> TUNA (https://pypi.tuna.tsinghua.edu.cn/simple)
#   HF    -> direct (huggingface.co)   [hf-mirror.com is dead, do NOT use it]
#   GitHub-> ghfast.top prefix
# Board RAM is tight (llama-server resident) — this script never compiles from
# source: sherpa-onnx is pinned to a version with a prebuilt cp310 aarch64 wheel.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$HERE/.venv"
MODELS="$HERE/models"
PIP_INDEX="https://pypi.tuna.tsinghua.edu.cn/simple"
GHFAST="https://ghfast.top"
PY=/usr/bin/python3.10

msg()  { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!! \033[0m %s\n' "$*"; }

# ---------------------------------------------------------------- venv + pip
# python3.10-venv (ensurepip) is NOT installed on this box and apt needs sudo,
# so if the normal venv has no pip we bootstrap it with get-pip.py.
if [[ ! -x "$VENV/bin/python" ]]; then
  msg "creating venv (python3.10) -> $VENV"
  "$PY" -m venv "$VENV" 2>/dev/null || "$PY" -m venv --without-pip "$VENV"
fi
if ! "$VENV/bin/python" -m pip --version &>/dev/null; then
  msg "no pip in venv — bootstrapping with get-pip.py"
  GP=/tmp/get-pip.py
  curl -fsSL -o "$GP" https://bootstrap.pypa.io/get-pip.py \
    || curl -fsSL -o "$GP" "$GHFAST/https://raw.githubusercontent.com/pypa/get-pip/main/public/get-pip.py"
  "$VENV/bin/python" "$GP" -i "$PIP_INDEX"
fi
msg "pip ready: $("$VENV/bin/pip" --version)"
"$VENV/bin/pip" install --quiet --upgrade pip -i "$PIP_INDEX"

# ---------------------------------------------------------------- python deps
# sherpa-onnx 1.10.46 ships a cp310 manylinux2014_aarch64 wheel on TUNA.
# --only-binary guarantees we never fall back to a source build.
if ! "$VENV/bin/python" -c "import sherpa_onnx" &>/dev/null; then
  msg "pip install sherpa-onnx (wheel only)"
  "$VENV/bin/pip" install --only-binary=:all: "sherpa-onnx==1.10.46" -i "$PIP_INDEX"
else
  msg "sherpa-onnx already installed"
fi
for mod in edge_tts aiohttp numpy; do
  "$VENV/bin/python" -c "import $mod" &>/dev/null && continue
  pkg=${mod//_/-}
  msg "pip install $pkg"
  "$VENV/bin/pip" install "$pkg" -i "$PIP_INDEX"
done

# ---------------------------------------------------------------- models
# fetch <url> <dest> <min_bytes> — skip if a plausibly-complete file exists.
fetch() {
  local url="$1" dest="$2" min="$3"
  if [[ -f "$dest" ]] && (( $(stat -c%s "$dest") >= min )); then
    msg "have $(basename "$dest") ($(du -h "$dest" | cut -f1))"
    return
  fi
  msg "download $(basename "$dest")"
  mkdir -p "$(dirname "$dest")"
  curl -fsSL -o "$dest" "$url"
  local got; got=$(stat -c%s "$dest")
  (( got >= min )) || { warn "$(basename "$dest") too small ($got < $min)"; return 1; }
}

HF_SV="https://huggingface.co/csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/resolve/main"
HF_MELO="https://huggingface.co/csukuangfj/vits-melo-tts-zh_en/resolve/main"

# Silero VAD (~2MB)
fetch "$GHFAST/https://github.com/snakers4/silero-vad/raw/master/src/silero_vad/data/silero_vad.onnx" \
      "$MODELS/silero_vad.onnx" 2000000

# SenseVoice ASR (~228MB int8 + tokens)
fetch "$HF_SV/model.int8.onnx" "$MODELS/sense-voice/model.int8.onnx" 200000000
fetch "$HF_SV/tokens.txt"      "$MODELS/sense-voice/tokens.txt"      1000

# Melo TTS (zh_en): model + lexicon/tokens + normalization fsts + jieba dict
fetch "$HF_MELO/model.onnx"   "$MODELS/vits-melo-tts-zh_en/model.onnx"   150000000
fetch "$HF_MELO/lexicon.txt"  "$MODELS/vits-melo-tts-zh_en/lexicon.txt"  1000000
fetch "$HF_MELO/tokens.txt"   "$MODELS/vits-melo-tts-zh_en/tokens.txt"   100
for f in date.fst number.fst phone.fst new_heteronym.fst; do
  fetch "$HF_MELO/$f" "$MODELS/vits-melo-tts-zh_en/$f" 1000
done
for f in README.md hmm_model.utf8 idf.utf8 jieba.dict.utf8 stop_words.utf8 user.dict.utf8; do
  fetch "$HF_MELO/dict/$f" "$MODELS/vits-melo-tts-zh_en/dict/$f" 1
done
for f in char_state_tab.utf8 prob_emit.utf8 prob_start.utf8 prob_trans.utf8; do
  fetch "$HF_MELO/dict/pos_dict/$f" "$MODELS/vits-melo-tts-zh_en/dict/pos_dict/$f" 1
done

# ---------------------------------------------------------------- ffmpeg note
command -v ffmpeg >/dev/null || warn "ffmpeg not found — needed to decode edge-tts mp3"

# ---------------------------------------------------------------- control token
# HTTP 控制面 Bearer token(0600)。缺失则生成;已存在则保留(daemon 也会自愈生成)。
if [[ ! -s "$HERE/token" ]]; then
  msg "generating control token -> $HERE/token"
  ( umask 077; openssl rand -hex 24 > "$HERE/token" )
  chmod 600 "$HERE/token"
else
  msg "control token present"
fi

# ---------------------------------------------------------------- systemd (user)
# 幂等安装 voice-daemon.service:把 WorkingDirectory/ExecStart 写死成本机真实路径,
# 再 daemon-reload + enable --now。重跑安全(覆盖单元文件、enable --now 幂等)。
UNIT_SRC="$HERE/systemd/voice-daemon.service"
UNIT_DIR="$HOME/.config/systemd/user"
if command -v systemctl >/dev/null && [[ -f "$UNIT_SRC" ]]; then
  msg "installing user unit voice-daemon.service (WorkingDirectory=$HERE)"
  mkdir -p "$UNIT_DIR"
  # 用真实 $HERE 替换 %h/work/... 路径,避免 checkout 位置不同导致失效
  sed -e "s#WorkingDirectory=.*#WorkingDirectory=$HERE#" \
      -e "s#ExecStart=.*#ExecStart=$VENV/bin/python $HERE/daemon.py#" \
      "$UNIT_SRC" > "$UNIT_DIR/voice-daemon.service"
  if systemctl --user daemon-reload 2>/dev/null; then
    systemctl --user enable --now voice-daemon.service 2>/dev/null \
      && msg "voice-daemon.service enabled + started" \
      || warn "enable --now failed (no user session bus? run: systemctl --user enable --now voice-daemon.service)"
  else
    warn "systemctl --user unavailable here — enable later inside a user session:"
    warn "  systemctl --user daemon-reload && systemctl --user enable --now voice-daemon.service"
  fi
else
  warn "systemctl or unit file missing — skipping service install"
fi

msg "voice env ready. venv: $VENV"
msg "smoke-test:  $VENV/bin/python -c 'import sherpa_onnx, edge_tts, aiohttp, numpy; print(\"ok\")'"
