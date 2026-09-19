#!/usr/bin/env bash
# Download Whisper weights and a default Piper voice into models/.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

mkdir -p models/whisper models/piper bin temp memory

WHISPER_URL="https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin"
PIPER_BASE="https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/libritts_r/medium"
PIPER_ONNX="en_US-libritts_r-medium.onnx"
PIPER_JSON="en_US-libritts_r-medium.onnx.json"

if [[ ! -f models/whisper/ggml-base.en.bin ]]; then
  echo "Downloading Whisper ggml-base.en.bin ..."
  curl -L --fail --retry 3 -o models/whisper/ggml-base.en.bin "$WHISPER_URL"
else
  echo "Whisper model already present."
fi

if [[ ! -f models/piper/$PIPER_ONNX ]]; then
  echo "Downloading Piper $PIPER_ONNX ..."
  curl -L --fail --retry 3 -o "models/piper/$PIPER_ONNX" "$PIPER_BASE/$PIPER_ONNX"
  curl -L --fail --retry 3 -o "models/piper/$PIPER_JSON" "$PIPER_BASE/$PIPER_JSON"
else
  echo "Piper voice already present."
fi

echo
echo "Starter Whisper + Piper files are under models/ (examples only)."
echo "Replace them with any whisper.cpp ggml and Piper ONNX voice you want."
echo "Still needed: whisper-cli in bin/ and Ollama (https://ollama.com)."
echo "  whisper.cpp: https://github.com/ggml-org/whisper.cpp"
echo "  then: ollama pull qwen2.5:7b   # or any other local chat model"
