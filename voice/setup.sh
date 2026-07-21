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
#
# Dual-venv layout (rollback safety, 2026-07-21): on the board .venv is a SYMLINK,
# not a directory. Two real venvs sit beside it:
#   .venv-stable  — the sherpa-onnx 1.10.46 baseline (no TEN VAD), kept as a
#                   seconds-level rollback cushion.
#   .venv-exp     — the current sherpa-onnx 1.13.4 venv (adds TEN VAD support).
#   .venv         — symlink -> .venv-exp (the live one the systemd unit runs).
# Roll back with:  ln -sfn .venv-stable .venv && systemctl --user restart voice-daemon
# Roll forward:    ln -sfn .venv-exp .venv && systemctl --user restart voice-daemon
# A fresh install below creates a single real .venv (1.13.4); the dual layout is
# only materialised on demand when upgrading an already-deployed board.
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
# sherpa-onnx 1.13.4 ships a cp310 manylinux2014_aarch64 wheel on TUNA (splits into
# sherpa-onnx + sherpa-onnx-core, both wheels — pip pulls the core dep automatically).
# 1.13.x is the first line whose VadModelConfig carries the ten_vad field, so TEN VAD
# (voice_vad.py) lights up here; 1.10.46 (the .venv-stable baseline) has no ten_vad.
# --only-binary guarantees we never fall back to a source build (board RAM is tight).
if ! "$VENV/bin/python" -c "import sherpa_onnx" &>/dev/null; then
  msg "pip install sherpa-onnx (wheel only)"
  "$VENV/bin/pip" install --only-binary=:all: "sherpa-onnx==1.13.4" -i "$PIP_INDEX"
else
  msg "sherpa-onnx already installed"
