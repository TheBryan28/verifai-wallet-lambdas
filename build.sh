#!/usr/bin/env bash
# Builds extract.zip, analyze.zip and index-corpus.zip for the Lambda Python runtime (x86_64).
# Usage: ./build.sh [python_version]   e.g. ./build.sh 3.13
set -euo pipefail

PYTHON_VERSION="${1:-3.13}"
ROOT="$(cd "$(dirname "$0")" && pwd)"
BUILD="$ROOT/build"
rm -rf "$BUILD" && mkdir -p "$BUILD"

# build <lambda folder> <copy shared/*.py: yes|no> <pip packages...>
build() {
  local name="$1" shared="$2"; shift 2
  local target="$BUILD/$name"
  mkdir -p "$target"
  # Download wheels built for Amazon Linux (manylinux), not for this machine.
  # Newer numpy releases only publish manylinux_2_28 wheels; Amazon Linux 2023 supports both tags.
  # --ignore-requires-python: older pip versions compare against the local Python
  # instead of --python-version; the cpXY wheel tags still guarantee compatibility.
  python3 -m pip install --quiet --target "$target" \
    --platform manylinux2014_x86_64 --platform manylinux_2_28_x86_64 --implementation cp \
    --python-version "$PYTHON_VERSION" --only-binary=:all: \
    --ignore-requires-python "$@"
  cp "$ROOT/$name/app.py" "$target/"
  if [ "$shared" = "yes" ]; then
    cp "$ROOT"/shared/*.py "$target/"
  fi
  (cd "$target" && zip -qr "$BUILD/$name.zip" .)
  echo "$BUILD/$name.zip ($(du -h "$BUILD/$name.zip" | cut -f1))"
}

build extract yes pypdf
build analyze yes numpy snowballstemmer
build index-corpus yes numpy snowballstemmer
