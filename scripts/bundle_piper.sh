#!/bin/bash
# Download a static Piper binary and the Greek rapunzelina voice into /opt/piper
# (or $PIPER_HOME). Safe to re-run: existing files are left in place.
set -euo pipefail

DEST="${PIPER_HOME:-/opt/piper}"
MODEL_NAME="el_GR-rapunzelina-medium"
PIPER_URL="${PIPER_TARBALL_URL:-https://github.com/rhasspy/piper/releases/download/2023.11.14-2/piper_linux_x86_64.tar.gz}"
MODEL_BASE="${PIPER_MODEL_BASE:-https://huggingface.co/rhasspy/piper-voices/resolve/main/el/el_GR/rapunzelina/medium}"

mkdir -p "$DEST/models"
if [ ! -x "$DEST/piper" ]; then
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' EXIT
  echo "Downloading Piper into $DEST"
  wget -q -O "$tmp/piper.tgz" "$PIPER_URL"
  tar -xzf "$tmp/piper.tgz" -C "$tmp"
  # tarball may unpack as ./piper/piper or ./piper
  if [ -x "$tmp/piper/piper" ]; then
    cp -a "$tmp/piper/." "$DEST/"
  elif [ -x "$tmp/piper" ]; then
    cp -a "$tmp/piper" "$DEST/piper"
  else
    echo "Piper binary not found in tarball" >&2
    exit 1
  fi
  chmod +x "$DEST/piper"
fi

if [ ! -f "$DEST/models/${MODEL_NAME}.onnx" ]; then
  echo "Downloading Piper voice ${MODEL_NAME}"
  wget -q -O "$DEST/models/${MODEL_NAME}.onnx" "${MODEL_BASE}/${MODEL_NAME}.onnx?download=true"
  wget -q -O "$DEST/models/${MODEL_NAME}.onnx.json" "${MODEL_BASE}/${MODEL_NAME}.onnx.json?download=true"
fi

# Smoke-check the binary (model load is left to runtime).
"$DEST/piper" --help >/dev/null
echo "Piper bundled at $DEST"