fi
for mod in edge_tts aiohttp numpy; do
  "$VENV/bin/python" -c "import $mod" &>/dev/null && continue
  pkg=${mod//_/-}
  msg "pip install $pkg"
  "$VENV/bin/pip" install "$pkg" -i "$PIP_INDEX"
done
# webrtcvad: optional VAD engine (P2.7). Builds a cp310 aarch64 wheel from source.
# Its module does `import pkg_resources`, which setuptools>=81 removed — pin
# setuptools<81 so the import works. Best-effort: the 'energy' engine is the
# zero-dependency baseline, so a failed webrtcvad build must NOT abort setup
# (voice_vad just reports webrtc unavailable and the GUI greys it out).
if ! "$VENV/bin/python" -c "import webrtcvad" &>/dev/null; then
  msg "pip install webrtcvad (+ setuptools<81 for pkg_resources)"
  "$VENV/bin/pip" install "setuptools<81" webrtcvad -i "$PIP_INDEX" \
    || warn "webrtcvad unavailable (energy VAD still works); skipping"
fi
# ruamel.yaml: round-trip yaml patch for the Hermes brain switch (§5.5) — keeps
# the profile config.yaml's comments/other bytes intact while touching only
# model.*/providers.<name>.
if ! "$VENV/bin/python" -c "import ruamel.yaml" &>/dev/null; then
  msg "pip install ruamel.yaml"
  "$VENV/bin/pip" install "ruamel.yaml" -i "$PIP_INDEX"
else
  msg "ruamel.yaml already installed"
fi

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
HF_PARA="https://huggingface.co/csukuangfj/sherpa-onnx-paraformer-zh-2024-03-09/resolve/main"
HF_WHISPER="https://huggingface.co/csukuangfj/sherpa-onnx-whisper-turbo/resolve/main"
HF_STREAM="https://huggingface.co/csukuangfj/sherpa-onnx-streaming-zipformer-bilingual-zh-en-2023-02-20/resolve/main"
HF_MELO="https://huggingface.co/csukuangfj/vits-melo-tts-zh_en/resolve/main"

# Silero VAD (~2MB)
fetch "$GHFAST/https://github.com/snakers4/silero-vad/raw/master/src/silero_vad/data/silero_vad.onnx" \
      "$MODELS/silero_vad.onnx" 2000000

# TEN VAD (~324KB) — needs sherpa-onnx >=1.13 (ten_vad field). Filename must be
# ten-vad.onnx to match voice_vad.TEN_MODEL. Unavailable on the 1.10.46 baseline.
fetch "$GHFAST/https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/ten-vad.onnx" \
      "$MODELS/ten-vad.onnx" 100000

# SenseVoice ASR (~228MB int8 + tokens)
fetch "$HF_SV/model.int8.onnx" "$MODELS/sense-voice/model.int8.onnx" 200000000
fetch "$HF_SV/tokens.txt"      "$MODELS/sense-voice/tokens.txt"      1000

# Paraformer-large ASR (zh, ~232MB int8 + tokens) — A/B alternative to SenseVoice,
# non-autoregressive same footprint; often stronger on Mandarin homophones.
fetch "$HF_PARA/model.int8.onnx" "$MODELS/paraformer-zh/model.int8.onnx" 200000000
fetch "$HF_PARA/tokens.txt"      "$MODELS/paraformer-zh/tokens.txt"      1000

# Whisper large-v3-turbo ASR (multilingual, ~1GB int8) — heavy A/B only; autoregressive,
# ~1.5GB RAM + higher latency on Orin CPU, run with the vision service stopped.
fetch "$HF_WHISPER/turbo-encoder.int8.onnx" "$MODELS/whisper-turbo/turbo-encoder.int8.onnx" 200000000
fetch "$HF_WHISPER/turbo-decoder.int8.onnx" "$MODELS/whisper-turbo/turbo-decoder.int8.onnx" 80000000
fetch "$HF_WHISPER/turbo-tokens.txt"        "$MODELS/whisper-turbo/turbo-tokens.txt"        100000

# Qwen3-ASR-0.6B int8 ASR (LLM-ASR, sherpa-onnx native from_qwen3_asr; needs sherpa >=1.13
# with qwen3-asr support). Strong noise/far-field robustness; ~3.5GB RSS so run with the
# vision service stopped. Ships as a tarball (conv_frontend/encoder/decoder + tokenizer dir).
# Streaming zipformer models (DEBUG streaming mode 免VAD, 二级下拉可选). Chinese-trained
# ones (zh-2025 / multi-zh) are much cleaner than the old bilingual (which repeats tokens).
# fetch_stream_tar <pkg-name> <target-dir>: download a GitHub asr-models tarball + extract.
fetch_stream_tar() {
  local pkg="$1" dir="$MODELS/$2"
  if [[ -f "$dir/tokens.txt" ]]; then msg "have $2 ($(du -sh "$dir" | cut -f1))"; return; fi
  msg "download + extract $2"
  local tmp; tmp="$(mktemp --suffix=.tar.bz2)"
  curl -fsSL -o "$tmp" \
    "$GHFAST/https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/$pkg.tar.bz2" \
    || curl -fsSL -o "$tmp" \
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/$pkg.tar.bz2"
  mkdir -p "$dir"; tar xjf "$tmp" -C "$dir" --strip-components=1; rm -f "$tmp"
  [[ -f "$dir/tokens.txt" ]] || warn "$2 extract failed"
}
fetch_stream_tar "sherpa-onnx-streaming-zipformer-zh-int8-2025-06-30"          "streaming-zh-2025"
fetch_stream_tar "sherpa-onnx-streaming-zipformer-zh-xlarge-int8-2025-06-30"   "streaming-zh-xlarge"
fetch_stream_tar "sherpa-onnx-streaming-zipformer-multi-zh-hans-int8-2023-12-13" "streaming-multi-zh"
# 老双语基线(HF 单文件)
STREAM_D="$MODELS/streaming-zipformer-zh-en"
for f in encoder-epoch-99-avg-1.int8.onnx decoder-epoch-99-avg-1.int8.onnx \
         joiner-epoch-99-avg-1.int8.onnx tokens.txt; do
  min=1000; [[ "$f" == *.onnx ]] && min=1000000
  fetch "$HF_STREAM/$f" "$STREAM_D/$f" "$min"
done

# X-ASR zh-en (2026, 160M params / 1M-hour data, code-switching; fp32 only ~584MB.
# True-streaming zipformer2 transducer, 480ms chunk variant).
HF_XASR="https://huggingface.co/GilgameshWind/X-ASR-zh-en/resolve/main/deployment/models/chunk-480ms-model"
XASR_D="$MODELS/streaming-x-asr-zh-en"
fetch "$HF_XASR/encoder-480ms.onnx" "$XASR_D/encoder-480ms.onnx" 500000000
fetch "$HF_XASR/decoder-480ms.onnx" "$XASR_D/decoder-480ms.onnx" 5000000
fetch "$HF_XASR/joiner-480ms.onnx"  "$XASR_D/joiner-480ms.onnx"  5000000
fetch "$HF_XASR/tokens.txt"         "$XASR_D/tokens.txt"         1000

# Streaming Paraformer-large bilingual zh-en (DAMO online, int8 ~226MB, RTF~0.15;
# fp32 is the 825MB class — int8 is the same model quantized).
HF_SPARA="https://huggingface.co/csukuangfj/sherpa-onnx-streaming-paraformer-bilingual-zh-en/resolve/main"
SPARA_D="$MODELS/streaming-paraformer-zh-en"
fetch "$HF_SPARA/encoder.int8.onnx" "$SPARA_D/encoder.int8.onnx" 100000000
fetch "$HF_SPARA/decoder.int8.onnx" "$SPARA_D/decoder.int8.onnx" 50000000
fetch "$HF_SPARA/tokens.txt"        "$SPARA_D/tokens.txt"        1000

QWEN3_DIR="$MODELS/qwen3-asr"
QWEN3_TAR="sherpa-onnx-qwen3-asr-0.6B-int8-2026-03-25.tar.bz2"
if [[ -f "$QWEN3_DIR/encoder.int8.onnx" ]]; then
  msg "have qwen3-asr ($(du -sh "$QWEN3_DIR" | cut -f1))"
else
  msg "download + extract qwen3-asr (~880MB tarball)"
  tmp_tar="$(mktemp --suffix=.tar.bz2)"
  curl -fsSL -o "$tmp_tar" \
    "$GHFAST/https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/$QWEN3_TAR" \
    || curl -fsSL -o "$tmp_tar" \
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/$QWEN3_TAR"
  mkdir -p "$QWEN3_DIR"
  tar xjf "$tmp_tar" -C "$QWEN3_DIR" --strip-components=1
  rm -f "$tmp_tar"
  [[ -f "$QWEN3_DIR/encoder.int8.onnx" ]] || warn "qwen3-asr extract failed"
fi

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

# Matcha TTS (zh-en, realtime: board RTF 0.18 vs melo 1.60) + 16k vocos vocoder.
# The zh-en acoustic model REQUIRES the 16khz vocoder — 22khz plays wrong.
MATCHA_D="$MODELS/matcha-icefall-zh-en"
if [[ ! -f "$MATCHA_D/tokens.txt" ]]; then
  msg "download + extract matcha-icefall-zh-en"
  tmp="$(mktemp --suffix=.tar.bz2)"
  curl -fsSL -o "$tmp" \
    "$GHFAST/https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/matcha-icefall-zh-en.tar.bz2" \
    || curl -fsSL -o "$tmp" \
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/matcha-icefall-zh-en.tar.bz2"
  mkdir -p "$MATCHA_D"; tar xjf "$tmp" -C "$MATCHA_D" --strip-components=1; rm -f "$tmp"
  [[ -f "$MATCHA_D/tokens.txt" ]] || warn "matcha extract failed"
else msg "have matcha-icefall-zh-en ($(du -sh "$MATCHA_D" | cut -f1))"; fi
fetch "$GHFAST/https://github.com/k2-fsa/sherpa-onnx/releases/download/vocoder-models/vocos-16khz-univ.onnx" \
      "$MODELS/vocos-16khz-univ.onnx" 40000000 \
  || fetch "https://github.com/k2-fsa/sherpa-onnx/releases/download/vocoder-models/vocos-16khz-univ.onnx" \
      "$MODELS/vocos-16khz-univ.onnx" 40000000

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

# ---------------------------------------------------------------- unified config
# 板端统一 config(desired state)。首次装机缺失才拷贝 example → ~/.config/lekiwi/,
# 已存在则不动(部署 rsync 覆盖 board/ 时不会踩掉用户存的 preset/搭配)。
CFG_DIR="$HOME/.config/lekiwi"
CFG_DST="$CFG_DIR/config.json"
CFG_SRC="$HERE/../board/config.example.json"
if [[ ! -f "$CFG_DST" && -f "$CFG_SRC" ]]; then
  msg "installing unified config -> $CFG_DST (from example)"
  mkdir -p "$CFG_DIR"
  cp "$CFG_SRC" "$CFG_DST"
elif [[ -f "$CFG_DST" ]]; then
  msg "unified config present: $CFG_DST (left untouched)"
else
  warn "config example missing ($CFG_SRC) — daemon will use built-in defaults"
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
