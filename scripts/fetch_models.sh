#!/usr/bin/env bash
# Downloads and unpacks Fishial model artifacts into data/models/.
# Safe to re-run; skips steps if outputs already exist.
set -euo pipefail

cd "$(dirname "$0")/.."

MODELS_DIR="data/models"
mkdir -p "$MODELS_DIR"
cd "$MODELS_DIR"

DETECTOR_URL="https://storage.googleapis.com/fishial-ml-resources/detector_v26_n3.zip"
CLASSIFIER_URL="https://storage.googleapis.com/fishial-ml-resources/classification_model_v0.10.2.zip"

fetch_and_unpack() {
  local url="$1" zip="$2" outdir="$3"
  if [[ -d "$outdir" && -f "$outdir/model.pt" ]]; then
    echo "✓ $outdir already present"
    return
  fi
  if [[ ! -f "$zip" ]]; then
    echo "↓ $zip"
    curl -sSL -o "$zip" "$url"
  fi
  mkdir -p "$outdir"
  unzip -q -o "$zip" -d "$outdir" -x "__MACOSX/*"
  echo "✓ unpacked into $outdir"
}

fetch_and_unpack "$DETECTOR_URL"   "detector_v26_n3.zip"           "detector_v26"
fetch_and_unpack "$CLASSIFIER_URL" "classification_model_v0.10.2.zip" "classifier_v0_10_2"

echo
echo "Done. Model tree:"
find detector_v26 classifier_v0_10_2 -maxdepth 1 -type f
